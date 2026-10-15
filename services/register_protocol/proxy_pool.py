# -*- coding: utf-8 -*-
"""注册用代理池：多代理随机调度（区别于全局单代理）。

配置格式：一行一个代理，支持：
  - 完整 URL：http://user:pass@host:port / socks5h://user:pass@host:port
  - 简写：host:port:user:pass（默认 http）
  - 简写：host:port
"""
from __future__ import annotations

import logging
import random
import re
from urllib.parse import urlparse

from services.proxy_service import normalize_proxy_url

logger = logging.getLogger(__name__)

# 代理池只接受带 host:port 的合法形态；normalize_proxy_url 本身不做强校验，
# 若不在这里拦一道，用户误填的说明文字会被当成代理塞给 curl_cffi，
# 表现为注册时连接失败，且错误信息完全不指向"代理填错"这个根因。
_SCHEME_ALLOWLIST = {"http", "https", "socks", "socks4", "socks5", "socks5h"}
_HOST_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _is_usable_proxy(url: str) -> bool:
    """校验规范化后的代理 URL 是否可用（scheme 合法 + host + port）。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in _SCHEME_ALLOWLIST:
        return False
    host = parsed.hostname or ""
    if not host or not _HOST_RE.match(host):
        return False
    try:
        port = parsed.port
    except ValueError:
        # 端口非数字（如 http://host:abc）
        return False
    return bool(port and 0 < int(port) <= 65535)


def _ensure_scheme(line: str) -> str:
    """补全缺失的 scheme。

    ``user:pass@host:port`` 这种省略 scheme 的写法很常见（代理服务商直接给的
    就是这种串），但 curl_cffi 和 urlparse 都需要 scheme，否则整条会被当成
    非法代理丢弃。这里统一按 http 补全，用户想用 socks 就显式写 socks5h://。
    """
    if "://" in line:
        return line
    if "@" in line:
        return f"http://{line}"
    return line


def parse_proxy_pool(raw: str | list[str]) -> list[str]:
    """解析代理池文本/列表为规范化 URL 列表，去重、保序、剔除无效行。"""
    if isinstance(raw, list):
        lines = [str(item) for item in raw]
    else:
        lines = str(raw or "").replace("\r", "\n").replace(";", "\n").replace(",", "\n").split("\n")
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            normalized = normalize_proxy_url(_ensure_scheme(line))
        except Exception:
            normalized = ""
        if not normalized:
            logger.warning(f"[注册代理池] 跳过无法解析的代理行: {line[:60]}")
            continue
        if not _is_usable_proxy(normalized):
            logger.warning(f"[注册代理池] 跳过格式不合法（需 scheme://host:port）: {line[:60]}")
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def mask_proxy(url: str) -> str:
    """脱敏展示代理：保留用户名（含 sid 等区分标识）与 host:port，隐藏密码。

    代理池里多条线路常常共用一个 host:port，只打印 host 无法分辨实际用了哪条，
    排查时会失去全部信息量。
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = parsed.hostname or ""
    if not host:
        return url
    port = f":{parsed.port}" if parsed.port else ""
    user = parsed.username or ""
    return f"{user}@{host}{port}" if user else f"{host}{port}"


def pick_proxy(pool: list[str]) -> str | None:
    """从代理池随机挑一个；空池返回 None（直连）。"""
    if not pool:
        return None
    return random.choice(pool)


def pick_proxy_candidates(pool: list[str], count: int = 3) -> list[str]:
    """随机挑最多 count 条互不相同的代理，用作失败转移候选。

    商业代理池里单条线路的瞬时 TLS 抖动很常见（实测约 10%）。
    只挑一条且不做转移时，一次抖动就会让整次注册直接失败。
    """
    if not pool:
        return []
    size = max(1, min(int(count), len(pool)))
    return random.sample(pool, size)
