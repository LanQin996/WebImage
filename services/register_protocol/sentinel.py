# -*- coding: utf-8 -*-
"""Sentinel Token 生成模块（移植自 turb-gpt-free-register core/sentinel.py）。

p 字段（25 元素指纹数组）+ FNV-1a PoW + sentinel/req 请求体组装。
"""
import base64
import json
import random
import time
from datetime import datetime, timedelta, timezone

from .browser_profile import (
    DOCUMENT_KEY_SAMPLES,
    HARDWARE_CONCURRENCY,
    JS_HEAP_SIZE_LIMIT,
    NAVIGATOR_PROTO_SAMPLES,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    USER_AGENT,
    WINDOW_FEATURE_FLAGS,
    WINDOW_KEY_SAMPLES,
)
from .constants import SENTINEL_SV

_DEFAULT_NAVIGATOR_LANGUAGE = "en-US"
_DEFAULT_NAVIGATOR_LANGUAGES = ["en-US"]
_DEFAULT_TZ_OFFSET = -7 * 60
_DEFAULT_TZ_NAME = "Pacific Daylight Time"


def generate_fingerprint_data(
    device_id: str,
    attempt: int = 1,
    elapsed_ms: float = 0,
    profile: dict | None = None,
) -> list:
    """生成浏览器指纹数组（p 字段的明文）。"""
    profile = profile or {}
    screen_width = int(profile.get("screen_width", SCREEN_WIDTH))
    screen_height = int(profile.get("screen_height", SCREEN_HEIGHT))
    js_heap_size_limit = int(profile.get("js_heap_size_limit", JS_HEAP_SIZE_LIMIT))
    hardware_concurrency = int(profile.get("hardware_concurrency", HARDWARE_CONCURRENCY))
    navigator_language = str(profile.get("navigator_language", _DEFAULT_NAVIGATOR_LANGUAGE))
    navigator_languages = list(profile.get("navigator_languages", _DEFAULT_NAVIGATOR_LANGUAGES))
    user_agent = str(profile.get("user_agent", USER_AGENT))
    build_id = profile.get("build_id")
    react_listening_key = str(profile.get("react_listening_key") or ("_reactListening" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=11))))
    react_container_key = str(profile.get("react_container_key") or ("__reactContainer$" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=11))))
    react_resources_key = str(profile.get("react_resources_key") or react_container_key.replace("__reactContainer$", "__reactResources$", 1))
    tz_offset = int(profile.get("timezone_offset_minutes", _DEFAULT_TZ_OFFSET))
    tz_name = str(profile.get("timezone_name", _DEFAULT_TZ_NAME))

    tz = timezone(timedelta(minutes=tz_offset))
    now = datetime.now(tz)
    sign = "+" if tz_offset >= 0 else "-"
    abs_minutes = abs(tz_offset)
    gmt = f"GMT{sign}{abs_minutes // 60:02d}{abs_minutes % 60:02d}"
    date_str = now.strftime(f"%a %b %d %Y %H:%M:%S {gmt} ({tz_name})")

    perf_now = random.uniform(1000, 8000)
    time_origin = time.time() * 1000 - perf_now
    sid = str(device_id)

    navigator_props = list(profile.get("navigator_proto_samples") or NAVIGATOR_PROTO_SAMPLES)
    document_keys = list(profile.get("document_key_samples") or DOCUMENT_KEY_SAMPLES)
    window_keys = list(profile.get("window_key_samples") or WINDOW_KEY_SAMPLES)
    window_flags = dict(WINDOW_FEATURE_FLAGS)
    window_flags.update(profile.get("window_feature_flags") or {})
    script_src_samples = list(profile.get("script_src_samples") or [f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js"])

    return [
        screen_width + screen_height,          # [0]
        date_str,                              # [1]
        js_heap_size_limit,                    # [2]
        attempt,                               # [3]
        user_agent,                            # [4]
        random.choice(script_src_samples),     # [5]
        build_id,                              # [6]
        navigator_language,                    # [7]
        ",".join(navigator_languages),         # [8]
        round(elapsed_ms) if elapsed_ms else random.randint(1, 100),  # [9]
        random.choice(navigator_props),        # [10]
        random.choice(document_keys + [react_listening_key, react_container_key, react_resources_key]),  # [11]
        random.choice(window_keys),            # [12]
        round(perf_now, 10),                   # [13]
        sid,                                   # [14]
        "",                                    # [15]
        hardware_concurrency,                  # [16]
        round(time_origin, 1),                 # [17]
        int(window_flags.get("ai", 0)),        # [18]
        int(window_flags.get("InstallTrigger", 0)),      # [19]
        int(window_flags.get("cache", 0)),               # [20]
        int(window_flags.get("data", 0)),                # [21]
        int(window_flags.get("solana", 0)),              # [22]
        int(window_flags.get("dump", 0)),                # [23]
        int(window_flags.get("requestIdleCallback", 0)),  # [24]
    ]


def encode_config(config: list) -> str:
    json_str = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(json_str.encode("utf-8")).decode("ascii")


def _imul(a: int, b: int) -> int:
    a = a & 0xFFFFFFFF
    b = b & 0xFFFFFFFF
    return (a * b) & 0xFFFFFFFF


def fnv1a_hash(text: str) -> str:
    """FNV-1a 32 位哈希（与 SDK 的 Math.imul 语义对齐）。"""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = _imul(h, 16777619) & 0xFFFFFFFF
    h ^= (h >> 16)
    h = _imul(h, 2246822507) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = _imul(h, 3266489909) & 0xFFFFFFFF
    h ^= (h >> 16)
    h = h & 0xFFFFFFFF
    return format(h, "08x")


def solve_proof_of_work(
    seed: str,
    difficulty: str,
    device_id: str,
    max_attempts: int = 500000,
    profile: dict | None = None,
) -> str:
    """PoW：fnv1a(seed + b64(config)) 前缀 <= difficulty 即命中。"""
    start_time = time.time() * 1000
    config = generate_fingerprint_data(device_id, attempt=0, elapsed_ms=0, profile=profile)
    diff_len = len(difficulty)
    for i in range(max_attempts):
        config[3] = i
        config[9] = round(time.time() * 1000 - start_time)
        encoded = encode_config(config)
        if fnv1a_hash(seed + encoded)[:diff_len] <= difficulty:
            return encoded + "~S"
    return "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + encode_config(["e"])


def generate_requirements_token(device_id: str, profile: dict | None = None) -> str:
    """requirements token（sentinel/req 的 p 字段），带 gAAAAAC 前缀。"""
    config = generate_fingerprint_data(device_id, attempt=1, elapsed_ms=0, profile=profile)
    return "gAAAAAC" + encode_config(config) + "~S"


def build_sentinel_request_body(p: str, device_id: str, flow: str) -> str:
    return json.dumps({"p": p, "id": device_id, "flow": flow}, separators=(",", ":"))


def build_sentinel_token_header(
    p: str,
    turnstile_token: str,
    sentinel_token: str,
    device_id: str,
    flow: str,
) -> str:
    return json.dumps(
        {"p": p, "t": turnstile_token or "", "c": sentinel_token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
