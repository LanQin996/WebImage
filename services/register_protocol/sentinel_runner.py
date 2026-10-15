# -*- coding: utf-8 -*-
"""Sentinel Runner 适配层（移植自 turb-gpt-free-register core/sentinel_runner.py）。

通过 subprocess 调用 node_assets/sentinel-runner.js，在 Node vm 沙箱中运行真实 sdk.js，
生成可通过校验的 openai-sentinel-token。
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .browser_profile import (
    CHROME_FULL_VERSION,
    CHROME_MAJOR,
    DEVICE_MEMORY,
    HARDWARE_CONCURRENCY,
    JS_HEAP_SIZE_LIMIT,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SEC_CH_UA,
    SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS,
    SEC_CH_UA_FULL_VERSION_LIST,
    SEC_CH_UA_MODEL,
    SEC_CH_UA_PLATFORM,
    SEC_CH_UA_PLATFORM_VERSION,
    USER_AGENT,
)
from .constants import OPENAI_BUILD_ID, SENTINEL_SV

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent / "node_assets"
_RUNNER_PATH = _ASSETS_DIR / "sentinel-runner.js"
_SDK_PATH = _ASSETS_DIR / "sdk.js"

_FLOW_PAGE_URL = {
    "username_password_create": "https://auth.openai.com/create-account/password",
    "email_otp_validate": "https://auth.openai.com/email-verification",
    "authorize_continue": "https://auth.openai.com/email-verification",
    "oauth_create_account": "https://auth.openai.com/about-you",
}

_RUNNER_TIMEOUT = 60


def _resolve_node_executable() -> str:
    override = os.environ.get("NODE_EXECUTABLE")
    if override:
        return override
    return "node.exe" if sys.platform.startswith("win") else "node"


_node_health_cache: dict | None = None
_node_health_cache_at: float = 0.0
_NODE_HEALTH_TTL = 60.0
_node_health_lock = threading.Lock()


def node_health(*, force: bool = False) -> dict:
    """检查 Node 运行环境是否就绪（供设置页/守护状态展示）。

    返回 {available, version, executable, assets_ok, error}。
    不抛异常：任何失败都凝练成 error 字段，便于前端直接显示。
    结果缓存 60s：状态接口会被前端轮询，不能每次都 spawn 一个子进程。
    探测在锁外执行（双检锁）：Node 挂起时不阻塞其他并发查询。
    """
    global _node_health_cache, _node_health_cache_at
    if not force:
        cached = _node_health_cache
        if cached is not None and (time.monotonic() - _node_health_cache_at) < _NODE_HEALTH_TTL:
            return dict(cached)

    result = _probe_node_health()
    with _node_health_lock:
        _node_health_cache = dict(result)
        _node_health_cache_at = time.monotonic()
    return result


def _probe_node_health() -> dict:
    result: dict = {
        "available": False,
        "version": "",
        "executable": "",
        "assets_ok": False,
        "error": "",
    }
    try:
        exe = _resolve_node_executable()
        result["executable"] = exe
        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
        except FileNotFoundError:
            result["error"] = (
                "未找到 Node 可执行文件；注册协议依赖 Node 运行 sentinel sdk.js。"
                "请安装 Node.js 并加入 PATH，或用 NODE_EXECUTABLE 指定绝对路径。"
            )
            return result
        except subprocess.TimeoutExpired:
            result["error"] = "Node 自检超时（>15s）"
            return result
        if proc.returncode != 0:
            result["error"] = f"Node 执行异常 rc={proc.returncode}: {(proc.stderr or '').strip()[:200]}"
            return result
        result["version"] = (proc.stdout or "").strip()
        result["available"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result

    # 资产齐备性（sdk.js / sentinel-runner.js）
    missing = [p.name for p in (_RUNNER_PATH, _SDK_PATH) if not p.exists()]
    if missing:
        result["available"] = False
        result["error"] = f"缺少 sentinel 资源文件: {', '.join(missing)}"
        return result
    result["assets_ok"] = True
    return result


def _ensure_runner_environment() -> None:
    if not _RUNNER_PATH.exists():
        raise FileNotFoundError(f"找不到 sentinel-runner.js: {_RUNNER_PATH}")
    if not _SDK_PATH.exists():
        raise FileNotFoundError(f"找不到 sdk.js: {_SDK_PATH}")


def generate_sentinel_token(
    challenge: dict,
    flow: str,
    device_id: str,
    user_agent: str | None = None,
    page_url: str | None = None,
    browser_profile: dict | None = None,
    sentinel_sid: str | None = None,
    react_listening_key: str | None = None,
    react_container_key: str | None = None,
    react_resources_key: str | None = None,
    cookie: str | None = None,
) -> str:
    """把 sentinel/req 返回的 challenge 喂给 sdk.js，产出 openai-sentinel-token 头值。

    challenge 里的 `_request_p`（发起 req 时用的 p）会通过 --challenge-proof 传给 runner，
    但不作为 challenge 字段暴露给 SDK。
    """
    _ensure_runner_environment()
    if not flow:
        raise ValueError("flow 不能为空")
    if not device_id:
        raise ValueError("device_id 不能为空")

    profile = browser_profile or {}
    browser_family = str(profile.get("browser_family") or "chrome")
    request_idle_callback = int((profile.get("window_feature_flags") or {}).get("requestIdleCallback", 0))
    ua = user_agent or str(profile.get("user_agent") or USER_AGENT)
    screen_width = int(profile.get("screen_width", SCREEN_WIDTH))
    screen_height = int(profile.get("screen_height", SCREEN_HEIGHT))
    hardware_concurrency = int(profile.get("hardware_concurrency", HARDWARE_CONCURRENCY))
    js_heap_size_limit = int(profile.get("js_heap_size_limit", JS_HEAP_SIZE_LIMIT))
    device_memory = int(profile.get("device_memory", DEVICE_MEMORY))
    device_pixel_ratio = float(profile.get("device_pixel_ratio", 2))
    screen_avail_width = int(profile.get("screen_avail_width", screen_width))
    screen_avail_height = int(profile.get("screen_avail_height", max(0, screen_height - 25)))
    outer_width = int(profile.get("outer_width", screen_avail_width))
    outer_height = int(profile.get("outer_height", screen_avail_height))
    inner_width = int(profile.get("inner_width", profile.get("viewport_width", outer_width)))
    inner_height = int(profile.get("inner_height", profile.get("viewport_height", max(0, outer_height - 87))))
    color_depth = int(profile.get("color_depth", 24))
    navigator_language = str(profile.get("navigator_language", "en-US"))
    navigator_languages = list(profile.get("navigator_languages", ["en-US"]))
    chrome_major = str(profile.get("chrome_major", CHROME_MAJOR))
    chrome_full_version = str(profile.get("chrome_full_version", CHROME_FULL_VERSION))
    sec_ch_ua = str(profile.get("sec_ch_ua", SEC_CH_UA))
    sec_ch_ua_platform = str(profile.get("sec_ch_ua_platform", SEC_CH_UA_PLATFORM))
    navigator_platform = str(profile.get("navigator_platform", "MacIntel"))
    navigator_vendor = str(profile.get("navigator_vendor", "Google Inc."))
    user_agent_data_platform = str(profile.get("user_agent_data_platform", sec_ch_ua_platform.strip('"') or "macOS"))
    sec_ch_ua_full_version_list = str(profile.get("sec_ch_ua_full_version_list", SEC_CH_UA_FULL_VERSION_LIST))
    sec_ch_ua_platform_version = str(profile.get("sec_ch_ua_platform_version", SEC_CH_UA_PLATFORM_VERSION))
    sec_ch_ua_arch = str(profile.get("sec_ch_ua_arch", SEC_CH_UA_ARCH))
    sec_ch_ua_bitness = str(profile.get("sec_ch_ua_bitness", SEC_CH_UA_BITNESS))
    sec_ch_ua_model = str(profile.get("sec_ch_ua_model", SEC_CH_UA_MODEL))
    build_id = str(profile.get("build_id", OPENAI_BUILD_ID))
    # Auth 页面 Sentinel token 的 documentElement 通常没有 data-build。
    runner_build_id = "" if page_url is None and flow in {
        "email_otp_validate", "authorize_continue", "oauth_create_account", "username_password_create",
    } else build_id
    timezone_iana = str(profile.get("timezone_iana", "America/Los_Angeles"))
    timezone_name = str(profile.get("timezone_name", "Pacific Daylight Time"))
    timezone_offset_minutes = int(profile.get("timezone_offset_minutes", -420))
    runner_cookie = cookie or f"oai-did={device_id}"

    page = page_url or _FLOW_PAGE_URL.get(flow, "https://auth.openai.com/create-account/password")

    challenge_proof = str(challenge.get("_request_p") or "") if isinstance(challenge, dict) else ""
    challenge_payload = (
        {k: v for k, v in challenge.items() if k != "_request_p"}
        if isinstance(challenge, dict)
        else challenge
    )

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"sentinel-challenge-{flow}-",
        delete=False, encoding="utf-8",
    )
    try:
        json.dump(challenge_payload, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()

        cmd = [
            _resolve_node_executable(),
            str(_RUNNER_PATH),
            "--challenge-file", tmp.name,
            "--flow", flow,
            "--device-id", device_id,
            "--sentinel-sid", sentinel_sid or "",
            "--challenge-proof", challenge_proof,
            "--react-listening-key", react_listening_key or "",
            "--react-container-key", react_container_key or str(profile.get("react_container_key") or ""),
            "--react-resources-key", react_resources_key or str(profile.get("react_resources_key") or ""),
            "--page-url", page,
            "--user-agent", ua,
            "--browser-family", browser_family,
            "--navigator-platform", navigator_platform,
            "--navigator-vendor", navigator_vendor,
            "--user-agent-data-platform", user_agent_data_platform,
            "--request-idle-callback", "1" if request_idle_callback else "0",
            "--sdk", str(_SDK_PATH),
            "--script-src", (
                f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js"
                if flow == "email_otp_validate"
                else "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
            ),
            "--build-id", runner_build_id,
            "--width", str(screen_width),
            "--height", str(screen_height),
            "--avail-width", str(screen_avail_width),
            "--avail-height", str(screen_avail_height),
            "--outer-width", str(outer_width),
            "--outer-height", str(outer_height),
            "--inner-width", str(inner_width),
            "--inner-height", str(inner_height),
            "--color-depth", str(color_depth),
            "--cores", str(hardware_concurrency),
            "--language", navigator_language,
            "--languages", ",".join(navigator_languages),
            "--time-zone", timezone_iana,
            "--timezone-name", timezone_name,
            "--timezone-offset-minutes", str(timezone_offset_minutes),
            "--js-heap-size-limit", str(js_heap_size_limit),
            "--device-memory", str(device_memory),
            "--device-pixel-ratio", str(device_pixel_ratio),
            "--chrome-major", chrome_major,
            "--chrome-full-version", chrome_full_version,
            "--sec-ch-ua", sec_ch_ua,
            "--sec-ch-ua-platform", sec_ch_ua_platform,
            "--sec-ch-ua-full-version-list", sec_ch_ua_full_version_list,
            "--sec-ch-ua-platform-version", sec_ch_ua_platform_version,
            "--sec-ch-ua-arch", sec_ch_ua_arch,
            "--sec-ch-ua-bitness", sec_ch_ua_bitness,
            "--sec-ch-ua-model", sec_ch_ua_model,
            "--webgl-vendor", str(profile.get("webgl_vendor") or ""),
            "--webgl-renderer", str(profile.get("webgl_renderer") or ""),
            "--cookie", runner_cookie,
        ]

        logger.info(f"[SentinelRunner] 调用 Node 生成 token, flow={flow}")

        env = os.environ.copy()
        env["SENTINEL_CONFIG"] = "__none__"
        env["TZ"] = timezone_iana

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(_ASSETS_DIR),
                timeout=_RUNNER_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"sentinel-runner.js 执行超时（>{_RUNNER_TIMEOUT}s），flow={flow}") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "未找到 Node 可执行文件，请确认已安装 Node.js 并加入 PATH，"
                "或通过 NODE_EXECUTABLE 环境变量指定绝对路径。"
            ) from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"sentinel-runner.js 退出码 {proc.returncode}\n"
                f"stderr: {(proc.stderr or '').strip()}\n"
                f"stdout: {(proc.stdout or '').strip()}"
            )

        token_text = (proc.stdout or "").strip()
        if not token_text:
            raise RuntimeError(f"runner 输出为空, stderr: {(proc.stderr or '').strip()}")

        try:
            parsed = json.loads(token_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"runner 输出不是合法 JSON: {token_text[:200]}") from exc

        for required_key in ("p", "c", "id", "flow"):
            if required_key not in parsed:
                raise RuntimeError(f"runner 输出缺少字段 {required_key}: {token_text[:200]}")

        logger.info(
            f"[SentinelRunner] token 生成成功, flow={flow}, "
            f"turnstile={bool(parsed.get('t'))}, so={bool(parsed.get('_so') or parsed.get('so'))}"
        )
        return token_text
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
