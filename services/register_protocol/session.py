# -*- coding: utf-8 -*-
"""curl_cffi Session 封装（移植自 turb-gpt-free-register core/session.py）。

统一管理 Cookie、请求头、TLS 指纹与会话级熔断；代理由注册代理池显式注入，
不走 proxy_chain 链式中转。
"""
import hashlib
import logging
import random
import re
import threading
import time
import uuid
from urllib.parse import urlparse

from curl_cffi.requests import Session

from .browser_profile import (
    BROWSER_PROFILE_POOL,
    IMPERSONATE,
    IP_GEO_ENDPOINTS,
    IP_GEO_TIMEOUT,
    REQUEST_TIMEOUT,
    SEC_CH_UA,
    SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS,
    SEC_CH_UA_FULL_VERSION_LIST,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_MODEL,
    SEC_CH_UA_PLATFORM,
    SEC_CH_UA_PLATFORM_VERSION,
    USER_AGENT,
    build_browser_environment,
)
from .constants import OAI_CLIENT_BUILD_NUMBER, OPENAI_CLIENT_VERSION, SENTINEL_SV

logger = logging.getLogger(__name__)

_GEO_CACHE: dict[str, dict] = {}
_GEO_CACHE_LOCK = threading.Lock()
_CF_COOKIE_NAMES = ("cf_clearance", "__cf_bm", "__cfseq", "cf_chl_rc_i", "cf_chl_rc_ni", "cf_chl_rc_m")
_COUNTRY_NAME_TO_CODE = {
    "JAPAN": "JP", "CHINA": "CN", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
    "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "VIETNAM": "VN", "VIET NAM": "VN",
    "THAILAND": "TH", "SINGAPORE": "SG", "HONG KONG": "HK", "TAIWAN": "TW",
    "SOUTH KOREA": "KR", "REPUBLIC OF KOREA": "KR", "INDONESIA": "ID", "MALAYSIA": "MY",
    "PHILIPPINES": "PH", "INDIA": "IN", "AUSTRALIA": "AU", "CANADA": "CA",
    "GERMANY": "DE", "FRANCE": "FR", "NETHERLANDS": "NL", "BRAZIL": "BR",
}


def _seed_uuid(seed: str, salt: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{salt}:{seed}".strip()))


def _seed_int(seed: str, salt: str, *, bits: int = 63) -> int:
    digest = hashlib.sha256(f"{salt}:{seed}".encode("utf-8")).digest()
    nbytes = max(1, (bits + 7) // 8)
    value = int.from_bytes(digest[:nbytes], "big")
    return value & ((1 << bits) - 1)


class BrowserSession:
    """模拟 Chrome 浏览器的 HTTP 会话管理器（curl_cffi impersonate 绕过 TLS 指纹）。"""

    def __init__(
        self,
        proxy: str | None = "",
        *,
        detect_exit_geo: bool = True,
        auto_locale_from_ip: bool = True,
        device_id: str | None = None,
        auth_session_logging_id: str | None = None,
        oai_session_id: str | None = None,
        sentinel_sid: str | None = None,
        browser_profile: dict | None = None,
        fingerprint_seed: str | None = None,
    ) -> None:
        # proxy="" → 直连；proxy="..." → 指定代理
        self.proxy = proxy or ""
        self.proxy_target = self.proxy
        self.fingerprint_seed = str(fingerprint_seed or "").strip()

        if device_id:
            self.device_id = str(device_id)
        elif self.fingerprint_seed:
            self.device_id = _seed_uuid(self.fingerprint_seed, "device_id")
        else:
            self.device_id = str(uuid.uuid4())

        if auth_session_logging_id:
            self.auth_session_logging_id = str(auth_session_logging_id)
        elif self.fingerprint_seed:
            self.auth_session_logging_id = _seed_uuid(self.fingerprint_seed, "auth_session_logging_id")
        else:
            self.auth_session_logging_id = str(uuid.uuid4())

        self.document_navigation_id = str(uuid.uuid4())

        if oai_session_id:
            self.oai_session_id = str(oai_session_id)
        elif self.fingerprint_seed:
            self.oai_session_id = _seed_uuid(self.fingerprint_seed, "oai_session_id")
        else:
            self.oai_session_id = str(uuid.uuid4())

        if self.fingerprint_seed:
            self.datadog_trace_id = str(_seed_int(self.fingerprint_seed, "datadog_trace_id"))
            self.datadog_parent_id = str(_seed_int(self.fingerprint_seed, "datadog_parent_id"))
        else:
            self.datadog_trace_id = str(random.getrandbits(63))
            self.datadog_parent_id = str(random.getrandbits(63))
        self.datadog_origin = "rum"
        self.client_build_number = str(OAI_CLIENT_BUILD_NUMBER)
        self.client_version = str(OPENAI_CLIENT_VERSION)

        if sentinel_sid:
            self.sentinel_sid = str(sentinel_sid)
        elif self.fingerprint_seed:
            self.sentinel_sid = _seed_uuid(self.fingerprint_seed, "sentinel_sid")
        else:
            self.sentinel_sid = str(uuid.uuid4())

        if self.fingerprint_seed:
            self.sentinel_iframe_sid = _seed_uuid(self.fingerprint_seed, "sentinel_iframe_sid")
            self.react_listening_key = "_reactListening" + _seed_uuid(self.fingerprint_seed, "react_listening_key").replace("-", "")[:12]
            self.react_container_key = "__reactContainer$" + _seed_uuid(self.fingerprint_seed, "react_container_key").replace("-", "")[:11]
        else:
            self.sentinel_iframe_sid = str(uuid.uuid4())
            self.react_listening_key = "_reactListening" + uuid.uuid4().hex[:12]
            self.react_container_key = "__reactContainer$" + uuid.uuid4().hex[:11]
        self.react_resources_key = "__reactResources$" + self.react_container_key.split("$", 1)[1]

        self.session = Session(impersonate=IMPERSONATE)
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.session.timeout = REQUEST_TIMEOUT

        # 会话级熔断：403→900s / 429→300s 冷却
        self.blocked_until = 0.0
        self.blocked_reason = ""

        self._auto_locale_from_ip = auto_locale_from_ip
        self.exit_geo = self._detect_exit_geo() if detect_exit_geo else {}

        if browser_profile:
            self.browser_profile = dict(browser_profile)
        else:
            self.browser_profile = dict(
                self._seeded_browser_profile(self.fingerprint_seed, self.exit_geo)
            )
        self.browser_profile["react_listening_key"] = self.react_listening_key
        self.browser_profile["react_container_key"] = self.react_container_key
        self.browser_profile["react_resources_key"] = self.react_resources_key

        # oai-did / oai-locale 一方 Cookie 与头部、Sentinel 指纹保持一致
        locale = self.navigator_language()
        for domain in ("chatgpt.com", "auth.openai.com", "sentinel.openai.com"):
            self.session.cookies.set("oai-did", self.device_id, domain=domain, path="/")
            self.session.cookies.set("oai-locale", locale, domain=domain, path="/")

        self._cf_cookie_seen = self.cf_cookie_snapshot()

    def _seeded_browser_profile(self, seed: str, geo: dict | None) -> dict:
        pool = list(BROWSER_PROFILE_POOL or [])
        base = pool[_seed_int(seed, "browser_profile_index", bits=32) % len(pool)] if (seed and pool) else None
        return build_browser_environment(
            geo,
            base_profile=base,
            build_id=self.client_version,
            auto_locale_from_ip=self._auto_locale_from_ip,
        )

    # ---------------- 上下文管理 ----------------

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------------- Cookie / 指纹工具 ----------------

    def cf_cookie_snapshot(self) -> dict:
        out = {}
        try:
            for cookie in self.session.cookies.jar:
                name = getattr(cookie, "name", "")
                if name in _CF_COOKIE_NAMES:
                    out[f"{getattr(cookie, 'domain', '')}:{name}"] = len(str(getattr(cookie, "value", "") or ""))
        except Exception:
            pass
        return out

    def _observe_cf_cookie_changes(self, url: str) -> None:
        current = self.cf_cookie_snapshot()
        if current != getattr(self, "_cf_cookie_seen", {}):
            logger.info("[CF] Cookie 状态更新 url=%s keys=%s", url, sorted(current.keys()))
            self._cf_cookie_seen = current

    def _cookie_header_for_domain(self, domain: str) -> str:
        pairs = []
        wanted = domain.lower().lstrip(".")
        try:
            for cookie in self.session.cookies.jar:
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")
                cdom = str(getattr(cookie, "domain", "") or "").lower().lstrip(".")
                if not name:
                    continue
                if cdom and not (wanted == cdom or wanted.endswith("." + cdom) or cdom.endswith("." + wanted)):
                    continue
                pairs.append(f"{name}={value}")
        except Exception:
            pass
        return "; ".join(pairs)

    def _cookie_header_with_device(self, domain: str) -> str:
        existing = self._cookie_header_for_domain(domain)
        pairs = [p.strip() for p in existing.split(";") if p.strip()]
        names = {p.split("=", 1)[0].strip() for p in pairs if "=" in p}
        if "oai-did" not in names:
            pairs.append(f"oai-did={self.device_id}")
        if "oai-locale" not in names:
            pairs.append(f"oai-locale={self.navigator_language()}")
        return "; ".join(pairs)

    def auth_cookie_header(self) -> str:
        return self._cookie_header_with_device("auth.openai.com")

    def chatgpt_cookie_header(self) -> str:
        return self._cookie_header_with_device("chatgpt.com")

    def navigator_language(self) -> str:
        return str((getattr(self, "browser_profile", {}) or {}).get("navigator_language") or "en-US")

    def js_timezone_offset_min(self) -> int:
        profile = getattr(self, "browser_profile", {}) or {}
        return -int(profile.get("timezone_offset_minutes", 0) or 0)

    # ---------------- 出口 IP 地理 ----------------

    def _detect_exit_geo(self) -> dict:
        if not self._auto_locale_from_ip:
            return {}
        cache_key = self.proxy or "__direct__"
        with _GEO_CACHE_LOCK:
            cached = _GEO_CACHE.get(cache_key)
            if cached is not None:
                return dict(cached)

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        for url in IP_GEO_ENDPOINTS:
            try:
                resp = self.session.get(url, headers=headers, timeout=IP_GEO_TIMEOUT)
                if resp.status_code != 200:
                    continue
                geo = self._normalize_geo_response(resp.json())
                if geo.get("country") or geo.get("timezone"):
                    with _GEO_CACHE_LOCK:
                        _GEO_CACHE[cache_key] = dict(geo)
                    logger.info(
                        "[指纹] 出口IP地理: ip=%s country=%s city=%s tz=%s",
                        geo.get("ip") or "?", geo.get("country") or "?",
                        geo.get("city") or "?", geo.get("timezone") or "?",
                    )
                    return geo
            except Exception as exc:
                logger.debug(f"[指纹] 出口 IP 地理检测失败 endpoint={url}: {exc}")
        with _GEO_CACHE_LOCK:
            _GEO_CACHE[cache_key] = {}
        return {}

    @staticmethod
    def _normalize_country_code(value: object) -> str:
        text = str(value or "").strip().upper().replace("_", " ")
        if len(text) == 2 and text.isalpha():
            return text
        return _COUNTRY_NAME_TO_CODE.get(text, text if len(text) == 2 else "")

    @staticmethod
    def _normalize_geo_response(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        timezone = data.get("timezone")
        if isinstance(timezone, dict):
            timezone = timezone.get("id") or timezone.get("name")
        raw_country = data.get("country_code") or data.get("countryCode")
        if not raw_country:
            country_obj = data.get("country")
            if isinstance(country_obj, dict):
                raw_country = country_obj.get("code") or country_obj.get("iso_code") or country_obj.get("name")
            else:
                raw_country = country_obj
        return {
            "ip": data.get("ip") or data.get("query"),
            "country": BrowserSession._normalize_country_code(raw_country),
            "region": data.get("region") or data.get("regionName"),
            "city": data.get("city"),
            "timezone": timezone or "",
            "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
        }

    # ---------------- 请求头 ----------------

    def _get_common_headers(self) -> dict:
        profile = getattr(self, "browser_profile", {}) or {}
        headers = {
            "User-Agent": str(profile.get("user_agent") or USER_AGENT),
            "accept-language": str(profile.get("accept_language") or "en-US,en;q=0.9"),
        }
        if bool(profile.get("send_client_hints", True)):
            headers["sec-ch-ua"] = str(profile.get("sec_ch_ua") or SEC_CH_UA)
            headers["sec-ch-ua-mobile"] = str(profile.get("sec_ch_ua_mobile") or SEC_CH_UA_MOBILE)
            headers["sec-ch-ua-platform"] = str(profile.get("sec_ch_ua_platform") or SEC_CH_UA_PLATFORM)
        return headers

    @staticmethod
    def _sec_fetch_site_for(target_origin: str, referer: str) -> str:
        ref = (referer or "").lower()
        target = target_origin.lower().rstrip("/")
        if ref.startswith(target):
            return "same-origin"
        if ref.startswith(("https://chatgpt.com", "https://auth.openai.com", "https://sentinel.openai.com")):
            return "cross-site"
        return "none"

    def get_datadog_headers(self) -> dict:
        return {
            "x-datadog-origin": self.datadog_origin,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": self.datadog_trace_id,
            "x-datadog-parent-id": self.datadog_parent_id,
        }

    def get_trace_context_headers(self) -> dict:
        trace_hex = format(int(self.datadog_trace_id), "032x")[-32:]
        parent_hex = format(int(self.datadog_parent_id), "016x")[-16:]
        return {
            "traceparent": f"00-{trace_hex}-{parent_hex}-01",
            "tracestate": f"dd=s:1;o:{self.datadog_origin}",
        }

    def _attach_auth_rum_headers(self, headers: dict) -> dict:
        self.datadog_trace_id = str(random.getrandbits(64) or 1)
        self.datadog_parent_id = str(random.getrandbits(64) or 1)
        headers.update(self.get_trace_context_headers())
        headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        headers["x-openai-document-navigation-id"] = self.document_navigation_id
        headers.update(self.get_datadog_headers())
        return headers

    def rotate_document_navigation_id(self) -> str:
        self.document_navigation_id = str(uuid.uuid4())
        return self.document_navigation_id

    def _attach_datadog_headers(self, headers: dict) -> dict:
        headers.update(self.get_datadog_headers())
        return headers

    def _attach_oai_context_headers(self, headers: dict) -> dict:
        headers["oai-client-build-number"] = str(self.client_build_number)
        headers["oai-client-version"] = str(self.client_version)
        headers["oai-device-id"] = self.device_id
        headers["oai-language"] = self.navigator_language()
        headers["oai-session-id"] = self.oai_session_id
        return headers

    def observe_chatgpt_document(self, response) -> None:
        """从真实登录页 HTML 同步 ChatGPT 前端 build 元数据。"""
        try:
            final_url = str(getattr(response, "url", "") or "")
            if urlparse(final_url).hostname != "chatgpt.com":
                return
            html = str(getattr(response, "text", "") or "")
            build = re.search(r'\bdata-build=["\']([^"\']+)', html[:200000])
            seq = re.search(r'\bdata-seq=["\']([^"\']+)', html[:200000])
            if build:
                self.client_version = build.group(1)
                self.browser_profile["build_id"] = self.client_version
            if seq:
                self.client_build_number = seq.group(1)
            if build or seq:
                logger.info("[指纹] 同步 ChatGPT 前端版本：build=%s seq=%s", self.client_version, self.client_build_number)
        except Exception as exc:
            logger.debug("[指纹] 解析 ChatGPT 登录页 build 失败：%s", exc)

    def _attach_frontend_api_headers(self, headers: dict) -> dict:
        self._attach_oai_context_headers(headers)
        self._attach_datadog_headers(headers)
        return headers

    def get_nextauth_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
        })
        return headers

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
        })
        return self._attach_frontend_api_headers(headers)

    def get_auth_headers(self, referer: str = "https://auth.openai.com/create-account/password") -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
            "origin": "https://auth.openai.com",
        })
        return self._attach_auth_rum_headers(headers)

    def get_auth_navigate_headers(
        self,
        referer: str = "https://chatgpt.com/",
        user_initiated: bool = True,
        target_origin: str = "https://auth.openai.com",
    ) -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": self._sec_fetch_site_for(target_origin, referer),
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        if referer:
            headers["referer"] = referer
        if user_initiated:
            headers["sec-fetch-user"] = "?1"
        return headers

    def get_chatgpt_navigate_headers(self, referer: str = "https://chatgpt.com/", user_initiated: bool = True) -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": self._sec_fetch_site_for("https://chatgpt.com", referer),
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        if referer:
            headers["referer"] = referer
        if user_initiated:
            headers["sec-fetch-user"] = "?1"
        return headers

    def get_sentinel_headers(self) -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "priority": "u=1, i",
        })
        return headers

    def get_sentinel_frame_headers(self, user_initiated: bool = False) -> dict:
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "referer": "https://auth.openai.com/",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "iframe",
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        if user_initiated:
            headers["sec-fetch-user"] = "?1"
        return headers

    # ---------------- 熔断与请求 ----------------

    def _raise_if_circuit_open(self) -> None:
        if self.blocked_until and time.time() < self.blocked_until:
            remain = max(0, int(self.blocked_until - time.time()))
            raise RuntimeError(f"当前会话已熔断冷却（剩余 {remain}s）：{self.blocked_reason}")

    def reset_circuit_breaker(self) -> None:
        self.blocked_until = 0.0
        self.blocked_reason = ""

    @staticmethod
    def _parse_retry_after(value: str | None) -> int:
        if not value:
            return 0
        text = str(value).strip()
        return max(0, int(text)) if text.isdigit() else 0

    def _observe_response_for_circuit_breaker(self, resp, url: str):
        status = int(getattr(resp, "status_code", 0) or 0)
        self._observe_cf_cookie_changes(url)
        if status not in (403, 429):
            return resp
        retry_after = self._parse_retry_after(
            getattr(resp, "headers", {}).get("retry-after") if getattr(resp, "headers", None) else None
        )
        cool_down = retry_after if retry_after > 0 else (300 if status == 429 else 900)
        self.blocked_until = max(self.blocked_until, time.time() + min(cool_down, 3600))
        self.blocked_reason = f"HTTP {status} from {url}"
        logger.warning("[熔断] 会话收到 HTTP %s，冷却 %ss：%s", status, min(cool_down, 3600), url)
        return resp

    @staticmethod
    def _chatgpt_target_route(path: str) -> str:
        if path.startswith("/backend-api/accounts/check/"):
            return "/backend-api/accounts/check/{version}"
        if path.startswith("/backend-anon/accounts/check/"):
            return "/backend-anon/accounts/check/{version}"
        if path.startswith("/backend-api/conversation/") and path != "/backend-api/conversation/init":
            return "/backend-api/conversation/{conversation_id}"
        if path.startswith("/backend-anon/conversation/") and path != "/backend-anon/conversation/init":
            return "/backend-anon/conversation/{conversation_id}"
        return path

    def _attach_openai_target_headers_for_url(self, url: str, headers: dict | None) -> dict | None:
        if headers is None:
            return headers
        try:
            parsed = urlparse(str(url))
        except Exception:
            return headers
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        if host != "chatgpt.com":
            return headers
        if not (path.startswith("/backend-api/") or path.startswith("/backend-anon/") or path.startswith("/ces/")):
            return headers
        headers.setdefault("x-openai-target-path", path)
        headers.setdefault("x-openai-target-route", self._chatgpt_target_route(path))
        return headers

    def get(self, url: str, headers: dict = None, **kwargs):
        self._raise_if_circuit_open()
        headers = self._attach_openai_target_headers_for_url(url, headers)
        resp = self.session.get(url, headers=headers, **kwargs)
        return self._observe_response_for_circuit_breaker(resp, url)

    def post(self, url: str, headers: dict = None, **kwargs):
        self._raise_if_circuit_open()
        headers = self._attach_openai_target_headers_for_url(url, headers)
        resp = self.session.post(url, headers=headers, **kwargs)
        return self._observe_response_for_circuit_breaker(resp, url)
