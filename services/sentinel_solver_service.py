"""常驻浏览器哨兵求解服务（sentinel solver）客户端。

网页端 Sentinel 的 turnstile 挑战已经换成随机化 opcode 的 VM，纯 Python 复刻走不通，
需要真实 Chromium 环境执行。这里对接一个独立部署的求解服务：

- 只发送挑战参数（prepare 响应 + 令牌种子 p）+ 当前请求指纹（UA），不发送账号 token、Cookie 或用户 prompt；
- 账号走代理池时额外发送该代理地址（含凭据），让 solver 与业务请求同一出口 IP；
- 服务端自带密钥鉴权（X-Solver-Key / Authorization: Bearer 都发），默认只监听本机端口；
- 未配置时返回 None，调用方回退到内置的纯 Python 求解器。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from services.config import config
from utils.log import logger


class SentinelSolverError(RuntimeError):
    pass


class SentinelSolverService:
    """调用外部 sentinel solver 取 turnstile token。"""

    # /health 的 UA 缓存：成功 10 分钟（求解器不常重启），失败 30 秒（重启后能尽快拿到新 UA）
    USER_AGENT_TTL_SECS = 600.0
    USER_AGENT_ERROR_TTL_SECS = 30.0
    # 探活等待上限：/health 只是顺手查一下环境，不该按求解超时（默认 180s）干等
    HEALTH_TIMEOUT_SECS = 10.0

    def __init__(self) -> None:
        self._user_agent_lock = threading.Lock()
        self._user_agent_cache: tuple[str, str, float] = ("", "", 0.0)

    def settings(self) -> dict[str, Any]:
        value = config.get_sentinel_solver_settings()
        return value if isinstance(value, dict) else {}

    def enabled(self) -> bool:
        settings = self.settings()
        return bool(settings.get("enabled")) and bool(str(settings.get("url") or "").strip())

    def health(self) -> dict[str, Any]:
        settings = self.settings()
        url = str(settings.get("url") or "").strip().rstrip("/")
        if not url:
            return {"ok": False, "error": "sentinel solver 未配置 url"}
        timeout = min(self.HEALTH_TIMEOUT_SECS, float(settings.get("timeout_seconds") or 30))
        try:
            request = urllib.request.Request(
                url + "/health",
                headers=self._headers(settings, with_key=False),
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    def user_agent(self) -> str:
        """求解页面当前 UA（GET /health 的 ``user_agent``）；未启用或查询失败返回 ""。

        求解页面的 navigator.userAgent 必须与主服务请求头/TLS 指纹一致，否则 Sentinel 握手
        会判为可疑（上游表现为 404 Model not found）。每次握手都要用，所以缓存：成功 10 分钟，
        失败/为空只 30 秒，避免求解器刚重启就被钉死。
        """
        settings = self.settings()
        if not self.enabled():
            return ""
        url = str(settings.get("url") or "").strip().rstrip("/")
        with self._user_agent_lock:
            cached_url, cached_value, expires_at = self._user_agent_cache
            if cached_url == url and time.monotonic() < expires_at:
                return cached_value
            status = self.health()
            value = str(status.get("user_agent") or "").strip()
            ttl = self.USER_AGENT_TTL_SECS if value else self.USER_AGENT_ERROR_TTL_SECS
            self._user_agent_cache = (url, value, time.monotonic() + ttl)
            if not value:
                logger.warning({"event": "sentinel_solver_user_agent_unavailable",
                                "error": str(status.get("error") or "empty user_agent")[:200]})
            return value

    @staticmethod
    def _headers(settings: dict[str, Any], with_key: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = str(settings.get("key") or "").strip()
        if with_key and key:
            # 两种鉴权头都发：本项目的 solver 认 X-Solver-Key，另一些实现只认 Bearer
            headers["X-Solver-Key"] = key
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post_json(self, url: str, payload: bytes, settings: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST JSON 并解析；HTTP/网络错误原样抛出，非对象响应按空对象处理（调用方判 ok）。"""
        request = urllib.request.Request(url, data=payload, headers=self._headers(settings), method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8") or "{}"
        data = json.loads(text)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            return ""

    def solve_turnstile(
            self,
            requirements: dict[str, Any],
            p_token: str,
            timeout: float | None = None,
            proxy: str = "",
            user_agent: str = "",
    ) -> str | None:
        """返回 turnstile token；未配置或失败时返回 None（由调用方回退）。

        ``proxy`` 为账号的池出口：solver 用对应代理的浏览器上下文求解；该通道失败不会退回默认出口。
        ``user_agent`` 为主服务当前请求指纹：求解页面会切到它，保证两边指纹同源；为空时用 solver 默认 UA。
        """
        settings = self.settings()
        if not self.enabled():
            return None
        base = str(settings["url"]).strip().rstrip("/")
        body: dict[str, Any] = {"requirements": requirements, "p": p_token}
        proxy = str(proxy or "").strip()
        if proxy:
            body["proxy"] = proxy
        user_agent = str(user_agent or "").strip()
        if user_agent:
            body["user_agent"] = user_agent
        payload = json.dumps(body).encode("utf-8")
        wait = float(timeout if timeout is not None else (settings.get("timeout_seconds") or 180))
        # 不同部署挂的路径不一样：本项目在 /turnstile，遇到 404 再试 /v1/turnstile
        paths = ["/turnstile", "/v1/turnstile"]
        data: dict[str, Any] = {}
        for index, path in enumerate(paths):
            try:
                data = self._post_json(base + path, payload, settings, wait)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and index + 1 < len(paths):
                    continue
                logger.warning({"event": "sentinel_solver_http_error", "status": exc.code, "path": path,
                                "detail": self._http_error_detail(exc)})
                return None
            except Exception as exc:
                logger.warning({"event": "sentinel_solver_failed", "error": str(exc)[:200]})
                return None
        if not data.get("ok"):
            logger.warning({
                "event": "sentinel_solver_rejected",
                "error": str(data.get("error") or "")[:200],
                "lane": data.get("lane"),
            })
            return None
        if proxy and data.get("lane") != "proxy":
            # 老版本 solver 不认识 proxy 字段，会用默认出口求解：记录下来便于排查 IP 不一致
            logger.warning({"event": "sentinel_solver_lane_mismatch", "lane": data.get("lane")})
        token = str(data.get("turnstile") or data.get("token") or "").strip()
        return token or None


sentinel_solver_service = SentinelSolverService()
