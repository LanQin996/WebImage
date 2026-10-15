# -*- coding: utf-8 -*-
"""ChatGPT 前端 bootstrap 预热（移植自 turb-gpt-free-register core/chatgpt_bootstrap.py）。

只做可失败的预热：任何单个接口异常都会记录并继续，不打断注册主流程。
"""
from __future__ import annotations

import logging

from .session import BrowserSession

logger = logging.getLogger(__name__)

_ANON_BASE = "https://chatgpt.com/backend-anon"


def _safe_request(label: str, fn, *, strict: bool = False):
    try:
        resp = fn()
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {(getattr(resp, 'text', '') or '')[:180]}")
        return resp
    except Exception as exc:
        if strict:
            raise
        logger.debug("[Bootstrap] %s 跳过/失败：%s: %s", label, type(exc).__name__, str(exc)[:180])
        return None


def anonymous_bootstrap(session: BrowserSession, *, strict: bool = False) -> None:
    """注册前匿名态登录页初始化（对齐 2026-09 Web 轨迹）。"""
    referer = "https://chatgpt.com/auth/login"
    tz = session.js_timezone_offset_min()
    logger.info("[Bootstrap] 匿名态 ChatGPT 预热开始")
    _safe_request("anon accounts/check", lambda: session.get(
        f"{_ANON_BASE}/accounts/check/v4-2023-04-27?timezone_offset_min={tz}",
        headers=session.get_chatgpt_headers(referer=referer),
    ), strict=strict)
    _safe_request("CES settings", lambda: session.get(
        "https://chatgpt.com/ces/v1/projects/oai/settings",
        headers=session.get_nextauth_headers(referer=referer),
    ), strict=strict)
    _safe_request("anon me", lambda: session.get(
        f"{_ANON_BASE}/me", headers=session.get_chatgpt_headers(referer=referer),
    ), strict=strict)
    profile = getattr(session, "browser_profile", {}) or {}
    country = str((profile.get("geo") or {}).get("country") or "").upper()
    if not country:
        language = str(profile.get("navigator_language") or "")
        if "-" in language:
            country = language.rsplit("-", 1)[-1].upper()
    if country:
        _safe_request("anon pricing config", lambda: session.get(
            f"{_ANON_BASE}/checkout_pricing_config/configs/{country}",
            headers=session.get_chatgpt_headers(referer=referer),
        ), strict=strict)
    if not strict:
        session.reset_circuit_breaker()
    logger.info("[Bootstrap] 匿名态 ChatGPT 预热完成")
