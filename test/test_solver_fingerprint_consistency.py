"""求解页面的浏览器身份必须与主服务请求指纹一致（伪装 404 的高概率成因）。

上游对网页生图启用了 Sentinel 握手评分：指纹自相矛盾（页面是 macOS Edge 148、请求头/TLS 却是
Windows Chrome 146）或握手环境与请求环境对不上时，不返回 403，而是统一回
``404 {"detail": "Model not found"}``。这里守住三件事：

- ``CdpPage.UA`` 与 ``services/openai_backend_api.py`` 的 ``FINGERPRINT_UA`` 字符级相等；
- ``Emulation.setUserAgentOverride`` 的 ``navigator.platform`` 与 UA 里的系统匹配；
- 按请求覆盖 UA 的链路（``POST /turnstile`` 的 ``user_agent`` → 默认 lane / 代理 lane 页面）与
  ``GET /health`` 的 ``user_agent`` 回读。

solver 脚本不是包内模块，用 importlib 按路径加载（同 test_sentinel_solver_script.py）；浏览器与页面
全部用假替身，不启动真实 Chromium。
"""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from services.openai_backend_api import FINGERPRINT_UA

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sentinel_solver.py"
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
PROXY_A = "http://alice:secretA@10.0.0.1:8001"
REQS = {"turnstile": {"dx": "abc"}}


def load_solver(name: str = "sentinel_solver_fingerprint_under_test") -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


solver = load_solver()


class FakeWs:
    """记录 CDP 指令的假 websocket：对每条指令回一个空 result。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv_text(self, timeout: float | None = None) -> str | None:
        return json.dumps({"id": self.sent[-1]["id"], "result": {}})

    def close(self) -> None:
        self.closed = True


def make_page(user_agent: str | None = None) -> tuple[Any, FakeWs]:
    ws = FakeWs()
    with mock.patch.object(solver, "SimpleWebSocket", lambda url, timeout=60.0: ws):
        page = solver.CdpPage(9333, user_agent, target_id="T1")
    return page, ws


def overrides(ws: FakeWs) -> list[dict]:
    return [message["params"] for message in ws.sent if message["method"] == "Emulation.setUserAgentOverride"]


class PageFingerprintTests(unittest.TestCase):
    def test_solver_default_ua_equals_main_service_fingerprint(self) -> None:
        self.assertEqual(solver.CdpPage.UA, FINGERPRINT_UA)

    def test_default_ua_is_windows_chrome_without_edge_brand(self) -> None:
        ua = solver.CdpPage.UA

        self.assertIn("(Windows NT 10.0; Win64; x64)", ua)
        self.assertIn("Chrome/146.0.0.0", ua)
        self.assertNotIn("Edg/", ua)
        self.assertNotIn("Macintosh", ua)

    def test_page_override_uses_platform_matching_the_ua(self) -> None:
        _, ws = make_page()
        calls = overrides(ws)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["userAgent"], FINGERPRINT_UA)
        # 真实 Chrome 在 Windows 上报的 navigator.platform 就是 Win32
        self.assertEqual(calls[0]["platform"], "Win32")
        self.assertEqual(calls[0]["acceptLanguage"], ACCEPT_LANGUAGE)

    def test_explicit_user_agent_argument_wins(self) -> None:
        page, ws = make_page(MAC_UA)

        self.assertEqual(page.user_agent, MAC_UA)
        self.assertEqual(overrides(ws)[0]["platform"], "MacIntel")

    def test_set_user_agent_switches_platform_and_skips_noop(self) -> None:
        page, ws = make_page()

        self.assertTrue(page.set_user_agent(MAC_UA))
        self.assertEqual(overrides(ws)[-1]["platform"], "MacIntel")
        self.assertFalse(page.set_user_agent(MAC_UA))                 # 同值不下发
        self.assertFalse(page.set_user_agent("   "))                  # 空值忽略，沿用当前 UA
        self.assertEqual(page.user_agent, MAC_UA)
        self.assertTrue(page.set_user_agent(FINGERPRINT_UA))
        self.assertEqual(overrides(ws)[-1], {"userAgent": FINGERPRINT_UA,
                                             "acceptLanguage": ACCEPT_LANGUAGE, "platform": "Win32"})
        self.assertEqual(len(overrides(ws)), 3)

    def test_platform_falls_back_to_windows_default(self) -> None:
        self.assertEqual(solver.platform_for_user_agent(""), "Win32")
        self.assertEqual(solver.platform_for_user_agent("SomeBot/1.0"), "Win32")
        self.assertEqual(solver.platform_for_user_agent("x Linux x"), "Linux x86_64")


class FakePage:
    """假页面：与真 CdpPage.set_user_agent 一样，同值不下发，只记录真正切过的 UA。"""

    def __init__(self, env: "FakeEnv", target_id: str | None) -> None:
        self.env = env
        self.target_id = target_id
        self.user_agent = solver.CdpPage.UA
        self.applied: list[str] = []
        self.set_calls = 0

    def set_user_agent(self, user_agent: str) -> bool:
        self.set_calls += 1
        if user_agent == self.user_agent:
            return False
        self.user_agent = user_agent
        self.applied.append(user_agent)
        return True

    def open(self, url: str) -> None:
        self.opened = url

    def evaluate(self, expression: str, timeout: float = 0) -> Any:
        if expression == "1+1":
            return 2
        result = self.env.token
        return result() if callable(result) else result

    def current_url(self) -> str:
        return "https://chatgpt.com/"

    def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self) -> None:
        self.ws_url = "ws://127.0.0.1/devtools/browser/fake"
        self.contexts: list[str] = []
        self.disposed: list[str] = []

    def ping(self) -> None:
        return None

    def create_context(self, proxy_server: str) -> str:
        self.contexts.append(proxy_server)
        return f"ctx-{len(self.contexts)}"

    def create_target(self, context_id: str) -> str:
        return f"tgt-{context_id}"

    def dispose_context(self, context_id: str) -> None:
        self.disposed.append(context_id)

    def close(self) -> None:
        return None


class FakeEnv:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.token: Any = "tok"
        self.browser = FakeBrowser()

    def page_factory(self, cdp_port: int, user_agent: str | None, target_id: str | None = None) -> FakePage:
        page = FakePage(self, target_id)
        self.pages.append(page)
        return page

    def browser_factory(self, cdp_port: int) -> FakeBrowser:
        return self.browser

    def lane_pages(self) -> list[FakePage]:
        return [page for page in self.pages if page.target_id]


def make_pool(env: FakeEnv, user_agent: str | None = None) -> Any:
    pool = solver.BrowserPool(9333, "https://chatgpt.com/", user_agent,
                              page_factory=env.page_factory, browser_factory=env.browser_factory)
    pool._cdp_alive = lambda: True
    return pool


class RequestScopedUserAgentTests(unittest.TestCase):
    """lane 与默认页面都会跨请求复用，UA 必须跟着每次求解走。"""

    def test_default_lane_applies_request_user_agent(self) -> None:
        env = FakeEnv()
        pool = make_pool(env)

        self.assertEqual(pool.solve(REQS, "p", user_agent=MAC_UA), (True, "tok"))
        page = env.pages[0]
        self.assertEqual(page.applied, [MAC_UA])
        self.assertEqual(page.user_agent, MAC_UA)
        self.assertEqual(pool.solve(REQS, "p", user_agent=FINGERPRINT_UA), (True, "tok"))
        self.assertEqual(page.applied, [MAC_UA, FINGERPRINT_UA])
        self.assertEqual(pool.solve(REQS, "p"), (True, "tok"))        # 不带 UA 时回到默认 UA，不是沿用上一次
        self.assertEqual(page.user_agent, FINGERPRINT_UA)
        self.assertEqual(page.applied, [MAC_UA, FINGERPRINT_UA])
        self.assertEqual(pool.solve(REQS, "p", user_agent=MAC_UA), (True, "tok"))
        self.assertEqual(pool.solve(REQS, "p"), (True, "tok"))
        self.assertEqual(page.applied, [MAC_UA, FINGERPRINT_UA, MAC_UA, FINGERPRINT_UA])
        self.assertEqual(len(env.pages), 1)                           # 页面复用，没有多开 tab

    def test_proxy_lane_applies_request_user_agent(self) -> None:
        env = FakeEnv()
        pool = make_pool(env)

        self.assertEqual(pool.solve(REQS, "p", proxy=PROXY_A, user_agent=MAC_UA), (True, "tok"))
        self.assertEqual([page.applied for page in env.lane_pages()], [[MAC_UA]])
        self.assertEqual([page.applied for page in env.pages if page.target_id is None], [])
        # 默认 UA 与页面初始 UA 相同：不该为它多花一次 CDP 往返
        self.assertEqual(pool.solve(REQS, "p", proxy=PROXY_A, user_agent=FINGERPRINT_UA), (True, "tok"))
        self.assertEqual([page.applied for page in env.lane_pages()], [[MAC_UA, FINGERPRINT_UA]])

    def test_vm_timeout_retry_reapplies_user_agent(self) -> None:
        env = FakeEnv()
        pool = make_pool(env)
        results = iter(["ERR:turnstile VM timed out after 12 steps", "tok"])
        env.token = lambda: next(results)                        # VM 超时重试时同样要带上请求 UA

        self.assertEqual(pool.solve(REQS, "p", user_agent=MAC_UA), (True, "tok"))
        page = env.pages[0]
        self.assertEqual(page.set_calls, 2)                      # 两次尝试都走 UA 设置
        self.assertEqual(page.applied, [MAC_UA])                 # 第二次同值，不再重复下发
        self.assertEqual(page.user_agent, MAC_UA)

    def test_page_without_setter_still_solves(self) -> None:
        class PlainPage:
            def __init__(self) -> None:
                self.scripts = 0

            def open(self, url: str) -> None:
                return None

            def evaluate(self, expression: str, timeout: float = 0) -> Any:
                if expression == "1+1":
                    return 2
                self.scripts += 1
                return "tok"

            def close(self) -> None:
                return None

        plain = PlainPage()
        pool = solver.BrowserPool(9333, "https://chatgpt.com/", page_factory=lambda *a, **k: plain)
        pool._cdp_alive = lambda: True

        self.assertEqual(pool.solve(REQS, "p", user_agent=MAC_UA), (True, "tok"))
        self.assertEqual(plain.scripts, 1)


class HandlerUserAgentTests(unittest.TestCase):
    KEY = "test-key"

    def setUp(self) -> None:
        self.env = FakeEnv()
        self.pool = make_pool(self.env)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), solver.make_handler(self.pool, self.KEY))
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _request(self, method: str, path: str, body: Any = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        head = {"Content-Type": "application/json", "X-Solver-Key": self.KEY}
        head.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_turnstile_body_user_agent_is_applied_to_the_page(self) -> None:
        status, data = self._request("POST", "/turnstile",
                                     {"requirements": REQS, "p": "seed", "user_agent": MAC_UA})

        self.assertEqual((status, data["ok"], data["lane"]), (200, True, "default"))
        self.assertEqual(self.env.pages[0].applied, [MAC_UA])

    def test_turnstile_without_user_agent_uses_the_solver_default(self) -> None:
        """缺省/空值都表示"用默认 UA"，不能残留上一次请求切过去的身份。"""
        bodies = ({"user_agent": MAC_UA}, {"user_agent": ""}, {"user_agent": MAC_UA}, {},
                  {"user_agent": "   "})
        for body in bodies:
            status, data = self._request("POST", "/turnstile", {"requirements": REQS, "p": "seed", **body})
            self.assertEqual((status, data["ok"]), (200, True))

        page = self.env.pages[0]
        self.assertEqual(page.applied, [MAC_UA, solver.CdpPage.UA, MAC_UA, solver.CdpPage.UA])
        self.assertEqual(page.user_agent, solver.CdpPage.UA)
        self.assertEqual(len(self.env.pages), 1)

    def test_turnstile_rejects_non_string_user_agent(self) -> None:
        status, data = self._request("POST", "/turnstile",
                                     {"requirements": REQS, "p": "seed", "user_agent": 123})

        self.assertEqual((status, data["ok"], data["error"]), (400, False, "user_agent must be a string"))
        self.assertEqual(self.env.pages, [])

    def test_v1_turnstile_alias_is_served(self) -> None:
        status, data = self._request("POST", "/v1/turnstile", {"requirements": REQS, "p": "seed"})

        self.assertEqual((status, data["ok"], data["turnstile"]), (200, True, "tok"))

    def test_bearer_key_is_accepted(self) -> None:
        status, data = self._request("POST", "/turnstile", {"requirements": REQS, "p": "seed"},
                                     headers={"X-Solver-Key": "", "Authorization": f"Bearer {self.KEY}"})
        self.assertEqual((status, data["ok"]), (200, True))

        status, data = self._request("POST", "/turnstile", {"requirements": REQS, "p": "seed"},
                                     headers={"X-Solver-Key": "", "Authorization": "Bearer wrong"})
        self.assertEqual((status, data), (401, {"ok": False, "error": "bad key"}))

    def test_bad_key_with_large_body_is_rejected_without_aborting(self) -> None:
        """鉴权失败也要先把请求体读完：请求体没读完就关连接，客户端会读到"连接被中止"（Windows 上偶发）。"""
        body = {"requirements": REQS, "p": "seed", "filler": "x" * (256 * 1024)}
        status, data = self._request("POST", "/turnstile", body, headers={"X-Solver-Key": "nope"})

        self.assertEqual((status, data), (401, {"ok": False, "error": "bad key"}))

    def test_unknown_post_path_returns_404(self) -> None:
        status, data = self._request("POST", "/v2/turnstile", {"requirements": REQS, "p": "seed"})

        self.assertEqual((status, data), (404, {"ok": False, "error": "not found"}))
        self.assertEqual(self.env.pages, [])

    def test_health_reports_default_user_agent(self) -> None:
        status, data = self._request("GET", "/health", headers={"X-Solver-Key": ""})

        self.assertEqual(status, 200)
        self.assertEqual(data["user_agent"], solver.CdpPage.UA)

    def test_health_reports_configured_user_agent(self) -> None:
        pool = make_pool(FakeEnv(), user_agent=MAC_UA)

        self.assertEqual(pool.current_user_agent(), MAC_UA)
        self.assertEqual(make_pool(FakeEnv()).current_user_agent(), solver.CdpPage.UA)


if __name__ == "__main__":
    unittest.main()
