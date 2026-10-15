# -*- coding: utf-8 -*-
"""OpenAI Auth 模块（步骤4-12，移植自 turb-gpt-free-register core/openai_auth.py）。

处理 auth.openai.com 注册请求与 sentinel.openai.com 的 sentinel token 请求。
停止信号由 service 层通过 `set_stop_checker` 注入，避免反向依赖。
"""
import json
import logging
import random
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar

from .constants import SENTINEL_SV
from .sentinel import build_sentinel_request_body, generate_requirements_token
from .sentinel_runner import generate_sentinel_token
from .session import BrowserSession

logger = logging.getLogger(__name__)

PROXY_RETRY_MAX_ATTEMPTS = 3
PROXY_RETRY_DELAY = 1.0
PREFLIGHT_TIMEOUT = 12.0

# 用 ContextVar 而非模块全局：并发注册时每个线程有独立的停止检查，
# 先结束的任务不会清掉其他在跑任务的 checker。
_stop_checker: ContextVar[Callable[[], None] | None] = ContextVar("register_stop_checker", default=None)


def set_stop_checker(checker: Callable[[], None] | None) -> None:
    """注入停止检查回调（抛异常即中断注册）。仅作用于当前上下文/线程。"""
    _stop_checker.set(checker)


def _check_stop_requested() -> None:
    checker = _stop_checker.get()
    if checker is not None:
        checker()


class EmailOtpInvalidError(RuntimeError):
    """邮箱验证码无效/过期，可重新发送后重试。"""


class AccountUnusableError(Exception):
    """邮箱对应的 OpenAI 账号已废（删除/停用/封禁）。"""

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


_ACCOUNT_DEAD_CODES = frozenset({"account_deactivated", "account_deleted", "account_banned"})

_ACCOUNT_DEAD_TEXT_MARKERS = (
    "account_deactivated", "account_deleted", "account_banned",
    "account deactivated", "account deleted", "account banned",
    "account has been deactivated", "account has been deleted",
    "account was deactivated", "account was deleted",
    "your account has been deactivated", "your account has been deleted",
    "your account was deactivated", "your account was deleted",
    "账号已停用", "账号已禁用", "账号已删除",
    "账户已停用", "账户已禁用", "账户已删除",
)


def detect_account_unusable_text(text: str) -> str:
    low = str(text or "").lower()
    for code in _ACCOUNT_DEAD_CODES:
        if code in low:
            return code
    if any(marker in low for marker in _ACCOUNT_DEAD_TEXT_MARKERS):
        if "delete" in low or "删除" in low:
            return "account_deleted"
        if "ban" in low or "封" in low:
            return "account_banned"
        return "account_deactivated"
    return ""


def detect_account_unusable_response_body(body: str) -> str:
    try:
        payload = json.loads(body or "")
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    code = ""
    if isinstance(err, dict):
        code = str(err.get("code") or "")
    elif isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error_code") or "")
    return code if code in _ACCOUNT_DEAD_CODES else ""


def _extract_error_code(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return ""


def _is_transient_network_error(exc: Exception) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    transient_classes = ("SSLError", "ConnectionError", "Timeout", "CurlError", "ProxyError")
    if any(t.lower() in name.lower() for t in transient_classes):
        return True
    transient_keywords = (
        "wrong_version_number", "tls connect", "ssl", "connection reset",
        "connection refused", "timed out", "proxy",
        "curl: (97)", "curl: (35)", "curl: (52)", "curl: (56)",
    )
    return any(k in msg for k in transient_keywords)


def _is_retryable_authorize_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in (403, 408, 425, 429) or status >= 500:
        return True
    text = str(exc or "").lower()
    return _is_transient_network_error(exc) or any(
        marker in text for marker in (
            "http 403", "http error 403", "status=403",
            "http 429", "http error 429", "status=429", "熔断冷却",
        )
    )


def _exception_http_status(exc: Exception) -> int:
    try:
        return int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _reset_retryable_circuit(session: BrowserSession) -> None:
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
    else:
        session.blocked_until = 0.0
        session.blocked_reason = ""


def _interruptible_sleep(seconds: float, interval: float = 0.25) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_stop_requested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(max(0.01, interval), remaining))


def _request_with_proxy_retry(session: BrowserSession, label: str, fn):
    """对代理/TLS/超时及可恢复 HTTP 错误进行有限指数退避重试。"""
    max_attempts = PROXY_RETRY_MAX_ATTEMPTS
    retry_delay = PROXY_RETRY_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _check_stop_requested()
        try:
            response = fn()
            response.raise_for_status()
            if attempt > 1:
                logger.info("[%s] 代理链路重试成功 (%s/%s)", label, attempt, max_attempts)
            return response
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_authorize_error(exc) or attempt >= max_attempts:
                raise
            _reset_retryable_circuit(session)
            backoff = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "[%s] 代理链路临时失败 (%s/%s): %s: %s，保留当前会话，%.1fs 后重试",
                label, attempt, max_attempts, type(exc).__name__, str(exc)[:180], backoff,
            )
            _interruptible_sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"{label} 重试耗尽但无异常记录")


def network_preflight(session: BrowserSession) -> None:
    """注册前网络预检：只建立边缘节点/cookie/基础连通性，不携带邮箱、不触发 OTP。"""
    resp = _request_with_proxy_retry(
        session,
        "预检:chatgpt-auth-login",
        lambda: session.get(
            "https://chatgpt.com/auth/login",
            headers=session.get_chatgpt_navigate_headers(referer=""),
            allow_redirects=True,
            timeout=PREFLIGHT_TIMEOUT,
        ),
    )
    observe = getattr(session, "observe_chatgpt_document", None)
    if callable(observe):
        observe(resp)


def follow_authorize(session: BrowserSession, authorize_url: str) -> str:
    """步骤4: 跟随 authorize URL 重定向，建立 auth.openai.com session cookies。"""
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    max_attempts = PROXY_RETRY_MAX_ATTEMPTS
    retry_delay = PROXY_RETRY_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"[步骤4] 跟随 authorize URL (尝试 {attempt}/{max_attempts})...")
            resp = session.get(authorize_url, headers=headers, allow_redirects=True)
            resp.raise_for_status()
            final_url = str(getattr(resp, "url", "") or "")
            session.rotate_document_navigation_id()
            logger.info(f"[步骤4] 重定向完成, 最终URL: {final_url}")
            return final_url
        except Exception as exc:
            last_exc = exc
            if _exception_http_status(exc) == 403:
                if attempt >= max_attempts:
                    logger.warning("[步骤4] authorize HTTP 403，已达最大重试次数")
                    raise
                _reset_retryable_circuit(session)
                backoff = retry_delay * (2 ** (attempt - 1))
                logger.warning("[步骤4] authorize HTTP 403，%.1fs 后重试 (%s/%s)", backoff, attempt, max_attempts)
                time.sleep(backoff)
                continue
            if not _is_retryable_authorize_error(exc):
                raise
            if attempt >= max_attempts:
                break
            _reset_retryable_circuit(session)
            backoff = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                f"[步骤4] authorize 临时失败 ({type(exc).__name__}: {str(exc)[:120]})，{backoff:.1f}s 后重试..."
            )
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError("步骤4 重试耗尽但无异常记录")


def _ensure_sentinel_frame(session: BrowserSession, context_name: str, *, user_initiated: bool) -> None:
    ready_contexts = getattr(session, "_sentinel_frame_contexts", None)
    if not isinstance(ready_contexts, set):
        ready_contexts = set()
        session._sentinel_frame_contexts = ready_contexts
    if context_name in ready_contexts:
        return
    frame_url = f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}"
    _request_with_proxy_retry(
        session,
        f"Sentinel iframe:{context_name}",
        lambda: session.get(
            frame_url,
            headers=session.get_sentinel_frame_headers(user_initiated=user_initiated),
            allow_redirects=True,
        ),
    )
    ready_contexts.add(context_name)


def _sentinel_profile(session: BrowserSession) -> dict:
    profile = dict(getattr(session, "browser_profile", None) or {})
    profile["build_id"] = None
    profile["script_src_samples"] = [f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js"]
    return profile


def request_sentinel_token(session: BrowserSession, flow: str) -> dict:
    """步骤6/9/11: POST sentinel.openai.com/backend-api/sentinel/req。"""
    iframe_flow = flow == "username_password_create"
    context_name = "password" if iframe_flow else "top_level"
    _ensure_sentinel_frame(session, context_name, user_initiated=iframe_flow)

    sentinel_sid = getattr(session, "sentinel_iframe_sid" if iframe_flow else "sentinel_sid", session.device_id)
    profile = _sentinel_profile(session)

    context_p = getattr(session, "_sentinel_context_p", None)
    if not isinstance(context_p, dict):
        context_p = {}
        session._sentinel_context_p = context_p
    p = context_p.get(context_name)
    if not p:
        p = generate_requirements_token(sentinel_sid, profile=profile)
        context_p[context_name] = p

    body = build_sentinel_request_body(p, session.device_id, flow)
    logger.info(f"[Sentinel] 请求 sentinel token, flow={flow}")
    resp = _request_with_proxy_retry(
        session,
        f"Sentinel token:{flow}",
        lambda: session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers=session.get_sentinel_headers(),
            data=body,
        ),
    )

    data = resp.json()
    if isinstance(data, dict):
        data = dict(data)
        data["_request_p"] = p
    if data.get("proofofwork", {}).get("required"):
        logger.info(f"[Sentinel] 需要 PoW: seed={data['proofofwork']['seed']}, difficulty={data['proofofwork']['difficulty']}")
    requires = []
    if data.get("turnstile", {}).get("required"):
        requires.append("turnstile")
    if data.get("so", {}).get("required"):
        requires.append("so")
    if data.get("proofofwork", {}).get("required"):
        requires.append("pow")
    logger.info(f"[Sentinel] 服务端要求项: {requires or '无'}")
    return data


def request_password_sentinel_bundle(session: BrowserSession) -> dict:
    """密码页 iframe 首次加载的三连 req（同一 p 同一 SID），返回 password flow 的 challenge。"""
    _ensure_sentinel_frame(session, "password", user_initiated=True)

    profile = _sentinel_profile(session)
    sid = getattr(session, "sentinel_iframe_sid", session.device_id)
    p = generate_requirements_token(sid, profile=profile)

    context_p = getattr(session, "_sentinel_context_p", None)
    if not isinstance(context_p, dict):
        context_p = {}
        session._sentinel_context_p = context_p
    context_p["password"] = p

    responses: dict[str, dict] = {}
    for flow in ("email_otp_validate", "username_password_create", "authorize_continue"):
        body = build_sentinel_request_body(p, session.device_id, flow)
        resp = _request_with_proxy_retry(
            session,
            f"Sentinel password bundle:{flow}",
            lambda body=body: session.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                headers=session.get_sentinel_headers(),
                data=body,
            ),
        )
        response_data = resp.json()
        if isinstance(response_data, dict):
            response_data = dict(response_data)
            response_data["_request_p"] = p
        responses[flow] = response_data
    logger.info("[Sentinel] 密码页 flow bundle 初始化完成")
    return responses["username_password_create"]


def build_sentinel_header(session: BrowserSession, sentinel_resp: dict, flow: str) -> tuple:
    """challenge 喂给 Node runner 跑真实 sdk.js，产出 (sentinel-token, so-token)。"""
    iframe_flow = flow == "username_password_create"
    sentinel_sid = getattr(session, "sentinel_iframe_sid" if iframe_flow else "sentinel_sid", None)
    header_value = generate_sentinel_token(
        challenge=sentinel_resp,
        flow=flow,
        device_id=session.device_id,
        user_agent=(getattr(session, "browser_profile", {}) or {}).get("user_agent"),
        browser_profile=getattr(session, "browser_profile", None),
        sentinel_sid=sentinel_sid,
        react_listening_key=getattr(session, "react_listening_key", None),
        react_container_key=getattr(session, "react_container_key", None),
        react_resources_key=getattr(session, "react_resources_key", None),
        cookie=session.auth_cookie_header(),
    )

    so_header = None
    try:
        parsed = json.loads(header_value)
        so_value = parsed.pop("_so", None) or parsed.pop("so", None)
        header_value = json.dumps(parsed, separators=(",", ":"))
        if so_value:
            so_header = json.dumps(
                {"so": so_value, "c": parsed.get("c", sentinel_resp.get("token", "")), "id": session.device_id, "flow": flow},
                separators=(",", ":"),
            )
            logger.info("[Sentinel] 检测到 SO 字段，已构建 so-token 头")
    except (ValueError, TypeError) as exc:
        logger.warning(f"[Sentinel] runner 输出解析失败: {exc}")

    return header_value, so_header


def generate_registration_password(length: int = 14) -> str:
    length = max(8, min(int(length), 64))
    groups = ("ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghjkmnpqrstuvwxyz", "23456789", "!@#$%^&*?_-+=")
    chars = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def navigate_create_account_password(session: BrowserSession, current_url: str | None = None) -> str:
    """步骤5: 进入注册密码页。"""
    current = str(current_url or "")
    if "/create-account/password" in current:
        return current
    url = "https://auth.openai.com/create-account/password"
    referer = current if current.startswith("https://auth.openai.com/") else "https://auth.openai.com/"
    headers = session.get_auth_navigate_headers(referer=referer)
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    logger.info("[步骤5] 切换到创建账号密码页...")
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    session.rotate_document_navigation_id()
    final_url = str(getattr(resp, "url", "") or url)
    if "/create-account/password" not in final_url:
        raise RuntimeError(f"无法进入注册密码页，最终落点: {final_url}")
    logger.info("[步骤5] 已进入创建账号密码页")
    return final_url


def register_user(
    session: BrowserSession,
    email: str,
    password: str,
    sentinel_header: str,
    so_header: str | None = None,
) -> dict:
    """步骤7: 提交邮箱和密码。"""
    url = "https://auth.openai.com/api/accounts/user/register"
    headers = session.get_auth_headers(referer="https://auth.openai.com/create-account/password")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    body = json.dumps({"password": password, "username": email}, separators=(",", ":"))
    logger.info("[步骤7] 提交邮箱和密码：%s", email)
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error("[步骤7] user/register 失败 status=%s body=%s", resp.status_code, (resp.text or "")[:500])
        resp.raise_for_status()
    return resp.json()


def navigate_email_otp_send(session: BrowserSession, continue_url: str | None = None) -> str:
    """步骤8: 跟随 user/register 返回地址发送 OTP。"""
    url = str(continue_url or "https://auth.openai.com/api/accounts/email-otp/send")
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/create-account/password")
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    resp = session.get(url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    session.rotate_document_navigation_id()
    final_url = str(getattr(resp, "url", "") or "")
    if "/email-verification" not in final_url:
        raise RuntimeError(f"OTP 发送导航落点异常: {final_url}")
    return final_url


def navigate_about_you(session: BrowserSession, about_url: str | None = None) -> str:
    """进入 about-you 页面状态。"""
    url = str(about_url or "https://auth.openai.com/about-you")
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    headers["sec-fetch-site"] = "same-origin"
    logger.info("[步骤10.5] 导航到 about-you 页面")
    resp = session.get(url, headers=headers, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"about-you 导航失败 status={resp.status_code}: {(resp.text or '')[:240]}")
    final_url = str(getattr(resp, "url", "") or url)
    if "/api/accounts/user/register" in final_url or "/create-account/password" in final_url:
        raise RuntimeError(f"about-you 导航落入旧密码注册路径: {final_url}")
    return final_url


def send_email_otp(session: BrowserSession, referer: str = "https://auth.openai.com/email-verification") -> None:
    """重新发送邮箱验证码。"""
    headers = session.get_auth_navigate_headers(referer=referer)
    headers["sec-fetch-site"] = "same-origin"
    headers["sec-fetch-user"] = "?1"
    logger.info("[OTP] 请求重新发送邮箱验证码...")
    resp = session.get(
        "https://auth.openai.com/api/accounts/email-otp/send",
        headers=headers, allow_redirects=True,
    )
    if resp.status_code >= 400:
        resp.raise_for_status()
    session.rotate_document_navigation_id()


def validate_email_otp(
    session: BrowserSession,
    code: str,
    sentinel_header: str | None = None,
    so_header: str | None = None,
) -> dict:
    """步骤10: 提交邮箱验证码验证。"""
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header

    logger.info(f"[步骤10] 提交邮箱验证码: {code}")
    resp = session.post(url, headers=headers, data=json.dumps({"code": code}))

    if resp.status_code != 200:
        logger.error(f"[步骤10] 失败 status={resp.status_code}: {(resp.text or '')[:300]}")
        err_code = _extract_error_code(resp)
        if err_code in _ACCOUNT_DEAD_CODES:
            raise AccountUnusableError(f"账号已废弃（{err_code}），邮箱不可再用", error_code=err_code)
        low = (resp.text or "").lower()
        if resp.status_code in (400, 401, 422) and any(k in low for k in (
            "invalid", "incorrect", "expired", "code", "otp", "verification",
            "验证码", "認証コード", "確認コード", "コード",
        )):
            raise EmailOtpInvalidError(f"邮箱验证码无效或已过期: status={resp.status_code}")
        resp.raise_for_status()

    data = resp.json()
    logger.info(f"[步骤10] 验证成功: {data.get('page', {}).get('type')}")
    return data


def create_account(
    session: BrowserSession,
    name: str,
    birthday: str,
    sentinel_header: str,
    so_header: str | None = None,
) -> dict:
    """步骤12: 提交用户信息，完成注册。"""
    url = "https://auth.openai.com/api/accounts/create_account"
    headers = session.get_auth_headers(referer="https://auth.openai.com/about-you")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    body = json.dumps({"name": name, "birthdate": birthday})
    logger.info(f"[步骤12] 提交用户信息, 名称: {name}, 生日: {birthday}")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[步骤12] 失败 status={resp.status_code}: {(resp.text or '')[:300]}")
        resp.raise_for_status()
    logger.info("[步骤12] 创建账号成功")
    return resp.json()
