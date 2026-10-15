# -*- coding: utf-8 -*-
"""Platform OAuth（web 链路）：注册后用同一条 session 直接换号池 token。

为什么需要它：
  Codex OAuth 走的是 CLI 客户端（codex_cli_simplified_flow），OpenAI 对这条链路
  常常强制手机号验证，且要另开一条干净 session 重新登录一次——注册刚成功就再登一次，
  触发风控的概率也高。

  历史实现（basketikun/chatgpt2api @ 66fee0c1 之前的 services/register/openai_register.py）
  的做法是整条注册链走 platform OAuth：authorize 用 platform client +
  screen_hint=signup，create_account 的 continue_url 直接带回 platform 回调 code，
  用它换 token 落 source_type=web。全程没有第二次登录，因此不存在手机号墙。

  同时 services/account_service.py 的 _OAUTH_CLIENT_ID 就是
  app_2SKx67EdpoN0G6j64rFvigXD（platform client），号池续期本来就跑在同一条链上，
  用 platform token 才不会出现"注册用 A client、续期用 B client"的错配。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

from .constants import (
    PLATFORM_AUDIENCE,
    PLATFORM_AUTH0_CLIENT,
    PLATFORM_AUTH_URL,
    PLATFORM_CLIENT_ID,
    PLATFORM_REDIRECT_URI,
    PLATFORM_SCOPE,
    PLATFORM_TOKEN_URL,
)
from .session import BrowserSession

logger = logging.getLogger(__name__)

PLATFORM_BASE = "https://platform.openai.com"


def generate_pkce() -> tuple[str, str]:
    """生成 PKCE code_verifier / code_challenge（S256）。"""
    verifier_bytes = secrets.token_bytes(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url(session: BrowserSession, email: str, code_challenge: str) -> str:
    """构造 platform OAuth authorize URL。

    screen_hint=signup 是关键：一次性邮箱域名 OpenAI 本来也会走注册分支，
    但显式声明可以避免被路由到登录分支后在 user/register 报 invalid_auth_step。
    """
    params = {
        "issuer": "https://auth.openai.com",
        "client_id": PLATFORM_CLIENT_ID,
        "audience": PLATFORM_AUDIENCE,
        "redirect_uri": PLATFORM_REDIRECT_URI,
        "device_id": session.device_id,
        "screen_hint": "signup",
        "max_age": "0",
        "login_hint": email,
        "scope": PLATFORM_SCOPE,
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": PLATFORM_AUTH0_CLIENT,
    }
    return f"{PLATFORM_AUTH_URL}?{urlencode(params)}"


def seed_device_cookies(session: BrowserSession) -> None:
    """authorize 之前把 oai-did 写进 auth.openai.com，缺失会被判成无设备上下文。"""
    raw = getattr(session, "session", None)
    jar = getattr(raw, "cookies", None)
    if jar is None:
        return
    for domain in (".auth.openai.com", "auth.openai.com"):
        try:
            jar.set("oai-did", session.device_id, domain=domain)
        except Exception:
            logger.debug("[Platform] 写入 oai-did 失败 domain=%s", domain, exc_info=True)


def follow_authorize(session: BrowserSession, authorize_url: str) -> str:
    """跟随 authorize 重定向，落到 create-account / 登录页。

    注意：不要带 platform.openai.com 的 Referer。实测裸打 authorize 返回 200
    （落到 Create a password 页），而带上 platform referer + sec-fetch-site=cross-site
    反而会被 Cloudflare 判定为可疑跳转，返回 403 "Just a moment..."。
    """
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/")
    headers["sec-fetch-site"] = "none"
    logger.info("[Platform] 跟随 platform authorize ...")
    resp = session.get(authorize_url, headers=headers, allow_redirects=True)
    if _is_cloudflare_challenge(resp):
        # 质询页往往是瞬时的，重试一次即可；仍失败则抛出并带上特征。
        logger.warning("[Platform] authorize 命中 Cloudflare 质询，重试一次")
        resp = session.get(authorize_url, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    final_url = str(getattr(resp, "url", "") or "")
    logger.info(f"[Platform] authorize 落点: {final_url[:160]}")
    return final_url


def _is_cloudflare_challenge(resp) -> bool:
    """识别 Cloudflare 质询页（Just a moment...）。"""
    if resp is None:
        return False
    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (403, 503):
        return False
    body = (getattr(resp, "text", "") or "")[:2000].lower()
    return "just a moment" in body or "cf-chl" in body or "challenge-platform" in body


def extract_code(continue_url: str) -> str:
    """从 continue_url 查询串里取 authorization code。"""
    if not continue_url:
        return ""
    try:
        params = parse_qs(urlparse(continue_url).query)
    except Exception:
        return ""
    return str((params.get("code") or [""])[0]).strip()


def exchange_token(session: BrowserSession, code: str, code_verifier: str) -> dict:
    """用 authorization code 换 token（platform client，与号池续期同源）。"""
    if not code:
        raise RuntimeError("[Platform] authorization code 为空，无法换 token")
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "auth0-client": PLATFORM_AUTH0_CLIENT,
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "origin": PLATFORM_BASE,
        "referer": f"{PLATFORM_BASE}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    logger.info("[Platform] 用 authorization code 换 token ...")
    resp = session.post(
        PLATFORM_TOKEN_URL,
        headers=headers,
        data=json.dumps({
            "client_id": PLATFORM_CLIENT_ID,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": PLATFORM_REDIRECT_URI,
        }),
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"[Platform] 换 token 失败 status={resp.status_code}: {(resp.text or '')[:300]}"
        )
    data = resp.json() if resp.text else {}
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(f"[Platform] token 响应缺少 access_token: {data}")
    logger.info("[Platform] token 换取成功")
    return data
