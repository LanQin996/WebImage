# -*- coding: utf-8 -*-
"""ChatGPT NextAuth 模块（步骤1-3，移植自 turb-gpt-free-register core/chatgpt_auth.py）。"""
import logging
from urllib.parse import parse_qs, urlencode, urlparse

from .session import BrowserSession

logger = logging.getLogger(__name__)

_CC_CAPS = "login_methods chatgpt_login_finalizer_v1"


def _ensure_authorize_context(authorize_url: str, session: BrowserSession, email: str) -> str:
    try:
        parsed = urlparse(authorize_url)
        if not parsed.netloc.endswith("auth.openai.com"):
            return authorize_url
        params = parse_qs(parsed.query, keep_blank_values=True)
        changed = bool(params.pop("ext-passkey-client-capabilities", None))
        language_getter = getattr(session, "navigator_language", None)
        ui_locale = str(language_getter() if callable(language_getter) else "en-US") or "en-US"
        required = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": "login_or_signup",
            "login_hint": email,
            "ui_locales": ui_locale,
            "ccaps": _CC_CAPS,
            "auth_return_target_category": "chatgpt_home",
        }
        for key, value in required.items():
            if key in {"ccaps", "auth_return_target_category", "ui_locales"}:
                if params.get(key) != [value]:
                    params[key] = [value]
                    changed = True
            elif not params.get(key):
                params[key] = [value]
                changed = True
        if not changed:
            return authorize_url
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()
    except Exception:
        return authorize_url


def get_providers(session: BrowserSession) -> dict:
    logger.info("[步骤1] 获取 OAuth Providers...")
    resp = session.get(
        "https://chatgpt.com/api/auth/providers",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"[步骤1] 成功获取 {len(data)} 个 providers: {list(data.keys())}")
    return data


def get_csrf_token(session: BrowserSession) -> str:
    logger.info("[步骤2] 获取 CSRF Token...")
    resp = session.get(
        "https://chatgpt.com/api/auth/csrf",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
    )
    resp.raise_for_status()
    csrf_token = resp.json().get("csrfToken", "")
    logger.info(f"[步骤2] CSRF Token: {csrf_token[:20]}...")
    return csrf_token


def probe_auth_session(session: BrowserSession) -> dict:
    resp = session.get(
        "https://chatgpt.com/api/auth/session",
        headers=session.get_nextauth_headers(referer="https://chatgpt.com/auth/login"),
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def signin_openai(session: BrowserSession, csrf_token: str, email: str) -> str:
    """步骤3: 发起 OAuth Signin，返回 authorize URL。"""
    query_params = {
        "prompt": "login",
        "ext-oai-did": session.device_id,
        "auth_session_logging_id": session.auth_session_logging_id,
        "screen_hint": "login_or_signup",
        "login_hint": email,
    }
    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query_params)

    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    body = urlencode({"callbackUrl": "/", "csrfToken": csrf_token, "json": "true"})

    logger.info(f"[步骤3] 发起 OAuth Signin, 邮箱: {email}")
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()

    authorize_url = resp.json().get("url", "")
    if not authorize_url:
        raise ValueError(f"[步骤3] 未获取到 authorize URL: {resp.text[:200]}")
    authorize_url = _ensure_authorize_context(authorize_url, session, email)
    logger.info("[步骤3] 获取 authorize URL 成功")
    return authorize_url
