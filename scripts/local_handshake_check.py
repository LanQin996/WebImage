"""本地端到端联调：匿名实例（无账号）真机跑通 Sentinel 握手链路。

用途（只做验证，不参与线上逻辑）：

    # 1. 先起本机 solver（真实 Chrome + Clash 代理）
    python scripts/sentinel_solver.py --port 8788 --cdp-port 9333 --key local-debug-key \
        --chrome "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" \
        --profile-dir "%TEMP%/webimage-solver-profile" --proxy http://127.0.0.1:7890
    # 2. 再跑本脚本
    python scripts/local_handshake_check.py [--proxy http://127.0.0.1:7890]

做的事情：
- 只在内存里把 ``sentinel_solver.url`` 指向本机 8788（不落盘，config.json 不变）；
- 只在内存里给 ``config.data["proxy"]`` 设出口（匿名实例本身没有账号代理，直连访问不到 chatgpt.com）；
- 用 ``OpenAIBackendAPI("")`` 走匿名链路：``_bootstrap()`` → ``_get_chat_requirements()``；
- 断言首页 GET 通过 Cloudflare、requirements token 非空、turnstile 是否必需、
  握手 UA 与 solver ``/health`` 的 UA 是否一致，并打印实际出口 IP（cdn-cgi/trace）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.config import config  # noqa: E402
from services.sentinel_solver_service import sentinel_solver_service  # noqa: E402


def patch_solver_settings(url: str, key: str, timeout: float) -> dict:
    """只在内存里改 solver 配置（config.json 不动）。"""
    previous = config.data.get("sentinel_solver")
    config.data["sentinel_solver"] = {
        "enabled": True, "url": url, "key": key, "timeout_seconds": timeout,
    }
    return {"previous": previous}


def patch_proxy(proxy: str) -> dict:
    """只在内存里设全局出口（匿名实例没有账号代理，默认直连）。"""
    previous = config.data.get("proxy")
    config.data["proxy"] = proxy
    return {"previous": previous}


def solver_health(url: str) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=10) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--solver-url", default="http://127.0.0.1:8788")
    parser.add_argument("--solver-key", default="local-debug-key")
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    patch_solver_settings(args.solver_url, args.solver_key, args.timeout)
    if args.proxy:
        patch_proxy(args.proxy)

    health = solver_health(args.solver_url)
    solver_ua = str(health.get("user_agent") or "")

    from services.openai_backend_api import OpenAIBackendAPI

    # 观察点 1：实际发往 solver 的请求体（确认 user_agent 被带过去）
    solver_calls: list[dict] = []
    original_post = sentinel_solver_service._post_json

    def recording_post(url, payload, settings, timeout):
        try:
            solver_calls.append({"url": url, "body": json.loads(payload.decode("utf-8"))})
        except Exception:
            solver_calls.append({"url": url, "body": {}})
        return original_post(url, payload, settings, timeout)

    sentinel_solver_service._post_json = recording_post  # type: ignore[method-assign]

    # 观察点 2：HTTP 往返（首页 / prepare / finalize 的状态码）
    fetches: list[dict] = []
    original_fetch = OpenAIBackendAPI._fetch

    def recording_fetch(self, method, url, **kwargs):
        response = original_fetch(self, method, url, **kwargs)
        fetches.append({"method": method, "url": url.split("?")[0], "status": response.status_code,
                        "cf_mitigated": bool(getattr(response, "headers", {}).get("cf-mitigated"))})
        return response

    OpenAIBackendAPI._fetch = recording_fetch  # type: ignore[method-assign]

    # 观察点 3：prepare 响应里 turnstile 是否必需
    prepare_payload: dict = {}
    original_solve = OpenAIBackendAPI._solve_turnstile

    def recording_solve(self, requirements_payload, source_p):
        prepare_payload.update(requirements_payload)
        return original_solve(self, requirements_payload, source_p)

    OpenAIBackendAPI._solve_turnstile = recording_solve  # type: ignore[method-assign]

    api = OpenAIBackendAPI("")
    profile = api._egress_profile()
    report: dict = {
        "solver_health": {"ok": health.get("ok"), "browser": health.get("browser")},
        "solver_ua": solver_ua,
        "main_egress": {"proxy_source": profile.proxy_source, "proxy_url": profile.proxy_url},
        "steps": [],
    }
    try:
        api._bootstrap()
        report["steps"].append({"step": "bootstrap", "ok": True})

        trace = api.session.get("https://chatgpt.com/cdn-cgi/trace", timeout=30).text
        report["egress_trace"] = {key: value for key, value in
                                 (line.split("=", 1) for line in trace.splitlines() if "=" in line)
                                 if key in {"ip", "loc", "colo", "http"}}

        handshake_ua = api._sentinel_handshake_ua()
        report["handshake_ua"] = handshake_ua
        report["ua_match"] = handshake_ua == solver_ua

        requirements = api._get_chat_requirements()
        required = bool((prepare_payload.get("turnstile") or {}).get("required"))
        report["steps"].append({"step": "chat_requirements", "ok": True})
        report["turnstile"] = {
            "required_by_upstream": required,
            "has_dx": bool((prepare_payload.get("turnstile") or {}).get("dx")),
            "token_len": len(requirements.turnstile_token or ""),
            "from_solver": bool(solver_calls),
        }
        report["requirements"] = {
            "token_len": len(requirements.token or ""),
            "proof_token_len": len(requirements.proof_token or ""),
            "so_token_len": len(requirements.so_token or ""),
        }
        report["solver_call"] = {
            "url": solver_calls[-1]["url"] if solver_calls else "",
            "user_agent_sent": (solver_calls[-1]["body"].get("user_agent") if solver_calls else ""),
            "proxy_sent": (solver_calls[-1]["body"].get("proxy", "") if solver_calls else ""),
            "has_requirements": bool(solver_calls and solver_calls[-1]["body"].get("requirements")),
        }
        report["http"] = fetches
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
        report["http"] = fetches
    finally:
        api.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (not report.get("error") and report.get("ua_match")
          and report.get("requirements", {}).get("token_len", 0) > 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
