# -*- coding: utf-8 -*-
"""cloudflare_temp_email 邮箱客户端（自部署 Worker，dreamhunter2333/cloudflare_temp_email）。

流程：POST /api/new_address（或 /admin/new_address）→ {address, jwt}
轮询 GET /api/mails?limit=&offset=（Bearer jwt）→ 抽取 OpenAI OTP。
"""
from __future__ import annotations

import logging
import random
import string
import threading
import time
from dataclasses import dataclass
from typing import Callable

from curl_cffi import requests as curl_requests

from .otp import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

# 邮箱轮询期间连续连接失败多少次才放弃（每轮间隔 otp_poll_interval 秒）。
# 代理链路上的瞬时 TLS/超时抖动很常见，偶尔几次不该判定整次注册失败。
_MAIL_POLL_MAX_ERRORS = 8

# 等待切片：把 poll_interval 拆成 0.25s 的小段，使「停止任务」能在 0.25s 内响应
_SLEEP_SLICE = 0.25


def _interruptible_sleep(seconds: float, stop_checker: Callable[[], None] | None) -> None:
    """分片睡眠，期间持续检查停止信号，避免长时间阻塞。"""
    if stop_checker is None:
        time.sleep(seconds)
        return
    remaining = float(seconds)
    while remaining > 0:
        stop_checker()
        chunk = _SLEEP_SLICE if remaining > _SLEEP_SLICE else remaining
        time.sleep(chunk)
        remaining -= chunk


class MailProviderError(RuntimeError):
    pass


@dataclass
class TempMailbox:
    address: str
    jwt: str


def _random_local_name(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


class DomainRotator:
    """域名轮询器：每轮把池洗牌后依次用完，用完再重洗。

    纯 random.choice 在样本少时会出现连续撞同一域名；洗牌轮询保证
    "每个域名用完一轮才会再次出现"，分布更均匀，降低单域名集中建箱被风控的概率。
    线程安全：注册是多线程的。
    """

    def __init__(self, pool: list[str]) -> None:
        self._pool = list(pool)
        self._queue: list[str] = []
        self._last: str = ""
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._pool)

    def __bool__(self) -> bool:
        return bool(self._pool)

    @property
    def pool(self) -> list[str]:
        return list(self._pool)

    def next(self) -> str:
        """取下一个域名；池为空返回空串（交给 Worker 默认域名）。"""
        if not self._pool:
            return ""
        if len(self._pool) == 1:
            return self._pool[0]
        with self._lock:
            if not self._queue:
                queue = list(self._pool)
                random.shuffle(queue)
                # 避免轮次边界与上轮末尾撞同一域名（否则最大连续会出现 2 次）。
                # next() 从末尾弹出，所以首个取出的是 queue[-1]。
                if queue[-1] == self._last:
                    for idx in range(len(queue) - 1):
                        if queue[idx] != self._last:
                            queue[-1], queue[idx] = queue[idx], queue[-1]
                            break
                self._queue = queue
            picked = self._queue.pop()
            self._last = picked
            return picked


def parse_domain_pool(raw: object) -> list[str]:
    """解析邮箱域名池：支持换行/逗号/分号/空格分隔，去重且保持顺序。"""
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        text = str(raw or "")
        for sep in ("\r\n", "\n", ";", ",", " "):
            text = text.replace(sep, "\n")
        items = text.split("\n")
    pool: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = str(item or "").strip().lstrip("@").lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        pool.append(domain)
    return pool


class CfTempMailClient:
    """cloudflare_temp_email API 客户端。

    api_base: Worker 站点地址，如 https://mail.example.com
    auth_mode: none / x-admin-auth / x-api-key
    auth_key: 对应模式的密钥（admin 鉴权码或 api key）
    domain: 指定邮箱域名，支持多个（换行/逗号/分号分隔），每次建箱按洗牌轮询取一个；
            留空则用 Worker 默认域名。
    """

    def __init__(
        self,
        api_base: str,
        *,
        auth_mode: str = "none",
        auth_key: str = "",
        domain: str = "",
        timeout: float = 30.0,
        proxy: str | None = None,
        proxy_candidates: list[str] | None = None,
    ) -> None:
        self.api_base = str(api_base or "").strip().rstrip("/")
        if not self.api_base:
            raise MailProviderError("cloudflare_temp_email 的 API 地址未配置")
        self.auth_mode = str(auth_mode or "none").strip().lower()
        self.auth_key = str(auth_key or "").strip()
        self.domain_pool = parse_domain_pool(domain)
        self._rotator = DomainRotator(self.domain_pool)
        self.timeout = timeout
        self.proxy = proxy or None
        # 代理失败转移候选：商业代理池单条线路常有瞬时 TLS 抖动，
        # 建箱这种"一次性"请求失败会直接让整次注册报废。
        # 邮箱 JWT 与代理无关，换代理重试是安全的。
        candidates: list[str] = []
        for item in [self.proxy, *(proxy_candidates or [])]:
            value = str(item or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        self._proxy_candidates = candidates

    @property
    def domain(self) -> str:
        """兼容旧调用方：返回池中第一个域名（无则空串）。"""
        return self.domain_pool[0] if self.domain_pool else ""

    def pick_domain(self) -> str:
        """按洗牌轮询取一个可用域名；池为空时返回空串（交给 Worker 默认域名）。"""
        return self._rotator.next()

    def _session(self, proxy: str | None = None) -> curl_requests.Session:
        kwargs: dict = {"timeout": self.timeout, "verify": False}
        chosen = self.proxy if proxy is None else proxy
        if chosen:
            kwargs["proxy"] = chosen
        return curl_requests.Session(**kwargs)

    def _request_with_failover(self, build):
        """依次用候选代理执行请求，返回 (响应, 使用的代理)。

        网络层异常（TLS/超时/连接重置）才换下一条；HTTP 状态码异常属于业务
        错误，换代理也没用，直接抛出交给调用方判断。
        """
        candidates = self._proxy_candidates or [None]
        last_exc: Exception | None = None
        for index, px in enumerate(candidates):
            try:
                with self._session(px) as s:
                    resp = build(s)
                if px != self.proxy:
                    logger.info(f"[邮箱] 代理失败转移成功（第 {index + 1} 条候选）")
                    self.proxy = px
                return resp
            except Exception as exc:
                last_exc = exc
                if index >= len(candidates) - 1:
                    break
                logger.warning(
                    "[邮箱] 代理异常，切换下一条候选（%s/%s）：%s: %s",
                    index + 1, len(candidates), type(exc).__name__, str(exc)[:120],
                )
        raise last_exc if last_exc else MailProviderError("邮箱请求失败且无可用代理")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_mode == "x-admin-auth" and self.auth_key:
            headers["x-admin-auth"] = self.auth_key
        elif self.auth_mode == "x-api-key" and self.auth_key:
            headers["x-api-key"] = self.auth_key
        return headers

    def create_mailbox(self, name: str | None = None) -> TempMailbox:
        """创建临时邮箱。admin 模式走 /admin/new_address，否则 /api/new_address。

        domain 池非空时每次随机选一个域名，用于分散多域名风控。
        """
        name = (name or _random_local_name()).strip() or _random_local_name()
        if self.auth_mode == "x-admin-auth":
            url = f"{self.api_base}/admin/new_address"
        else:
            url = f"{self.api_base}/api/new_address"
        payload = {"enablePrefix": True, "name": name}
        domain = self.pick_domain()
        if domain:
            payload["domain"] = domain
        # 同一条 payload 重试：地址名固定，若首次其实已在服务端建成，
        # 重试会得到 400（地址已存在）而不是静默造出两个箱。
        resp = self._request_with_failover(
            lambda s: s.post(url, json=payload, headers=self._headers())
        )
        if resp.status_code not in (200, 201):
            raise MailProviderError(
                f"创建临时邮箱失败 status={resp.status_code}: {(resp.text or '')[:200]}"
            )
        data = resp.json() if resp.text else {}
        address = str(data.get("address") or "").strip()
        jwt = str(data.get("jwt") or "").strip()
        if not address or not jwt:
            raise MailProviderError(f"临时邮箱响应缺字段: {data}")
        logger.info(f"[邮箱] 创建临时邮箱: {address}（域名池 {len(self.domain_pool)} 个，本次 {domain or '默认'}）")
        return TempMailbox(address=address, jwt=jwt)

    def list_mails(self, mailbox: TempMailbox, limit: int = 20, offset: int = 0) -> list[dict]:
        url = f"{self.api_base}/api/mails?limit={int(limit)}&offset={int(offset)}"
        headers = {**self._headers(), "Authorization": f"Bearer {mailbox.jwt}"}
        resp = self._request_with_failover(lambda s: s.get(url, headers=headers))
        if resp.status_code != 200:
            raise MailProviderError(
                f"拉取邮件失败 status={resp.status_code}: {(resp.text or '')[:200]}"
            )
        data = resp.json() if resp.text else {}
        results = data.get("results")
        return results if isinstance(results, list) else []

    @staticmethod
    def _mail_timestamp(mail: dict) -> float:
        """解析 cloudflare_temp_email 的 created_at（UTC 无时区后缀）为 epoch 秒。"""
        raw = str(mail.get("created_at") or mail.get("createdAt") or "").strip()
        if not raw:
            return 0.0
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _extract_from_raw(raw: str) -> tuple[str, str, str]:
        """从原始 MIME 文本解析出 (subject, sender, 正文文本)。

        部分 cloudflare_temp_email 部署（尤其未开启解析的 Worker）只返回 raw，
        不返回 subject/text/html 字段。此时若仍按字段读取，会拿到三个 None，
        表现为"验证码永远收不到"，而邮件其实早已到达。
        """
        if not raw:
            return "", "", ""
        subject = sender = ""
        bodies: list[str] = []
        try:
            from email import message_from_string, policy

            msg = message_from_string(raw, policy=policy.default)
            subject = str(msg.get("Subject") or "")
            sender = str(msg.get("From") or "")
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype not in ("text/plain", "text/html"):
                    continue
                try:
                    bodies.append(str(part.get_content()))
                except Exception:
                    continue
        except Exception:
            # 解析失败时退化为正则粗取
            logger.debug("[邮箱] MIME 解析失败，改用正则兜底", exc_info=True)
            head = raw.split("\n\n", 1)[0]
            for line in head.splitlines():
                low = line.lower()
                if low.startswith("subject:"):
                    subject = subject or line.split(":", 1)[1].strip()
                elif low.startswith("from:"):
                    sender = sender or line.split(":", 1)[1].strip()
            if "\n\n" in raw:
                bodies.append(raw.split("\n\n", 1)[1])
        return subject, sender, "\n".join(bodies)

    def wait_for_otp(
        self,
        mailbox: TempMailbox,
        *,
        after_ts: float,
        max_wait: float = 120.0,
        poll_interval: float = 2.0,
        stop_checker: Callable[[], None] | None = None,
    ) -> str:
        """轮询收件箱，抽取 after_ts 之后到达的 OpenAI OTP。

        stop_checker 每轮都会被调用，用于让「停止任务」能立刻中断等待；
        它通过抛异常来表示停止，这里不捕获，直接向上传播。
        """
        deadline = time.monotonic() + max_wait
        # 容忍服务器与本机 30s 时钟偏差
        threshold = after_ts - 30.0
        scanned = 0
        consecutive_errors = 0
        last_error = ""
        last_subjects: list[str] = []
        while time.monotonic() < deadline:
            if stop_checker is not None:
                stop_checker()
            try:
                mails = self.list_mails(mailbox, limit=10, offset=0)
                consecutive_errors = 0
            except MailProviderError as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                consecutive_errors += 1
                logger.warning(f"[邮箱] 拉取邮件失败（重试 {consecutive_errors}）: {exc}")
                _interruptible_sleep(poll_interval, stop_checker)
                continue
            except Exception as exc:
                # 代理链路抖动（SSLError/超时等）不该终结整次注册：
                # 收件箱轮询窗口长达数分钟，瞬时失败极为常见，继续重试即可。
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                consecutive_errors += 1
                logger.warning(
                    "[邮箱] 连接邮箱服务异常（重试 %s）: %s: %s",
                    consecutive_errors, type(exc).__name__, str(exc)[:160],
                )
                if consecutive_errors >= _MAIL_POLL_MAX_ERRORS:
                    raise MailProviderError(
                        f"连续 {consecutive_errors} 次无法连接邮箱服务，放弃等待验证码：{last_error}"
                    ) from exc
                _interruptible_sleep(poll_interval, stop_checker)
                continue
            for mail in mails:
                ts = self._mail_timestamp(mail)
                if ts and ts < threshold:
                    continue
                subject = str(mail.get("subject") or "")
                sender = str(mail.get("source") or mail.get("from") or "")
                body = str(mail.get("text") or mail.get("html") or "")
                # 该部署只给 raw 时，subject/text/html 全为空，必须从 raw 解析
                if not (subject or body):
                    r_subject, r_sender, r_body = self._extract_from_raw(str(mail.get("raw") or ""))
                    subject = subject or r_subject
                    sender = sender or r_sender
                    body = body or r_body
                scanned += 1
                if subject:
                    last_subjects.append(subject[:60])
                if not looks_like_openai_email(subject, sender, body):
                    continue
                code = extract_otp(subject, body)
                if code:
                    logger.info(f"[邮箱] 收到 OpenAI 验证码（subject={subject[:40]!r}）")
                    return code
            _interruptible_sleep(poll_interval, stop_checker)
        detail = f"；已见邮件主题={last_subjects[-3:]}" if last_subjects else "；收件箱为空"
        raise MailProviderError(
            f"等待 OpenAI 验证码超时（{max_wait:.0f}s），共扫描 {scanned} 封{detail}"
        )
