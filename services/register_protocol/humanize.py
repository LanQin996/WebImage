# -*- coding: utf-8 -*-
"""拟人化延迟（简化自 turb-gpt-free-register core/humanize.py）。"""
from __future__ import annotations

import random
import time

# 各类操作的 (最小, 最大) 秒数区间
_RANGES: dict[str, tuple[float, float]] = {
    "navigate": (0.6, 1.8),
    "api": (0.3, 1.0),
    "challenge": (0.5, 1.4),
    "otp_input": (1.2, 3.0),
    "form": (0.6, 1.6),
    "post_auth": (1.0, 2.5),
}


def delay(kind: str = "api") -> None:
    lo, hi = _RANGES.get(kind, _RANGES["api"])
    time.sleep(random.uniform(lo, hi))
