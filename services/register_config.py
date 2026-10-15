# -*- coding: utf-8 -*-
"""注册协议配置存取（挂在 config.json 的 register_settings 块）。"""
from __future__ import annotations

from services.config import _normalize_bool, _normalize_positive_int, config
from services.register_protocol.mail_provider import parse_domain_pool
from services.register_protocol.proxy_pool import parse_proxy_pool

_DEFAULTS: dict[str, object] = {
    "enabled": False,
    # cloudflare_temp_email
    "mail_api_base": "",
    "mail_auth_mode": "none",          # none / x-admin-auth / x-api-key
    "mail_auth_key": "",
    "mail_domain": "",
    # 代理池（一行一个）
    "proxy_pool_text": "",
    # 行为
    "otp_max_wait": 120,
    "otp_poll_interval": 2,
    "otp_total_budget": 240,
    # 取 token 链路：platform（web，无需二次登录）/ codex（需接码，易撞手机号墙）
    "token_source": "platform",
    "auto_locale_from_ip": True,
    "anon_bootstrap": True,
    "send_sentinel_on_email_otp_validate": False,
    # 批量
    "default_count": 1,
    "default_concurrency": 1,
    "fixed_password": "",
    # 水位守护（自动补水）
    "keeper_enabled": False,
    "keeper_target_accounts": 10,        # 目标有效账号数（0 = 不按账号数判定）
    "keeper_target_quota": 0,            # 目标生图额度总量（0 = 不按额度判定）
    "keeper_min_quota": 1,               # 单号至少多少额度才算"有效"
    "keeper_estimate_quota": 3,          # 池空时单号额度估算值（用于额度缺口折算）
    "keeper_check_interval": 10,         # 检查周期（分钟）
    "keeper_max_per_round": 3,           # 单轮最多补几个
    "keeper_concurrency": 1,             # 守护补号并发（与手动批量独立）
    "keeper_daily_limit": 50,            # 每日注册上限
    "keeper_source_type": "",            # 只统计该来源（如 codex），留空统计全部
    "keeper_fail_threshold": 5,          # 连续失败几次触发熔断（0 = 不熔断）
    "keeper_fail_cooldown_minutes": 60,  # 熔断时长（分钟）
}

_AUTH_MODES = {"none", "x-admin-auth", "x-api-key"}
_TOKEN_SOURCES = {"platform", "codex"}


def get_register_settings() -> dict:
    raw = config.data.get("register_settings")
    source = raw if isinstance(raw, dict) else {}
    out = dict(_DEFAULTS)
    for key in out:
        if key in source:
            out[key] = source[key]
    out["enabled"] = _normalize_bool(out.get("enabled"), False)
    out["mail_api_base"] = str(out.get("mail_api_base") or "").strip().rstrip("/")
    mode = str(out.get("mail_auth_mode") or "none").strip().lower()
    out["mail_auth_mode"] = mode if mode in _AUTH_MODES else "none"
    out["mail_auth_key"] = str(out.get("mail_auth_key") or "").strip()
    out["mail_domain"] = str(out.get("mail_domain") or "").strip()
    out["proxy_pool_text"] = str(out.get("proxy_pool_text") or "")
    # 下限 15s 防误填过小，上限 600s 防误填过大导致任务长时间挂死
    out["otp_max_wait"] = min(600, _normalize_positive_int(out.get("otp_max_wait"), 120, 15))
    out["otp_poll_interval"] = max(1, min(30, _normalize_positive_int(out.get("otp_poll_interval"), 2, 1)))
    # 0 = 不限总时长；否则给整个 OTP 阶段（含重试）封顶，防止拖死批量
    out["otp_total_budget"] = min(1800, _normalize_positive_int(out.get("otp_total_budget"), 240, 0))
    _token_source = str(out.get("token_source") or "platform").strip().lower()
    out["token_source"] = _token_source if _token_source in _TOKEN_SOURCES else "platform"
    out["auto_locale_from_ip"] = _normalize_bool(out.get("auto_locale_from_ip"), True)
    out["anon_bootstrap"] = _normalize_bool(out.get("anon_bootstrap"), True)
    out["send_sentinel_on_email_otp_validate"] = _normalize_bool(
        out.get("send_sentinel_on_email_otp_validate"), False
    )
    out["default_count"] = max(1, _normalize_positive_int(out.get("default_count"), 1, 1))
    out["default_concurrency"] = max(1, min(8, _normalize_positive_int(out.get("default_concurrency"), 1, 1)))
    out["fixed_password"] = str(out.get("fixed_password") or "")
    # 水位守护
    out["keeper_enabled"] = _normalize_bool(out.get("keeper_enabled"), False)
    out["keeper_target_accounts"] = _normalize_positive_int(out.get("keeper_target_accounts"), 10, 0)
    out["keeper_target_quota"] = _normalize_positive_int(out.get("keeper_target_quota"), 0, 0)
    out["keeper_min_quota"] = _normalize_positive_int(out.get("keeper_min_quota"), 1, 0)
    out["keeper_estimate_quota"] = max(1, _normalize_positive_int(out.get("keeper_estimate_quota"), 3, 1))
    out["keeper_check_interval"] = max(1, _normalize_positive_int(out.get("keeper_check_interval"), 10, 1))
    out["keeper_max_per_round"] = max(1, _normalize_positive_int(out.get("keeper_max_per_round"), 3, 1))
    out["keeper_concurrency"] = max(1, min(8, _normalize_positive_int(out.get("keeper_concurrency"), 1, 1)))
    out["keeper_daily_limit"] = _normalize_positive_int(out.get("keeper_daily_limit"), 50, 0)
    out["keeper_source_type"] = str(out.get("keeper_source_type") or "").strip().lower()
    out["keeper_fail_threshold"] = _normalize_positive_int(out.get("keeper_fail_threshold"), 5, 0)
    out["keeper_fail_cooldown_minutes"] = max(
        1, _normalize_positive_int(out.get("keeper_fail_cooldown_minutes"), 60, 1)
    )
    # 解析后的代理池（运行用）
    out["proxy_pool"] = parse_proxy_pool(out["proxy_pool_text"])
    # 解析后的邮箱域名池（运行用）
    out["domain_pool"] = parse_domain_pool(out["mail_domain"])
    # 就绪状态
    out["ready"] = bool(out["mail_api_base"])
    out["proxy_count"] = len(out["proxy_pool"])
    out["domain_count"] = len(out["domain_pool"])
    return out


def get_public_register_settings() -> dict:
    """脱敏给前端：不回显 mail_auth_key。"""
    s = get_register_settings()
    public = dict(s)
    public.pop("proxy_pool", None)
    public.pop("domain_pool", None)
    public["has_mail_auth_key"] = bool(s.get("mail_auth_key"))
    public["mail_auth_key"] = ""
    return public


def update_register_settings(updates: dict) -> dict:
    current = config.data.get("register_settings")
    base = dict(current) if isinstance(current, dict) else {}
    base.update({k: v for k, v in updates.items() if k in _DEFAULTS})
    # 密钥留空时保留原值（前端用 has_mail_auth_key 表示已配置）
    if not str(base.get("mail_auth_key") or "").strip():
        if isinstance(current, dict) and str(current.get("mail_auth_key") or "").strip():
            base["mail_auth_key"] = current.get("mail_auth_key")
    config.update({"register_settings": base})
    return get_public_register_settings()


# ---- 每日注册计数持久化（进程重启不重置，日限额才真正生效）----

_DAILY_KEY = "register_daily_counts"
_DAILY_SUCCESS_KEY = "register_daily_success"


def _parse_daily_map(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return out


def load_daily_counts() -> tuple[dict[str, int], dict[str, int]]:
    """返回 (每日尝试数, 每日成功数)。

    兼容旧数据：早期的 register_daily_counts 是按"提交数"记账的整数表，
    在成功表缺失时无法区分成功与否，直接作为尝试数沿用即可。
    """
    attempts = _parse_daily_map(config.data.get(_DAILY_KEY))
    successes = _parse_daily_map(config.data.get(_DAILY_SUCCESS_KEY))
    return attempts, successes


def save_daily_counts(
    counts: dict[str, int],
    *,
    success: dict[str, int] | None = None,
    keep_days: int = 3,
) -> None:
    """只保留最近若干天，避免 config.json 无限膨胀。"""
    recent = sorted(counts.items())[-max(1, keep_days):]
    payload: dict[str, object] = {_DAILY_KEY: {k: int(v) for k, v in recent}}
    if success is not None:
        recent_success = [item for item in sorted(success.items()) if item[0] in dict(recent)]
        payload[_DAILY_SUCCESS_KEY] = {k: int(v) for k, v in recent_success}
    config.update(payload)
