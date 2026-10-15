# -*- coding: utf-8 -*-
"""浏览器指纹画像（移植自 turb-gpt-free-register config/browser.py，仅保留协议链路所需部分）。

原则：同一 BrowserSession 内稳定，不同 BrowserSession 可自然分散；
协议头、JS navigator/screen/timezone/client hints 三方一致。
"""
from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

CHROME_MAJOR = "146"
CHROME_FULL_VERSION = "146.0.0.0"
SAFARI_WEBKIT_VERSION = "537.36"
MAC_OS_UA_VERSION = "10_15_7"

IMPERSONATE = "chrome146"

NAVIGATOR_PLATFORM = "MacIntel"
NAVIGATOR_VENDOR = "Google Inc."
USER_AGENT_DATA_PLATFORM = "macOS"
USER_AGENT = (
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X {MAC_OS_UA_VERSION}) "
    f"AppleWebKit/{SAFARI_WEBKIT_VERSION} (KHTML, like Gecko) "
    f"Chrome/{CHROME_FULL_VERSION} Safari/{SAFARI_WEBKIT_VERSION}"
)

# 必须与 curl_cffi chrome146 实际发出的品牌顺序和 GREASE 品牌完全一致。
SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
SEC_CH_UA_FULL_VERSION_LIST = (
    '"Chromium";v="146.0.0.0", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.0.0"'
)
SEC_CH_UA_PLATFORM = '"macOS"'
SEC_CH_UA_PLATFORM_VERSION = '"15.7.0"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_ARCH = '"arm"'
SEC_CH_UA_BITNESS = '"64"'
SEC_CH_UA_MODEL = '""'
SEND_CLIENT_HINTS = True

# ---------- 语言 / 时区 ----------
DEFAULT_LOCALE_PROFILE = "us"
IP_GEO_TIMEOUT = 6.0
IP_GEO_ENDPOINTS = [
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
    "https://ipwho.is/",
]

COUNTRY_LOCALE_PROFILE_MAP = {
    "JP": "jp", "CN": "cn", "HK": "hk", "TW": "tw", "US": "us", "CA": "us",
    "SG": "sg", "GB": "gb", "AU": "gb", "DE": "de", "FR": "fr", "NL": "nl",
    "VN": "vn",
}

COUNTRY_LANGUAGE_TAG_MAP = {
    "TH": "th-TH", "ID": "id-ID", "MY": "ms-MY", "PH": "en-PH",
    "KR": "ko-KR", "IN": "en-IN", "BR": "pt-BR", "MX": "es-MX",
    "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT", "PL": "pl-PL",
    "RU": "ru-RU", "TR": "tr-TR", "AE": "ar-AE", "SA": "ar-SA",
    "ZA": "en-ZA", "NZ": "en-NZ", "IE": "en-IE", "AT": "de-AT",
    "CH": "de-CH", "BE": "nl-BE", "SE": "sv-SE", "NO": "nb-NO",
    "DK": "da-DK", "FI": "fi-FI", "CZ": "cs-CZ", "RO": "ro-RO",
    "HU": "hu-HU", "GR": "el-GR", "IL": "he-IL", "UA": "uk-UA",
}

BROWSER_LOCALE_PROFILES = {
    "jp": {"navigator_language": "ja-JP", "navigator_languages": ["ja-JP"], "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Tokyo", "timezone_offset_minutes": 9 * 60, "timezone_name": "Japan Standard Time"},
    "cn": {"navigator_language": "zh-CN", "navigator_languages": ["zh-CN"], "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Shanghai", "timezone_offset_minutes": 8 * 60, "timezone_name": "China Standard Time"},
    "us": {"navigator_language": "en-US", "navigator_languages": ["en-US"], "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/Los_Angeles", "timezone_offset_minutes": -7 * 60, "timezone_name": "Pacific Daylight Time"},
    "sg": {"navigator_language": "en-SG", "navigator_languages": ["en-SG"], "accept_language": "en-SG,en-US;q=0.9,en;q=0.8", "timezone_iana": "Asia/Singapore", "timezone_offset_minutes": 8 * 60, "timezone_name": "Singapore Standard Time"},
    "hk": {"navigator_language": "zh-HK", "navigator_languages": ["zh-HK"], "accept_language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6", "timezone_iana": "Asia/Hong_Kong", "timezone_offset_minutes": 8 * 60, "timezone_name": "Hong Kong Standard Time"},
    "tw": {"navigator_language": "zh-TW", "navigator_languages": ["zh-TW"], "accept_language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Taipei", "timezone_offset_minutes": 8 * 60, "timezone_name": "Taipei Standard Time"},
    "gb": {"navigator_language": "en-GB", "navigator_languages": ["en-GB"], "accept_language": "en-GB,en-US;q=0.9,en;q=0.8", "timezone_iana": "Europe/London", "timezone_offset_minutes": 1 * 60, "timezone_name": "British Summer Time"},
    "de": {"navigator_language": "de-DE", "navigator_languages": ["de-DE"], "accept_language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Berlin", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "fr": {"navigator_language": "fr-FR", "navigator_languages": ["fr-FR"], "accept_language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Paris", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "nl": {"navigator_language": "nl-NL", "navigator_languages": ["nl-NL"], "accept_language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Amsterdam", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "vn": {"navigator_language": "vi-VN", "navigator_languages": ["vi-VN", "vi", "en-US", "en"], "accept_language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Ho_Chi_Minh", "timezone_offset_minutes": 7 * 60, "timezone_name": "Indochina Time"},
}

TIMEZONE_NAME_BY_IANA = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Hong_Kong": "Hong Kong Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/Los_Angeles": "Pacific Daylight Time",
    "America/New_York": "Eastern Daylight Time",
    "America/Chicago": "Central Daylight Time",
    "America/Denver": "Mountain Daylight Time",
    "Europe/London": "British Summer Time",
    "Europe/Berlin": "Central European Summer Time",
    "Europe/Paris": "Central European Summer Time",
    "Europe/Amsterdam": "Central European Summer Time",
    "Asia/Ho_Chi_Minh": "Indochina Time",
    "Asia/Bangkok": "Indochina Time",
}

# ---------- Sentinel / JS VM 环境 ----------
SCREEN_WIDTH = 1680
SCREEN_HEIGHT = 1050
HARDWARE_CONCURRENCY = 6
JS_HEAP_SIZE_LIMIT = 4395630592
DEVICE_MEMORY = 8

# 这些列表必须与 node_assets/sentinel-runner.js 的 createBrowserContext 保持一致。
NAVIGATOR_PROTO_SAMPLES = [
    "createAuctionNonce−function createAuctionNonce() { [native code] }",
    "clearOriginJoinedAdInterestGroups−function clearOriginJoinedAdInterestGroups() { [native code] }",
    "updateAdInterestGroups−function updateAdInterestGroups() { [native code] }",
    "canLoadAdAuctionFencedFrame−function canLoadAdAuctionFencedFrame() { [native code] }",
    "gpu−[object GPU]",
    "getBattery−function getBattery() { [native code] }",
    "getGamepads−function getGamepads() { [native code] }",
    "javaEnabled−function javaEnabled() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "vibrate−function vibrate() { [native code] }",
    "login−[object NavigatorLogin]",
]
DOCUMENT_KEY_SAMPLES = [
    "currentScript", "scripts", "cookie", "URL", "documentURI", "referrer",
    "title", "characterSet", "charset", "compatMode", "contentType", "readyState",
    "visibilityState", "hidden", "hasFocus", "documentElement", "body",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "getElementsByTagName", "createElement",
]
WINDOW_KEY_SAMPLES = [
    "window", "self", "top", "parent", "frames", "navigator", "screen", "location",
    "localStorage", "sessionStorage", "history", "innerWidth", "innerHeight",
    "outerWidth", "outerHeight", "devicePixelRatio", "chrome", "performance", "crypto",
    "TextEncoder", "URL", "URLSearchParams", "AbortController",
    "locationbar", "scrollX", "scrollY", "ondevicemotion",
    "requestAnimationFrame", "queueMicrotask", "onfocus", "onblur", "onpageshow",
]

SCRIPT_SRC_SAMPLES = [
    "https://accounts.google.com/gsi/client",
    "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload",
    "https://sentinel.openai.com/sentinel/20260810913b/sdk.js",
]

WINDOW_FEATURE_FLAGS = {
    "ai": 0,
    "InstallTrigger": 0,
    "cache": 0,
    "data": 0,
    "solana": 0,
    "dump": 0,
    "requestIdleCallback": 0,
}

REQUEST_TIMEOUT = 30

HAR_CAPTURE_BASE_PROFILE = {
    "screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 6,
    "device_memory": 8, "js_heap_size_limit": 4395630592, "device_pixel_ratio": 2,
}

BROWSER_PROFILE_POOL = [
    HAR_CAPTURE_BASE_PROFILE,
    {"screen_width": 1440, "screen_height": 900, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1512, "screen_height": 982, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1680, "screen_height": 1050, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1728, "screen_height": 1117, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 1800, "screen_height": 1169, "hardware_concurrency": 10, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
    {"screen_width": 2056, "screen_height": 1329, "hardware_concurrency": 12, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 2},
]


def _offset_minutes_for_timezone(tz_name: str, default: int) -> int:
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 60)
    except Exception:
        pass
    return int(default)


def _build_locale_from_geo(geo: dict | None, auto_from_ip: bool = True) -> dict:
    country = str((geo or {}).get("country") or (geo or {}).get("country_code") or "").upper()
    key = COUNTRY_LOCALE_PROFILE_MAP.get(country, DEFAULT_LOCALE_PROFILE) if (geo and auto_from_ip) else DEFAULT_LOCALE_PROFILE
    resolved_profile = key
    locale = dict(BROWSER_LOCALE_PROFILES.get(key, BROWSER_LOCALE_PROFILES[DEFAULT_LOCALE_PROFILE]))
    if geo and auto_from_ip:
        if country not in COUNTRY_LOCALE_PROFILE_MAP:
            language_tag = COUNTRY_LANGUAGE_TAG_MAP.get(country, "en-US")
            resolved_profile = f"geo:{country.lower() or 'unknown'}"
            base_language = language_tag.split("-", 1)[0]
            languages = [language_tag]
            if base_language != language_tag:
                languages.append(base_language)
            if base_language != "en":
                languages.extend(["en-US", "en"])
                accept_language = f"{language_tag},{base_language};q=0.9,en-US;q=0.8,en;q=0.7"
            else:
                languages.append("en")
                accept_language = f"{language_tag},en;q=0.9"
            locale.update({
                "navigator_language": language_tag,
                "navigator_languages": list(dict.fromkeys(languages)),
                "accept_language": accept_language,
            })
        tz = str(geo.get("timezone") or "").strip()
        if tz:
            locale["timezone_iana"] = tz
            locale["timezone_offset_minutes"] = _offset_minutes_for_timezone(tz, int(locale["timezone_offset_minutes"]))
            locale["timezone_name"] = TIMEZONE_NAME_BY_IANA.get(tz, locale.get("timezone_name", ""))
    locale["locale_profile"] = resolved_profile
    return locale


def build_browser_environment(
    geo: dict | None = None,
    base_profile: dict | None = None,
    *,
    build_id: str = "",
    auto_locale_from_ip: bool = True,
) -> dict:
    """构建完整浏览器环境画像，作为所有指纹字段的单一数据源。"""
    locale = _build_locale_from_geo(geo, auto_from_ip=auto_locale_from_ip)
    profile = dict(base_profile or random.choice(BROWSER_PROFILE_POOL))
    profile.update({
        "locale_profile": locale.get("locale_profile", DEFAULT_LOCALE_PROFILE),
        "geo": dict(geo or {}),
        "timezone_iana": locale["timezone_iana"],
        "timezone_offset_minutes": int(locale["timezone_offset_minutes"]),
        "timezone_name": locale["timezone_name"],
        "navigator_language": locale["navigator_language"],
        "navigator_languages": list(locale["navigator_languages"]),
        "accept_language": locale["accept_language"],
        "browser_family": "chrome",
        "browser_os": "macOS",
        "navigator_platform": NAVIGATOR_PLATFORM,
        "navigator_vendor": NAVIGATOR_VENDOR,
        "user_agent_data_platform": USER_AGENT_DATA_PLATFORM,
        "chrome_major": CHROME_MAJOR,
        "chrome_full_version": CHROME_FULL_VERSION,
        "user_agent": USER_AGENT,
        "send_client_hints": SEND_CLIENT_HINTS,
        "sec_ch_ua": SEC_CH_UA,
        "sec_ch_ua_platform": SEC_CH_UA_PLATFORM,
        "sec_ch_ua_platform_version": SEC_CH_UA_PLATFORM_VERSION,
        "sec_ch_ua_arch": SEC_CH_UA_ARCH,
        "sec_ch_ua_bitness": SEC_CH_UA_BITNESS,
        "sec_ch_ua_model": SEC_CH_UA_MODEL,
        "sec_ch_ua_full_version_list": SEC_CH_UA_FULL_VERSION_LIST,
        "sec_ch_ua_mobile": SEC_CH_UA_MOBILE,
        "navigator_proto_samples": list(NAVIGATOR_PROTO_SAMPLES),
        "document_key_samples": list(DOCUMENT_KEY_SAMPLES),
        "window_key_samples": list(WINDOW_KEY_SAMPLES),
        "script_src_samples": list(SCRIPT_SRC_SAMPLES),
        "window_feature_flags": dict(WINDOW_FEATURE_FLAGS),
        "build_id": build_id,
    })
    # Sentinel VM 与 HTTP 指纹必须使用同一组 screen/window/viewport/GPU 画像。
    screen_width = int(profile.get("screen_width", 1680))
    screen_height = int(profile.get("screen_height", 1050))
    profile.setdefault("screen_avail_width", screen_width)
    profile.setdefault("screen_avail_height", max(0, screen_height - 25))
    profile.setdefault("color_depth", 24)
    profile.setdefault("outer_width", int(profile["screen_avail_width"]))
    profile.setdefault("outer_height", int(profile["screen_avail_height"]))
    profile.setdefault("viewport_width", int(profile["outer_width"]))
    profile.setdefault("viewport_height", max(0, int(profile["outer_height"]) - 87))
    cores = int(profile.get("hardware_concurrency", 8))
    chip = "Apple M2 Max" if cores >= 12 else "Apple M2 Pro" if cores >= 10 else "Apple M2"
    profile.setdefault("webgl_vendor", "Google Inc. (Apple)")
    profile.setdefault(
        "webgl_renderer",
        f"ANGLE (Apple, ANGLE Metal Renderer: {chip}, Unspecified Version)",
    )
    return profile
