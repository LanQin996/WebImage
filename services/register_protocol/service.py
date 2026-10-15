# -*- coding: utf-8 -*-
"""注册编排服务：串行/小并发跑协议注册，产出 codex token 落 chatgpt2api 号池。

流程（对齐 turb-gpt-free-register main.run_registration）：
  预检 → 匿名 bootstrap → providers/csrf/signin → follow authorize
  → 密码页 + sentinel bundle → user/register → OTP 发送
  → 轮询 cloudflare_temp_email 取 OTP → validate
  → about-you/create_account（或 external_url 直接回调）
  → 取 token 落号池

取 token 有两条路（token_source）：
  platform（默认）：授权/注册/换 token 走同一条 platform OAuth 流，落 source_type=web。
    不发起第二次登录，因此不会撞上 Codex 的手机号验证。
  codex：注册完另开干净 session 走 Codex CLI 授权，落 source_type=codex。
    OpenAI 对该链路常强制手机号验证，没有接码平台时大概率失败。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable

from . import chatgpt_auth, openai_auth, platform_oauth
from .chatgpt_bootstrap import anonymous_bootstrap
from .codex_oauth import _parse_id_token, run_codex_oauth
from .humanize import delay as human_delay
from .mail_provider import CfTempMailClient, MailProviderError, TempMailbox
from .openai_auth import (
    AccountUnusableError,
    EmailOtpInvalidError,
    build_sentinel_header,
    create_account,
    follow_authorize,
    generate_registration_password,
    navigate_about_you,
    navigate_create_account_password,
    navigate_email_otp_send,
    network_preflight,
    register_user,
    request_password_sentinel_bundle,
    request_sentinel_token,
    send_email_otp,
    validate_email_otp,
)
from .profile_utils import generate_random_birthday, random_display_name
from .proxy_pool import mask_proxy, pick_proxy, pick_proxy_candidates
from .session import BrowserSession

logger = logging.getLogger(__name__)

_FINALIZE_SESSION_MAX_ATTEMPTS = 3
_FINALIZE_SESSION_BACKOFF_BASE = 2.0


class RegisterStopRequested(RuntimeError):
    pass


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)
    logger.info("[OAuth回调] 跟随 continue_url ...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    session.observe_chatgpt_document(resp)
    logger.info(f"[OAuth回调] 完成, 落点: {resp.url}")
    return str(resp.url)


def fetch_session(session: BrowserSession) -> dict:
    resp = session.get(
        "https://chatgpt.com/api/auth/session",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/"),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("accessToken"):
        raise RuntimeError("session 响应缺少 accessToken，登录态未建立")
    return data


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")
    last_exc: Exception | None = None
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            follow_oauth_callback(session, continue_url, referer=callback_referer)
            human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            return session_info, access_token
        except Exception as exc:
            last_exc = exc
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(f"[登录态] 回调/拉 Token 失败：{type(exc).__name__}: {str(exc)[:180]}，{backoff:.1f}s 后重试")
            time.sleep(backoff)
    raise RuntimeError(f"OAuth 回调/拉 Token 重试耗尽：{last_exc}") from last_exc


class RegisterTask:
    """单个账号的一次注册运行（同步阻塞，在线程里跑）。"""

    def __init__(
        self,
        *,
        settings: dict,
        mail_name: str | None = None,
        display_name: str | None = None,
        birthday: str | None = None,
        password: str | None = None,
        stop_event: threading.Event | None = None,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.mail_name = mail_name
        self.display_name = display_name
        self.birthday = birthday
        self.password = password
        self.stop_event = stop_event or threading.Event()
        self.progress_cb = progress_cb or (lambda msg: None)
        self.email = ""
        self.registration_password = ""
        self.mailbox: TempMailbox | None = None

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise RegisterStopRequested("任务已手动停止")

    def _progress(self, msg: str) -> None:
        self.progress_cb(msg)
        logger.info(msg)

    def run(self) -> dict:
        """执行完整注册，返回号池 payload 或抛异常。"""
        s = self.settings
        proxies = s.get("proxy_pool") or []
        proxy = pick_proxy(proxies) or ""

        mail_client = CfTempMailClient(
            s.get("mail_api_base") or "",
            auth_mode=s.get("mail_auth_mode") or "none",
            auth_key=s.get("mail_auth_key") or "",
            domain=s.get("mail_domain") or "",
            proxy=proxy or None,
            # 建箱是一次性请求，单条代理抖动就会报废整次注册；
            # 多给几条候选让邮箱客户端能失败转移。
            proxy_candidates=pick_proxy_candidates(proxies, 4),
        )
        self.mailbox = mail_client.create_mailbox(name=self.mail_name)
        self.email = self.mailbox.address
        self._progress(
            f"[注册] 邮箱就绪：{self.email}，代理="
            + (mask_proxy(proxy) if proxy else "直连（未配置可用代理）")
        )

        name = (self.display_name or "").strip() or random_display_name()
        birthday = (self.birthday or "").strip() or generate_random_birthday()
        self.registration_password = (self.password or "").strip() or generate_registration_password()

        openai_auth.set_stop_checker(self._check_stop)

        fingerprint_seed = f"register:{self.email.lower()}:{uuid.uuid4()}"
        session = BrowserSession(
            proxy=proxy,
            fingerprint_seed=fingerprint_seed,
            auto_locale_from_ip=bool(s.get("auto_locale_from_ip", True)),
        )
        create_acknowledged = False
        try:
            network_preflight(session)
            human_delay("navigate")
            if s.get("anon_bootstrap", True):
                anonymous_bootstrap(session, strict=False)
                human_delay("navigate")

            token_source = str(s.get("token_source") or "platform").strip().lower()
            platform_mode = token_source != "codex"
            code_verifier = ""

            if platform_mode:
                # platform 链路：authorize 直接声明 signup，注册完 continue_url 里
                # 就带着 platform 回调 code，无需第二次登录（因此无手机号墙）。
                code_verifier, code_challenge = platform_oauth.generate_pkce()
                platform_oauth.seed_device_cookies(session)
                authorize_url = platform_oauth.build_authorize_url(session, self.email, code_challenge)
                human_delay("api")
                authorize_final_url = platform_oauth.follow_authorize(session, authorize_url)
                human_delay("navigate")
            else:
                chatgpt_auth.get_providers(session)
                human_delay("api")
                csrf_token = chatgpt_auth.get_csrf_token(session)
                human_delay("api")
                authorize_url = chatgpt_auth.signin_openai(session, csrf_token, self.email)
                human_delay("api")
                authorize_final_url = follow_authorize(session, authorize_url)
                human_delay("navigate")
            navigate_create_account_password(session, authorize_final_url)
            human_delay("navigate")
            password_sentinel = request_password_sentinel_bundle(session)
            password_sentinel_header, password_so_header = build_sentinel_header(
                session, password_sentinel, "username_password_create",
            )
            human_delay("challenge")
            self._progress(f"[注册] 提交注册：{self.email}")
            register_result = register_user(
                session, self.email, self.registration_password,
                password_sentinel_header, password_so_header,
            )
            create_acknowledged = True

            otp_after_ts = time.time()
            navigate_email_otp_send(session, register_result.get("continue_url"))
            human_delay("navigate")

            validate_result = None
            max_otp_attempts = 3
            current_otp: str | None = None
            # OTP 等待总预算：单次上限 × 重试次数会线性放大总耗时（3×120=360s），
            # 对整体批量来说过久。这里给总时长封顶，避免个别卡死任务拖垮整批。
            otp_total_budget = float(s.get("otp_total_budget", 0) or 0)
            otp_budget_deadline = time.monotonic() + otp_total_budget if otp_total_budget > 0 else None
            for otp_attempt in range(1, max_otp_attempts + 1):
                self._check_stop()
                if current_otp is None:
                    per_wait = float(s.get("otp_max_wait", 120))
                    if otp_budget_deadline is not None:
                        left = otp_budget_deadline - time.monotonic()
                        if left <= 0:
                            raise RuntimeError(
                                f"OTP 等待已达总预算 {otp_total_budget:.0f}s，放弃本次注册：{self.email}"
                            )
                        per_wait = min(per_wait, left)
                    self._progress(
                        f"[OTP] 等待验证码：{self.email}"
                        f"（第 {otp_attempt}/{max_otp_attempts} 次，上限 {per_wait:.0f}s）"
                    )
                    current_otp = mail_client.wait_for_otp(
                        self.mailbox,
                        after_ts=otp_after_ts,
                        max_wait=per_wait,
                        poll_interval=float(s.get("otp_poll_interval", 2.0)),
                        stop_checker=self._check_stop,
                    )
                human_delay("otp_input")
                try:
                    sentinel_header_9 = so_header_9 = None
                    if s.get("send_sentinel_on_email_otp_validate", False):
                        sentinel_resp_9 = request_sentinel_token(session, "authorize_continue")
                        sentinel_header_9, so_header_9 = build_sentinel_header(session, sentinel_resp_9, "authorize_continue")
                        human_delay("challenge")
                    validate_result = validate_email_otp(session, current_otp, sentinel_header_9, so_header_9)
                    break
                except EmailOtpInvalidError as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(f"[OTP] 验证码错误/过期：{str(exc)[:180]}，重新发送")
                    otp_after_ts = time.time()
                    send_email_otp(session)
                    human_delay("api")
                    current_otp = None

            if validate_result is None:
                raise RuntimeError("OTP 验证未完成")
            human_delay("api")

            page = validate_result.get("page") if isinstance(validate_result, dict) else {}
            page = page if isinstance(page, dict) else {}
            page_type = str(page.get("type") or "")
            otp_continue_url = (
                validate_result.get("continue_url") or validate_result.get("external_url")
                or validate_result.get("url") or page.get("continue_url")
                or page.get("external_url") or page.get("url")
            )
            otp_continue_text = str(otp_continue_url or "")
            direct_oauth_after_otp = bool(
                otp_continue_text
                and "about-you" not in otp_continue_text
                and (
                    "chatgpt.com/api/auth/callback" in otp_continue_text
                    or "auth.openai.com/authorize/continue" in otp_continue_text
                    or page_type == "external_url"
                )
            )

            continue_url = ""
            chatgpt_access_token = ""
            if page_type == "external_url" or direct_oauth_after_otp:
                if not otp_continue_url:
                    raise RuntimeError(f"OTP external_url 响应缺少可跟随 URL: {validate_result}")
                self._progress(f"[注册] OTP 后直接 OAuth 回调：{self.email}")
                if platform_mode:
                    # platform 链路下 otp_continue_url 就是 platform 回调，直接留用
                    continue_url = str(otp_continue_url)
                else:
                    session_info, chatgpt_access_token = _finalize_registration_session(
                        session, otp_continue_url, self.email,
                        callback_referer="https://auth.openai.com/email-verification",
                    )
            else:
                if page_type and page_type not in ("about_you", "about-you"):
                    if otp_continue_url and "about-you" not in str(otp_continue_url):
                        raise RuntimeError(f"OTP 后续页面类型未知: page_type={page_type}, resp={validate_result}")
                about_url = str(otp_continue_url) if otp_continue_url and "about-you" in str(otp_continue_url) else None
                navigate_about_you(session, about_url)
                human_delay("navigate")
                sentinel_resp_11 = request_sentinel_token(session, "oauth_create_account")
                sentinel_header_11, so_header_11 = build_sentinel_header(session, sentinel_resp_11, "oauth_create_account")
                human_delay("challenge")
                human_delay("form")
                create_result = create_account(session, name, birthday, sentinel_header_11, so_header_11)
                create_acknowledged = True
                self._progress(f"[注册] create_account 通过：{self.email}")
                human_delay("post_auth")
                continue_url = str(create_result.get("continue_url") or "")
                if not continue_url:
                    raise RuntimeError(f"create_account 响应缺少 continue_url: {create_result}")
                if not platform_mode:
                    session_info, chatgpt_access_token = _finalize_registration_session(session, continue_url, self.email)

            if platform_mode:
                # create_account / OTP 的 continue_url 里就带着 platform 回调 code。
                # 取到即可换 token，不需要第二次登录，因此不存在手机号验证。
                platform_code = platform_oauth.extract_code(continue_url)
                if not platform_code:
                    platform_code = platform_oauth.extract_code(str(otp_continue_url or ""))
                if not platform_code:
                    # 万一响应没直接带 code，跟随一次 continue_url 看跳转里是否有
                    if continue_url.startswith("https://"):
                        try:
                            landed = platform_oauth.follow_authorize(session, continue_url)
                            platform_code = platform_oauth.extract_code(landed)
                        except Exception:
                            logger.warning("[Platform] 跟随 continue_url 取 code 失败", exc_info=True)
                if not platform_code:
                    raise RuntimeError(
                        f"[Platform] 注册流程结束仍未取到 authorization code：{self.email}"
                    )

                token_resp = platform_oauth.exchange_token(session, platform_code, code_verifier)
                id_claims = _parse_id_token(str(token_resp.get("id_token") or ""))
                effective_email = id_claims.get("email") or self.email
                self._progress(f"[注册] Platform token 到手：{effective_email}")

                return {
                    "access_token": str(token_resp.get("access_token") or "").strip(),
                    "refresh_token": str(token_resp.get("refresh_token") or "").strip(),
                    "id_token": str(token_resp.get("id_token") or "").strip(),
                    "account_id": id_claims.get("account_id", ""),
                    "email": effective_email,
                    "plan_type": id_claims.get("plan_type", ""),
                    "source_type": "web",
                    "registration_password": self.registration_password,
                    "proxy": proxy or "",
                    "timezone_iana": session.browser_profile.get("timezone_iana", ""),
                    "timezone_name": session.browser_profile.get("timezone_name", ""),
                }

            self._progress(f"[注册] 已拿到 ChatGPT accessToken：{self.email}，开始 Codex 授权")

            codex = run_codex_oauth(
                self.email,
                password=self.registration_password,
                otp_provider=lambda em, after_ts: mail_client.wait_for_otp(
                    self.mailbox,
                    after_ts=after_ts,
                    max_wait=float(s.get("otp_max_wait", 120)),
                    poll_interval=float(s.get("otp_poll_interval", 2.0)),
                ),
                proxy=proxy,
            )
            self._progress(f"[注册] Codex token 到手：{codex.get('email')}")

            return {
                "access_token": codex["access_token"],
                "refresh_token": codex["refresh_token"],
                "id_token": codex["id_token"],
                "account_id": codex.get("account_id", ""),
                "email": codex.get("email") or self.email,
                "plan_type": codex.get("plan_type", ""),
                "source_type": "codex",
                "chatgpt_access_token": chatgpt_access_token,
                "registration_password": self.registration_password,
                "proxy": proxy or "",
                "timezone_iana": session.browser_profile.get("timezone_iana", ""),
                "timezone_name": session.browser_profile.get("timezone_name", ""),
            }
        except RegisterStopRequested:
            raise
        except AccountUnusableError:
            raise
        except Exception as exc:
            logger.error(f"[注册] 失败：{self.email}: {type(exc).__name__}: {exc}")
            logger.debug(traceback.format_exc())
            raise
        finally:
            session.close()
            openai_auth.set_stop_checker(None)


# ============================================================
# 批量任务管理（内存态，供 API 层调用）
# ============================================================

class BatchJob:
    def __init__(self, job_id: str, total: int, source: str = "manual") -> None:
        self.job_id = job_id
        self.source = source
        self.total = total
        self.issued = 0
        self.done = 0
        self.success = 0
        self.failed = 0
        self.current_email = ""
        self.message = "排队中"
        self.finished = False
        self.error: str | None = None
        # 因日限额而提前收尾（用于前端提示，避免"少注册了几个"看起来像 bug）
        self.daily_blocked = False
        self.stop_event = threading.Event()
        self.results: list[dict] = []
        # RLock：add_log 的 Condition 复用这把锁，嵌套调用不会自锁死。
        self.lock = threading.RLock()
        # 实时日志环形缓冲：SSE 客户端接入时先补齐历史，再跟增量。
        self.logs: list[dict] = []
        self._log_seq = 0
        self._log_cond = threading.Condition(self.lock)
        self._LOG_LIMIT = 800

    def add_log(self, level: str, message: str, email: str = "") -> None:
        with self._log_cond:
            self._log_seq += 1
            self.logs.append({
                "seq": self._log_seq,
                "ts": time.time(),
                "level": level,
                "message": message,
                # 只回退到真实邮箱，绝不用 "第 N/M 个" 这类占位符冒充邮箱。
                "email": email or (self.current_email if "@" in self.current_email else ""),
            })
            if len(self.logs) > self._LOG_LIMIT:
                del self.logs[: len(self.logs) - self._LOG_LIMIT]
            self._log_cond.notify_all()

    def logs_since(self, seq: int) -> tuple[list[dict], bool]:
        """返回 (seq 之后的日志, 任务是否已结束)。"""
        with self.lock:
            return [item for item in self.logs if item["seq"] > seq], self.finished

    def wait_for_logs(self, seq: int, timeout: float = 15.0) -> tuple[list[dict], bool]:
        """阻塞等待新日志（或超时），用于 SSE 推送。"""
        with self._log_cond:
            if not any(item["seq"] > seq for item in self.logs) and not self.finished:
                self._log_cond.wait(timeout)
            return [item for item in self.logs if item["seq"] > seq], self.finished

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "job_id": self.job_id,
                "source": self.source,
                "total": self.total,
                "issued": self.issued,
                "done": self.done,
                "success": self.success,
                "failed": self.failed,
                "current_email": self.current_email,
                "message": self.message,
                "finished": self.finished,
                "error": self.error,
                "daily_blocked": self.daily_blocked,
                "results": list(self.results),
                "last_seq": self._log_seq,
            }


class RegisterManager:
    """注册批量任务管理器（单例，内存态）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, BatchJob] = {}
        self._lock = threading.Lock()
        # 每日计数按"实际尝试注册的账号数"累计（而不是任务提交时一次性加满），
        # 并且额外记录成功数用于展示。持久化到 config.json，避免重启绕过日限额。
        # 旧格式（date -> int）在读取时会自动兼容为尝试数。
        self._daily_counts: dict[str, int] = {}
        self._daily_success: dict[str, int] = {}
        self._daily_lock = threading.Lock()
        self._load_daily_counts()

    def get_job(self, job_id: str) -> BatchJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> BatchJob | None:
        with self._lock:
            job_id = next(reversed(self._jobs), None)
            return self._jobs[job_id] if job_id is not None else None

    def stop_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job:
            return False
        with job.lock:
            job.stop_event.set()
        return True

    def stop_all(self) -> None:
        """Request cancellation of every active batch during app shutdown."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if not job.finished:
                    job.stop_event.set()

    def inflight_count(self) -> int:
        """当前所有未完成任务里"已发起但未结束"的注册数，用于水位守护去重。"""
        total = 0
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if job.finished:
                    continue
                total += max(0, job.issued - job.done)
        return total

    def prune_finished_jobs(self, keep: int = 20) -> None:
        """清理已结束的旧任务，避免内存态无限增长。"""
        with self._lock:
            finished = [job for job in self._jobs.values() if job.finished]
            if len(finished) <= keep:
                return
            for job in finished[: len(finished) - keep]:
                self._jobs.pop(job.job_id, None)

    def _daily_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_daily_counts(self) -> None:
        try:
            from services.register_config import load_daily_counts

            counts, success = load_daily_counts()
            self._daily_counts = counts
            self._daily_success = success
        except Exception:
            logger.debug("[注册] 读取每日计数失败，按 0 起算", exc_info=True)

    def _persist_daily_counts(self) -> None:
        try:
            from services.register_config import save_daily_counts

            save_daily_counts(dict(self._daily_counts), success=dict(self._daily_success))
        except Exception:
            logger.warning("[注册] 写入每日计数失败，日限额本次可能不精确", exc_info=True)

    def daily_used(self) -> int:
        """今日已发起的注册数（用于日限额判断）。"""
        with self._daily_lock:
            return self._daily_counts.get(self._daily_key(), 0)

    def daily_success(self) -> int:
        """今日实际注册成功数（用于展示；旧数据无此项时回退为 0）。"""
        with self._daily_lock:
            return self._daily_success.get(self._daily_key(), 0)

    def daily_remaining(self, limit: int) -> int:
        """今日剩余可发起额度；limit<=0 表示不限制。"""
        if int(limit or 0) <= 0:
            return -1
        return max(0, int(limit) - self.daily_used())

    def try_reserve_attempt(self, limit: int) -> bool:
        """原子占用一个今日额度（limit<=0 表示不限），返回是否占用成功。

        必须"检查+自增"在一次加锁内完成：并发>1 时若先各自判断再各自自增，
        会在上限边界同时通过检查而超发。
        """
        with self._daily_lock:
            key = self._daily_key()
            used = self._daily_counts.get(key, 0)
            if int(limit or 0) > 0 and used >= int(limit):
                return False
            self._daily_counts[key] = used + 1
            self._persist_daily_counts()
            return True

    def _bump_daily(self, *, attempt: int = 0, success: int = 0) -> None:
        """按"实际发生的次数"累加，而不是任务提交时一次性加满。

        提交时预扣会把"失败/未执行"的次数也算进今日额度，导致明明一个都没成功
        却显示已用掉额度，甚至把日限额耗光。
        """
        if not attempt and not success:
            return
        with self._daily_lock:
            key = self._daily_key()
            if attempt:
                self._daily_counts[key] = self._daily_counts.get(key, 0) + attempt
            if success:
                self._daily_success[key] = self._daily_success.get(key, 0) + success
            self._persist_daily_counts()

    def start_batch(
        self,
        *,
        count: int,
        settings: dict,
        concurrency: int = 1,
        source: str = "manual",
        password: str | None = None,
        on_account: Callable[[dict], dict] | None = None,
        on_round: Callable[[bool], None] | None = None,
    ) -> BatchJob:
        job = BatchJob(str(uuid.uuid4()), total=count, source=source)
        with self._lock:
            self._jobs[job.job_id] = job
        # 注意：这里不再记账。每日用量在真正发起一次注册尝试时累加，
        # 否则提交即计入会让失败的任务也吃掉日限额。

        def progress(msg: str) -> None:
            with job.lock:
                job.message = msg
            job.add_log("info", msg)

        def worker_loop(worker_idx: int) -> None:
            while True:
                with job.lock:
                    if job.stop_event.is_set():
                        return
                    if job.issued >= job.total:
                        return
                    # Reserve and issue under the same job lock as cancellation.
                    limit = int(settings.get("keeper_daily_limit") or 0)
                    if not self.try_reserve_attempt(limit):
                        if not job.daily_blocked:
                            job.daily_blocked = True
                            remaining = max(0, job.total - job.issued)
                            job.add_log(
                                "warn",
                                f"已达每日注册上限 {limit}，剩余 {remaining} 个不再发起",
                            )
                        return
                    job.issued += 1
                    idx = job.issued
                ok = False
                err_msg = ""
                email = ""
                task: RegisterTask | None = None
                step = 0

                def task_progress(msg: str, _idx=idx) -> None:
                    nonlocal step
                    step += 1
                    job.add_log("step", f"[{_idx}/{job.total}] {msg}",
                                email=task.email if task else "")

                try:
                    job.add_log("info", f"[{idx}/{job.total}] 开始注册")
                    task = RegisterTask(
                        settings=settings,
                        password=password,
                        stop_event=job.stop_event,
                        progress_cb=task_progress,
                    )
                    with job.lock:
                        job.current_email = f"第 {idx}/{job.total} 个"
                    payload = task.run()
                    email = payload.get("email", "")
                    job.add_log("success", f"[{idx}/{job.total}] 注册成功：{email}")
                    self._bump_daily(success=1)
                    with job.lock:
                        job.current_email = email
                    added = None
                    if on_account is not None:
                        added = on_account(payload)
                    ok = True
                    with job.lock:
                        job.results.append({
                            "email": email,
                            "ok": True,
                            "added": bool((added or {}).get("added", 0) > 0),
                            "skipped": bool((added or {}).get("skipped", 0) > 0),
                        })
                except RegisterStopRequested:
                    err_msg = "已停止"
                    job.add_log("warn", f"[{idx}/{job.total}] 已停止")
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {str(exc)[:240]}"
                    email = task.email if task else ""
                    job.add_log("error", f"[{idx}/{job.total}] 失败：{err_msg}", email=email)
                with job.lock:
                    job.done += 1
                    if ok:
                        job.success += 1
                    else:
                        job.failed += 1
                        job.results.append({"email": email, "ok": False, "error": err_msg})
                if on_round is not None:
                    try:
                        on_round(ok)
                    except Exception:
                        logger.debug("[注册] on_round 回调异常", exc_info=True)
                if job.stop_event.is_set():
                    return
                time.sleep(2.0)

        def supervisor() -> None:
            threads = []
            for i in range(max(1, min(int(concurrency), 8))):
                t = threading.Thread(target=worker_loop, args=(i,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            with job.lock:
                job.finished = True
                job.message = "已完成" if not job.stop_event.is_set() else "已手动停止"
                job.current_email = ""
            job.add_log(
                "info",
                f"任务结束：成功 {job.success} / 失败 {job.failed} / 共 {job.total}",
            )

        threading.Thread(target=supervisor, daemon=True).start()
        return job


register_manager = RegisterManager()
