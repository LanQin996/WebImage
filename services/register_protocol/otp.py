# -*- coding: utf-8 -*-
"""OTP 验证码抽取（对齐 turb-gpt-free-register core/otp_utils.py）。"""
from __future__ import annotations

import re

_OPENAI_HINTS = (
    "openai",
    "chatgpt",
    "verification code",
    "verify your email",
    "验证码",
    "確認コード",
    "codigo de verificacion",
    "code de verification",
)

_OTP_CONTEXT_WORDS = (
    "verification", "code", "otp", "verify",
    "验证码", "確認コード", "verification code",
    "codigo", "de verification",
)

# OpenAI 的 HTML 邮件里大量出现 6 位十六进制颜色（如 color:#202123），
# 它们与验证码同为 6 位"数字/字母"串。若不先剔除样式与标签属性，
# extract_otp 会把颜色值当成验证码，注册必然卡在 OTP 校验。
_STYLE_BLOCK_RE = re.compile(r"<(style|script|head)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_CSS_COLOR_WORD_RE = re.compile(
    r"\b(?:background|color|border|outline|fill|stroke|box-shadow|text-shadow)"
    r"[-a-z]*\s*:\s*[^;\"']*",
    re.I,
)


def clean_mail_text(text: str) -> str:
    """剥离 HTML/CSS 噪声，只留下人类可见文本，避免颜色值污染 OTP 候选。"""
    value = str(text or "")
    if not value:
        return ""
    if "<" in value and ">" in value:
        value = _STYLE_BLOCK_RE.sub(" ", value)
        value = _CSS_COLOR_WORD_RE.sub(" ", value)
        value = _HEX_COLOR_RE.sub(" ", value)
        value = _TAG_RE.sub(" ", value)
    else:
        value = _HEX_COLOR_RE.sub(" ", value)
    return value


def looks_like_openai_email(subject: str, sender: str, body: str = "") -> bool:
    """粗判断邮件是否来自 OpenAI / 与 OpenAI 验证相关。"""
    haystack = f"{sender}\n{subject}\n{body[:600]}".lower()
    return any(hint in haystack for hint in _OPENAI_HINTS)


def extract_otp(subject: str, body: str) -> str | None:
    """从邮件主题/正文中抽取 6 位数字验证码。

    策略：先剥离 HTML/CSS 噪声，再收集所有 6 位数字候选，优先取靠近上下文关键词
    （verification/code/验证码...）的候选；否则取第一个。
    """
    clean_subject = clean_mail_text(subject)
    clean_body = clean_mail_text(body)
    text = f"{clean_subject}\n{clean_body}"
    candidates = [(m.start(), m.group(0)) for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text)]
    if not candidates:
        return None
    lowered = text.lower()
    best_pos: int | None = None
    best_dist = float("inf")
    for pos, _code in candidates:
        for word in _OTP_CONTEXT_WORDS:
            idx = lowered.find(word)
            if idx < 0:
                continue
            dist = abs(pos - idx)
            if dist < best_dist:
                best_dist = dist
                best_pos = pos
    if best_pos is None:
        return candidates[0][1]
    for pos, code in candidates:
        if pos == best_pos:
            return code
    return candidates[0][1]
