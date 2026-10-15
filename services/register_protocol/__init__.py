"""Node 协议注册驱动（纯 HTTP，curl_cffi + Node sentinel）。

移植自 turb-gpt-free-register 的 protocol 驱动。只保留服务端可跑的协议链路，
不含浏览器指纹（roxy/cloak/browser_use/skyvern）。邮箱对接自部署
cloudflare_temp_email Worker，支持代理池随机调度。
"""
