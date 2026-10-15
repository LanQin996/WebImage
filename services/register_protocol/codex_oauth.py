# -*- coding: utf-8 -*-
"""注册成功后的 Codex OAuth 授权（移植自 turb-gpt-free-register core/codex_oauth.py）。

仅保留 local 模式：本地 PKCE 生成授权地址 → 全新干净 session 登录
→ 密码/邮箱 OTP → 选 workspace → 拿 callback code → 换 token。
不走 CPA/sub2，不走手机接码（Auth 明确要求手机号时报错抛出）。
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

from .chatgpt_bootstrap import anonymous_bootstrap
from .constants import (
    CODEX_AUTH_URL,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    CODEX_TOKEN_URL,
)
from .openai_auth import (
    AccountUnusableError,
    _extract_error_code,
    _is_retryable_authorize_error,
    _is_transient_network_error,
    _reset_retryable_circuit,
    build_sentinel_header,
    detect_account_unusable_response_body,
    request_sentinel_token,
)
from .session import BrowserSession

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 15
_NET_MAX_ATTEMPTS = 3
_NET_BACKOFF_BASE = 2.0
_WORKSPACE_COOKIE_WAIT_SECONDS = 4.0
_WORKSPACE_POLL_INTERVAL_SECONDS = 0.2


def _with_net_retry(label: str, fn):
    last_exc = None
    for attempt in range(1, _NET_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                raise
            if attempt >= _NET_MAX_ATTEMPTS:
                break
            backoff = _NET_BACKOFF_BASE ** (attempt - 1)
            logger.warning(f"[Codex] {label} 临时网络错误 ({str(exc)[:120]})，{backoff:.1f}s 后重试 ({attempt}/{_NET_MAX_ATTEMPTS})")
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"[Codex] {label} 重试耗尽")


def _with_auth_navigation_retry(session: BrowserSession, label: str, fn):
    last_exc = None
    for attempt in range(1, _NET_MAX_ATTEMPTS + 1):
        try:
            resp = fn()
            status = int(getattr(resp, "status_code", 0) or 0)
            if status >= 400:
                error = RuntimeError(f"{label} status={status}, body={(getattr(resp, 'text', '') or '')[:180]}")
                error.response = resp
                raise error
            return resp
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_authorize_error(exc) or attempt >= _NET_MAX_ATTEMPTS:
                raise
            _reset_retryable_circuit(session)
            backoff = _NET_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                "[Codex] %s 临时失败（%s/%s）：%s；保留 session/Cookie，%.1fs 后重试",
                label, attempt, _NET_MAX_ATTEMPTS, str(exc)[:160], backoff,
            )
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"[Codex] {label} 重试耗尽")


# ---------------- PKCE / state ----------------

def _generate_pkce() -> tuple[str, str]:
    verifier_bytes = secrets.token_bytes(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _generate_state() -> str:
    return secrets.token_urlsafe(32)


def _build_authorize_url(state: str, code_challenge: str, prompt: str = "login") -> str:
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{CODEX_AUTH_URL}?{urlencode(params)}"


def _ensure_oai_context_url(auth_url: str, session: BrowserSession) -> str:
    try:
        parsed = urlparse(auth_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        additions = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": "login_or_signup",
        }
        for key, value in additions.items():
            if not params.get(key):
                params[key] = [value]
                changed = True
        if not changed:
            return auth_url
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()
    except Exception:
        return auth_url


# ---------------- 小工具 ----------------

def _is_redirect_uri(location: str) -> bool:
    try:
        parsed = urlparse(location)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port == 1455
        and parsed.path == "/auth/callback"
    )


def _extract_code(location: str, state: str) -> str:
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    err = (qs.get("error") or [""])[0]
    if err:
        raise RuntimeError(f"[Codex] 授权服务器返回错误: error={err}, desc={(qs.get('error_description') or [''])[0]}")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise RuntimeError(f"[Codex] redirect_uri 缺少 code: {location}")
    returned_state = (qs.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise RuntimeError("[Codex] state 不匹配（疑似 CSRF）")
    return code


def _decode_jwt_segment(seg: str) -> dict:
    try:
        padding = "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg + padding))
    except Exception:
        return {}


def _post_json(session, url, payload, referer, sentinel_header=None, so_header=None):
    headers = session.get_auth_headers(referer=referer)
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(url, headers=headers, data=json.dumps(payload), allow_redirects=False)
    _cache_auth_session_metadata(session, resp)
    return resp


def _resp_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _decode_auth_session_metadata(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    return _decode_jwt_segment(value.split(".", 1)[0])


def _cache_auth_session_metadata(session, resp) -> None:
    try:
        payload = _resp_json(resp)
        if not isinstance(payload, dict):
            return
        value = payload.get("oai-client-auth-session") or payload.get("auth_session") or payload.get("authSession")
        cached = _decode_auth_session_metadata(value)
        if cached:
            session._codex_auth_session_payload = cached
    except Exception:
        pass


def _page_type(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    return str((page if isinstance(page, dict) else {}).get("type") or "").strip()


def _extract_continue_url(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    return str(
        result.get("continue_url") or result.get("external_url") or result.get("redirect_url")
        or result.get("url") or page.get("continue_url") or page.get("external_url")
        or page.get("redirect_url") or page.get("url") or ""
    ).strip()


def _result_text(result: dict | None) -> str:
    try:
        return json.dumps(result or {}, ensure_ascii=False, separators=(",", ":")).lower()
    except Exception:
        return str(result or "").lower()


def _result_has_any(result: dict | None, *terms: str) -> bool:
    text = _result_text(result)
    return any(str(term).lower() in text for term in terms)


def _is_password_step(result: dict | None) -> bool:
    return _result_has_any(
        result, "log-in/password", "login/password", "password_verify",
        '"page":"password"', '"type":"password"', '"type":"login_password"',
    )


def _is_email_otp_step(result: dict | None) -> bool:
    return _result_has_any(result, "email-verification", "email_otp", "email-otp", "email_otp_send")


def _is_mfa_step(result: dict | None, continue_url: str = "") -> bool:
    return "/mfa-challenge/" in str(continue_url or "").lower() or _result_has_any(
        result, "mfa-challenge", "mfa_challenge", "totp", '"type":"mfa"',
    )


def _is_phone_step(result: dict | None) -> bool:
    return _result_has_any(
        result, "add-phone", "add_phone", "phone-verification", "phone_verification", "phone_otp",
    )


# ---------------- 流程步骤 ----------------

def _codex_protocol_fingerprint_warmup(session: BrowserSession) -> None:
    logger.info("[Codex][指纹预热] ChatGPT document + 匿名前端初始化")
    resp = _with_auth_navigation_retry(
        session,
        "ChatGPT 指纹预热",
        lambda: session.get(
            "https://chatgpt.com/auth/login",
            headers=session.get_chatgpt_navigate_headers(referer="", user_initiated=False),
            allow_redirects=True,
        ),
    )
    session.observe_chatgpt_document(resp)
    anonymous_bootstrap(session, strict=False)
    session.reset_circuit_breaker()


def _codex_auth_preflight(session: BrowserSession) -> None:
    _with_auth_navigation_retry(
        session,
        "auth document 预检",
        lambda: session.get(
            "https://auth.openai.com/log-in",
            headers=session.get_auth_navigate_headers(referer="", user_initiated=False, target_origin="https://auth.openai.com"),
            allow_redirects=True,
        ),
    )


def _bootstrap_authorize(session: BrowserSession, state: str, code_challenge: str) -> None:
    auth_url = _ensure_oai_context_url(_build_authorize_url(state, code_challenge, prompt="login"), session)
    headers = session.get_auth_navigate_headers(referer="", user_initiated=True)
    logger.info("[Codex] 跟随 Codex authorize URL 建立会话...")
    _with_auth_navigation_retry(
        session, "bootstrap authorize",
        lambda: session.get(auth_url, headers=headers, allow_redirects=True),
    )


def _submit_email(session: BrowserSession, email: str) -> dict:
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/authorize/continue",
        {"username": {"kind": "email", "value": email}},
        referer="https://auth.openai.com/log-in",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"[Codex] 提交邮箱失败 status={resp.status_code}: {(resp.text or '')[:300]}")
    result = _resp_json(resp)
    logger.info(
        "[Codex] 已提交邮箱 %s，Auth 下一步：page=%s continue=%s",
        email, _page_type(result) or "-", _extract_continue_url(result) or "-",
    )
    return result


def _password_verify(session: BrowserSession, password: str) -> dict:
    sentinel_resp = request_sentinel_token(session, "password_verify")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "password_verify")
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/password/verify",
        {"password": password},
        referer="https://auth.openai.com/log-in/password",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"[Codex] 密码验证失败 status={resp.status_code}: {(resp.text or '')[:300]}")
    logger.info("[Codex] 密码验证通过")
    return _resp_json(resp)


def _follow_login_continue(session: BrowserSession, continue_url: str, state: str) -> str | None:
    if not continue_url:
        return None
    url = continue_url if continue_url.startswith("http") else ("https://auth.openai.com" + continue_url)
    for _hop in range(_MAX_REDIRECTS):
        if _is_redirect_uri(url):
            return url
        headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/", user_initiated=True)
        resp = session.get(url, headers=headers, allow_redirects=False)
        loc = resp.headers.get("location") or resp.headers.get("Location")
        if not loc:
            return None
        url = loc if loc.startswith("http") else ("https://auth.openai.com" + loc)
    raise RuntimeError(f"[Codex] 登录 continue 跟随超过 {_MAX_REDIRECTS} 跳")


def _try_password_login(
    session: BrowserSession,
    email: str,
    state: str,
    password: str,
    initial_result: dict | None,
) -> tuple[str, str | None, dict]:
    """有注册密码时优先走密码登录。返回 (status, callback_url_or_None, result)。"""
    if not password or not _is_password_step(initial_result):
        return "not_applicable", None, initial_result or {}

    logger.info("[Codex] 走注册密码登录：%s", email)
    result = _password_verify(session, password)
    continue_url = _extract_continue_url(result)
    page_type = _page_type(result)

    if _is_mfa_step(result, continue_url) or page_type == "mfa_challenge":
        raise RuntimeError(f"[Codex] 账号触发了 MFA challenge，但注册流程未启用 TOTP：{email}")

    if _is_email_otp_step(result) or page_type in {"email_verification", "email_otp_send"}:
        logger.info("[Codex] 密码登录后仍要求邮箱 OTP，切换邮箱 OTP：%s", email)
        return "email_otp", None, result

    callback_url = _follow_login_continue(session, continue_url, state) if continue_url else None
    return "logged_in", callback_url, result


def _submit_email_otp(session: BrowserSession, code: str) -> dict:
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/email-otp/validate",
        {"code": code},
        referer="https://auth.openai.com/email-verification",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code != 200:
        error_code = _extract_error_code(resp) or detect_account_unusable_response_body(resp.text or "")
        if error_code in ("account_deactivated", "account_deleted", "account_banned"):
            raise AccountUnusableError(f"[Codex] 账号已废（{error_code}）", error_code=error_code)
        raise RuntimeError(f"[Codex] 邮箱 OTP 验证失败 status={resp.status_code}: {(resp.text or '')[:300]}")
    logger.info("[Codex] 邮箱 OTP 验证通过")
    return _resp_json(resp)


def _get_workspace_id(session: BrowserSession) -> str:
    def find_workspace_id(payload: dict) -> str:
        for workspace in payload.get("workspaces") or []:
            if isinstance(workspace, dict) and workspace.get("id"):
                return str(workspace["id"])
        return ""

    deadline = time.monotonic() + _WORKSPACE_COOKIE_WAIT_SECONDS
    while True:
        raw = None
        try:
            for c in session.session.cookies.jar:
                if c.name == "oai-client-auth-session":
                    raw = c.value
                    break
        except Exception:
            pass
        if raw:
            wid = find_workspace_id(_decode_auth_session_metadata(raw))
            if wid:
                logger.info(f"[Codex] workspace_id={wid}")
                return wid
        wid = find_workspace_id(getattr(session, "_codex_auth_session_payload", {}) or {})
        if wid:
            logger.info(f"[Codex] workspace_id={wid}（来自 Auth 响应元数据）")
            return wid
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("[Codex] 找不到可用的 workspace_id")
        time.sleep(min(_WORKSPACE_POLL_INTERVAL_SECONDS, remaining))


def _follow_until_callback(session: BrowserSession, url: str, state: str) -> str:
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    for _hop in range(_MAX_REDIRECTS):
        if _is_redirect_uri(url):
            return url
        headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/")
        resp = session.get(url, headers=headers, allow_redirects=False)
        loc = resp.headers.get("location") or resp.headers.get("Location")
        if loc is None:
            raise RuntimeError(f"[Codex] 跟随中断，未命中 callback: url={url}, status={getattr(resp, 'status_code', '')}")
        if _is_redirect_uri(loc):
            return loc
        url = loc if loc.startswith("http") else ("https://auth.openai.com" + loc)
    raise RuntimeError(f"[Codex] 跟随 callback 超过 {_MAX_REDIRECTS} 跳")


def _select_workspace_and_get_callback(session: BrowserSession, state: str) -> str:
    wid = _get_workspace_id(session)
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/workspace/select",
        {"workspace_id": wid},
        referer="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    )
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if loc and _is_redirect_uri(loc):
        return loc
    data = _resp_json(resp)
    next_url = None
    for key in ("redirect_url", "continue_url", "url", "next", "location"):
        v = data.get(key)
        if isinstance(v, str) and v:
            next_url = v
            break
    if not next_url and loc:
        next_url = loc
    if not next_url:
        raise RuntimeError(f"[Codex] workspace/select 后找不到下一跳 URL: status={resp.status_code}")
    return _follow_until_callback(session, next_url, state)


def exchange_codex_token(session: BrowserSession, code: str, code_verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": CODEX_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    headers = session._get_common_headers()
    headers.update({"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    logger.info("[Codex] 用 authorization code 换 token...")
    resp = session.post(CODEX_TOKEN_URL, headers=headers, data=urlencode(data))
    if resp.status_code != 200:
        raise RuntimeError(f"[Codex] 换 token 失败 status={resp.status_code}: {(resp.text or '')[:300]}")
    token_resp = resp.json()
    if not token_resp.get("access_token"):
        raise RuntimeError(f"[Codex] token 响应缺少 access_token: {token_resp}")
    return token_resp


def _parse_id_token(id_token: str) -> dict:
    """解析 JWT 里的身份信息。

    Codex 与 platform 两条链路的 claims 结构不同：
      - Codex id_token 带 chatgpt_account_id / chatgpt_plan_type
      - platform id_token 只有 user_id，account_id 由号池 backend-api/me 补齐
    这里兼容两种，取不到就留空（后续刷新会补全），不要伪造。
    """
    if not id_token:
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        claims = _decode_jwt_segment(parts[1])
    except Exception:
        return {}
    auth_claim = claims.get("https://api.openai.com/auth", {}) or {}
    profile_claim = claims.get("https://api.openai.com/profile", {}) or {}
    return {
        "email": claims.get("email") or profile_claim.get("email", ""),
        "account_id": (
            auth_claim.get("chatgpt_account_id")
            or auth_claim.get("account_id")
            or claims.get("account_id")
            or ""
        ),
        "plan_type": (
            auth_claim.get("chatgpt_plan_type")
            or auth_claim.get("plan_type")
            or claims.get("plan_type")
            or ""
        ),
        "user_id": auth_claim.get("user_id") or claims.get("sub") or "",
    }


def build_codex_storage(token_resp: dict, id_claims: dict) -> dict:
    """组装 CLIProxyAPI CodexTokenStorage JSON（chatgpt2api 号池兼容）。"""
    expires_in = token_resp.get("expires_in", 0) or 0
    expired_dt = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    last_refresh_dt = datetime.now(timezone.utc)
    return {
        "id_token": token_resp.get("id_token", ""),
        "access_token": token_resp.get("access_token", ""),
        "refresh_token": token_resp.get("refresh_token", ""),
        "account_id": id_claims.get("account_id", ""),
        "last_refresh": last_refresh_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": id_claims.get("email", ""),
        "type": "codex",
        "expired": expired_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def run_codex_oauth(
    email: str,
    *,
    password: str = "",
    otp_provider=None,
    proxy: str | None = "",
) -> dict:
    """local 模式 Codex 授权：全新干净 session → PKCE → 密码/邮箱 OTP → workspace → code → token。

    otp_provider: fn(email, after_ts) -> 6位验证码（邮箱 OTP）。
    password: 注册时设置的密码；Auth 出现密码页时优先走密码登录。
    返回 {"access_token","refresh_token","id_token","account_id","email","plan_type"}。
    任何失败抛异常（由调用方决定账号去留）。
    """
    if otp_provider is None:
        raise RuntimeError("[Codex] 缺少 otp_provider（邮箱 OTP 获取回调）")

    task_seed = f"codex-oauth:{email.lower()}:{uuid.uuid4()}"
    session = BrowserSession(proxy=proxy or "", fingerprint_seed=task_seed)
    try:
        logger.info(f"[Codex] 开始授权（全新 session）：{email}")

        code_verifier, code_challenge = _generate_pkce()
        state = _generate_state()

        _codex_protocol_fingerprint_warmup(session)
        _codex_auth_preflight(session)
        _bootstrap_authorize(session, state, code_challenge)

        otp_after_ts = time.time()
        auth_result = _submit_email(session, email)
        login_status, early_callback_url, auth_result = _try_password_login(
            session, email, state, password, auth_result,
        )
        password_login_done = login_status == "logged_in"

        if not password_login_done and (login_status == "email_otp" or _is_email_otp_step(auth_result)):
            email_otp = None
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                logger.info(f"[Codex] 等待邮箱 OTP：{email}（第 {attempt}/{max_attempts} 次）")
                try:
                    email_otp = otp_provider(email, after_ts=otp_after_ts)
                    break
                except Exception:
                    if attempt >= max_attempts:
                        raise
                    otp_after_ts = time.time()
                    auth_result = _submit_email(session, email)
                    retry_status, retry_callback, retry_result = _try_password_login(
                        session, email, state, password, auth_result,
                    )
                    if retry_status == "logged_in":
                        password_login_done = True
                        early_callback_url = retry_callback
                        auth_result = retry_result
                        break
            if not password_login_done:
                if not email_otp:
                    raise RuntimeError("[Codex] 邮箱 OTP 获取失败")
                auth_result = _submit_email_otp(session, email_otp)

        if _is_phone_step(auth_result):
            raise RuntimeError(f"[Codex] Auth 要求手机号验证，当前注册流程不支持接码：{email}")
        if _is_mfa_step(auth_result, _extract_continue_url(auth_result)):
            raise RuntimeError(f"[Codex] Auth 要求 MFA，当前注册流程未配置 TOTP：{email}")

        callback_url = early_callback_url or _select_workspace_and_get_callback(session, state)
        code = _extract_code(callback_url, state)
        logger.info(f"[Codex] 已拿到 authorization code：{code[:24]}...")

        token_resp = exchange_codex_token(session, code, code_verifier)
        id_claims = _parse_id_token(token_resp.get("id_token", ""))
        effective_email = id_claims.get("email") or email
        storage = build_codex_storage(token_resp, {**id_claims, "email": effective_email})
        logger.info(
            f"[Codex] 成功：{effective_email}，plan={id_claims.get('plan_type') or 'unknown'}, "
            f"account_id={id_claims.get('account_id') or 'unknown'}"
        )
        return {
            "access_token": storage["access_token"],
            "refresh_token": storage["refresh_token"],
            "id_token": storage["id_token"],
            "account_id": storage["account_id"],
            "email": effective_email,
            "plan_type": id_claims.get("plan_type", ""),
        }
    finally:
        session.close()
