# -*- coding: utf-8 -*-
"""OpenAI 协议链路使用的浏览器协议常量（对齐 turb-gpt-free-register config/openai_protocol.py）。"""

# 浏览器画像（协议头基线）
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
BROWSER_LOCALE = "en-US"
BROWSER_PLATFORM = "macOS"
BROWSER_PLATFORM_VERSION = "15.4.0"
BROWSER_ARCH = "arm"
BROWSER_MOBILE = "?0"
BROWSER_SEC_CH_UA = '"Chromium";v="146", "Google Chrome";v="146", "Not-A.Brand";v="99"'

# 前端构建版本
OPENAI_BUILD_ID = "prod-d4e40d432de549a66bf9feb61d43c262b258386f"
OAI_CLIENT_BUILD_NUMBER = "10762726"
OPENAI_CLIENT_VERSION = "prod-d4e40d432de549a66bf9feb61d43c262b258386f"
OPENAI_DEVICE_ID = "89f5edb4-e6af-4ef0-941d-9f9f07ef162e"
OAI_DEVICE_ID = "89f5edb4-e6af-4ef0-941d-9f9f07ef162e"

# Sentinel SDK 版本
SENTINEL_SV = "20260810913b"

# OAuth（chatgpt web）
OPENAI_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
OPENAI_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"
OPENAI_SCOPE = "openid email profile offline_access model.request"

# Platform OAuth（web/号池链路）
# 与 services/account_service.py 的 _OAUTH_CLIENT_ID 一致：注册后用同一条
# session 直接换 token，无需第二次登录，因此不触发 Codex 的手机号验证。
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
PLATFORM_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
PLATFORM_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_SCOPE = "openid profile email offline_access"
# auth0-spa-js 1.21.0 的 base64 标识，与号池刷新请求保持一致
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"

# Codex OAuth（CLIProxyAPI openai_auth.go）
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"

# 重试参数
NETWORK_PREFLIGHT_MAX_ATTEMPTS = 3
NETWORK_PREFLIGHT_RETRY_DELAY = 1.0
NETWORK_PREFLIGHT_TIMEOUT = 12.0

# 邮箱 OTP
OTP_MAX_WAIT_SECONDS = 120.0
OTP_POLL_INTERVAL_SECONDS = 2.0
OTP_SETTLE_SECONDS = 1.0
