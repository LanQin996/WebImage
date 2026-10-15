from __future__ import annotations

import base64
import importlib.util
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from services.config import DEFAULT_SENTINEL_SOLVER, _normalize_sentinel_solver_settings
from services.sentinel_solver_service import SentinelSolverService
from utils.pow import _js_date_string

SOLVER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sentinel_solver.py"


class FakeConfig:
    def __init__(self, settings: dict | None) -> None:
        self.settings = settings

    def get_sentinel_solver_settings(self) -> dict | None:
        return self.settings


def make_service(**overrides: object) -> SentinelSolverService:
    settings = dict(DEFAULT_SENTINEL_SOLVER)
    settings.update(overrides)
    service = SentinelSolverService()
    service.settings = lambda: dict(settings)      # type: ignore[method-assign]
    return service


SYSTEM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class SentinelSolverSettingsTests(unittest.TestCase):
    def test_defaults_are_disabled_and_empty(self) -> None:
        self.assertEqual(
            _normalize_sentinel_solver_settings(None),
            {"enabled": False, "url": "", "key": "", "timeout_seconds": 180},
        )

    def test_url_is_trimmed_and_timeout_has_a_floor(self) -> None:
        normalized = _normalize_sentinel_solver_settings(
            {"enabled": 1, "url": "  http://127.0.0.1:8788/  ", "key": " s3cret ", "timeout_seconds": 0}
        )

        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["url"], "http://127.0.0.1:8788")
        self.assertEqual(normalized["key"], "s3cret")
        self.assertEqual(normalized["timeout_seconds"], 1)


class SentinelSolverServiceTests(unittest.TestCase):
    def test_disabled_without_url_even_when_enabled(self) -> None:
        self.assertFalse(make_service(enabled=True, url="").enabled())
        self.assertFalse(make_service(enabled=False, url="http://127.0.0.1:8788").enabled())
        self.assertTrue(make_service(enabled=True, url="http://127.0.0.1:8788").enabled())

    def test_solve_returns_none_when_disabled(self) -> None:
        service = make_service(enabled=False, url="http://127.0.0.1:8788")

        self.assertIsNone(service.solve_turnstile({"turnstile": {"dx": "x"}}, "p"))

    def test_solve_posts_requirements_and_key_then_returns_token(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="test-key")
        captured: dict = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self) -> bytes:
                return json.dumps({"ok": True, "turnstile": "TOKEN"}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen):
            token = service.solve_turnstile({"turnstile": {"dx": "d"}, "persona": "chatgpt-paid"}, "p-token")

        self.assertEqual(token, "TOKEN")
        self.assertEqual(captured["url"], "http://127.0.0.1:8788/turnstile")
        self.assertEqual(captured["headers"].get("X-solver-key"), "test-key")
        self.assertEqual(captured["body"], {"requirements": {"turnstile": {"dx": "d"}, "persona": "chatgpt-paid"},
                                            "p": "p-token"})
        self.assertEqual(captured["timeout"], 180)

    def test_solve_returns_none_when_solver_reports_failure(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self) -> bytes:
                return json.dumps({"ok": False, "error": "vm timeout"}).encode()

        with patch("services.sentinel_solver_service.urllib.request.urlopen", lambda *a, **k: FakeResponse()):
            self.assertIsNone(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"))

    def test_solve_returns_none_on_network_error(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")

        with patch("services.sentinel_solver_service.urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"))

    def test_no_key_configured_means_no_key_header(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="")

        self.assertNotIn("X-Solver-Key", service._headers(service.settings()))


class SentinelSolverUserAgentTests(unittest.TestCase):
    """求解页面的 UA 必须跟着主服务请求指纹走，否则 Sentinel 握手会因指纹不一致被拒（伪装 404）。"""

    def _patch_health(self, reply: dict | Exception) -> tuple[list[dict], object]:
        calls: list[dict] = []

        def fake_urlopen(request, timeout=None):
            calls.append({"url": request.full_url, "timeout": timeout})
            if isinstance(reply, Exception):
                raise reply
            return FakeResponse(reply)

        return calls, patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen)

    def test_user_agent_reads_health_and_caches_success(self) -> None:
        calls, patcher = self._patch_health({"ok": True, "user_agent": SYSTEM_UA})
        service = make_service(enabled=True, url="http://127.0.0.1:8788")

        with patcher, patch("services.sentinel_solver_service.time.monotonic") as clock:
            clock.return_value = 1_000.0
            self.assertEqual(service.user_agent(), SYSTEM_UA)
            clock.return_value = 1_599.0
            self.assertEqual(service.user_agent(), SYSTEM_UA)          # 命中缓存，不再请求 /health
            clock.return_value = 1_601.0
            self.assertEqual(service.user_agent(), SYSTEM_UA)          # 超过 600s 后重新查询

        self.assertEqual([call["url"] for call in calls], ["http://127.0.0.1:8788/health"] * 2)

    def test_user_agent_caches_failure_for_a_shorter_time(self) -> None:
        calls, patcher = self._patch_health({"ok": False, "error": "browser not reachable"})
        service = make_service(enabled=True, url="http://127.0.0.1:8788")

        with patcher, patch("services.sentinel_solver_service.logger") as logger, \
                patch("services.sentinel_solver_service.time.monotonic") as clock:
            clock.return_value = 2_000.0
            self.assertEqual(service.user_agent(), "")
            clock.return_value = 2_029.0
            self.assertEqual(service.user_agent(), "")                 # 30s 内的失败结果直接用缓存
            clock.return_value = 2_031.0
            self.assertEqual(service.user_agent(), "")

        self.assertEqual(len(calls), 2)
        self.assertEqual([call.args[0]["event"] for call in logger.warning.call_args_list],
                         ["sentinel_solver_user_agent_unavailable", "sentinel_solver_user_agent_unavailable"])

    def test_user_agent_is_empty_without_calling_health_when_disabled(self) -> None:
        for service in (make_service(enabled=False, url="http://127.0.0.1:8788"),
                        make_service(enabled=True, url=""),
                        make_service()):
            with self.subTest(settings=service.settings()):
                with patch("services.sentinel_solver_service.urllib.request.urlopen",
                           side_effect=AssertionError("不该发起请求")):
                    self.assertEqual(service.user_agent(), "")

    def test_user_agent_cache_is_keyed_by_url(self) -> None:
        settings = dict(DEFAULT_SENTINEL_SOLVER, enabled=True, url="http://a:8788")
        service = SentinelSolverService()
        service.settings = lambda: dict(settings)                      # type: ignore[method-assign]
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            return FakeResponse({"ok": True, "user_agent": f"ua-of-{request.full_url.split('//')[1].split('/')[0]}"})

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen), \
                patch("services.sentinel_solver_service.time.monotonic", return_value=10.0):
            self.assertEqual(service.user_agent(), "ua-of-a:8788")
            settings["url"] = "http://b:8788"                          # 换求解器后必须重新读 UA
            self.assertEqual(service.user_agent(), "ua-of-b:8788")

        self.assertEqual(seen, ["http://a:8788/health", "http://b:8788/health"])

    def test_health_timeout_is_capped_and_respects_a_smaller_setting(self) -> None:
        calls, patcher = self._patch_health({"ok": True})
        with patcher:
            make_service(enabled=True, url="http://127.0.0.1:8788", timeout_seconds=180).health()
            make_service(enabled=True, url="http://127.0.0.1:8788", timeout_seconds=5).health()

        self.assertEqual([call["timeout"] for call in calls], [10.0, 5.0])


class SentinelSolverProtocolTests(unittest.TestCase):
    """与不同版本 solver 的协议兼容：请求体字段、鉴权头、端点路径、token 字段名。"""

    def test_solve_sends_user_agent_only_when_set(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")
        bodies: list[dict] = []

        def fake_urlopen(request, timeout=None):
            bodies.append(json.loads(request.data.decode()))
            return FakeResponse({"ok": True, "turnstile": "T"})

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p", user_agent=SYSTEM_UA), "T")
            self.assertEqual(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p", user_agent="  "), "T")

        self.assertEqual(bodies[0], {"requirements": {"turnstile": {"dx": "d"}}, "p": "p", "user_agent": SYSTEM_UA})
        self.assertNotIn("user_agent", bodies[1])

    def test_solve_falls_back_to_v1_endpoint_on_404(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if not request.full_url.endswith("/v1/turnstile"):
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            return FakeResponse({"ok": True, "turnstile": "T"})

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"), "T")

        self.assertEqual(seen, ["http://127.0.0.1:8788/turnstile", "http://127.0.0.1:8788/v1/turnstile"])

    def test_solve_gives_up_when_both_endpoints_are_missing(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen), \
                patch("services.sentinel_solver_service.logger") as logger:
            self.assertIsNone(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"))

        self.assertEqual(len(seen), 2)
        warnings = [call.args[0] for call in logger.warning.call_args_list]
        self.assertEqual([warning["event"] for warning in warnings], ["sentinel_solver_http_error"])
        self.assertEqual((warnings[0]["status"], warnings[0]["path"]), (404, "/v1/turnstile"))

    def test_solve_does_not_retry_other_http_errors(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 500, "Boom", {}, None)

        with patch("services.sentinel_solver_service.urllib.request.urlopen", fake_urlopen):
            self.assertIsNone(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"))

        self.assertEqual(seen, ["http://127.0.0.1:8788/turnstile"])

    def test_solve_accepts_token_field(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="k")

        with patch("services.sentinel_solver_service.urllib.request.urlopen",
                   lambda *a, **k: FakeResponse({"ok": True, "token": "T2"})):
            self.assertEqual(service.solve_turnstile({"turnstile": {"dx": "d"}}, "p"), "T2")

    def test_key_is_sent_as_both_headers(self) -> None:
        service = make_service(enabled=True, url="http://127.0.0.1:8788", key="s3cret")
        headers = service._headers(service.settings())

        self.assertEqual(headers["X-Solver-Key"], "s3cret")
        self.assertEqual(headers["Authorization"], "Bearer s3cret")
        self.assertNotIn("Authorization", service._headers(service.settings(), with_key=False))
        empty = make_service(enabled=True, url="http://127.0.0.1:8788", key="")
        self.assertNotIn("Authorization", empty._headers(empty.settings()))


class SentinelDateStringTests(unittest.TestCase):
    """指纹里的 `"" + new Date`：UTC 或非 ASCII 时区名都会被上游判为不可信（404 Model not found）。"""

    def test_converts_to_utc_plus_8_with_browser_style_name(self) -> None:
        moment = datetime(2026, 10, 15, 6, 7, 8, tzinfo=timezone.utc)

        self.assertEqual(_js_date_string(moment), "Thu Oct 15 2026 14:07:08 GMT+0800 (China Standard Time)")

    def test_ignores_process_timezone(self) -> None:
        # 容器默认 TZ=UTC，结果仍必须是东八区且为 ASCII
        value = _js_date_string()

        self.assertTrue(value.endswith(" GMT+0800 (China Standard Time)"), value)
        self.assertTrue(value.isascii(), value)


def _load_solver_script():
    spec = importlib.util.spec_from_file_location("sentinel_solver_under_test", SOLVER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class _EchoServer:
    """CONNECT 隧道的目标：收到什么回什么。"""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)


def _start_upstream_proxy(expected_auth: str, seen: list[str]) -> ThreadingHTTPServer:
    """要求 Proxy-Authorization 的上游代理：CONNECT 透传，普通 http 直接回显请求目标。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            seen.append(self.headers.get("Proxy-Authorization", ""))
            if seen[-1] == expected_auth:
                return True
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_CONNECT(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            host, port = self.path.rsplit(":", 1)
            target = socket.create_connection((host, int(port)), timeout=5)
            self.send_response(200, "Connection established")
            self.end_headers()
            threading.Thread(target=_pump, args=(target, self.connection), daemon=True).start()
            _pump(self.connection, target)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            body = f"via-upstream {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class PageProxyRelayTests(unittest.TestCase):
    """Chrome 不接受带凭据的代理：solver 在本机起转发，只用标准库（镜像里没有 curl_cffi）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.solver = _load_solver_script()

    def setUp(self) -> None:
        self.seen: list[str] = []
        self.expected_auth = "Basic " + base64.b64encode(b"user:p@ss").decode()
        upstream = _start_upstream_proxy(self.expected_auth, self.seen)
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        self.echo = _EchoServer()
        self.addCleanup(self.echo.sock.close)
        # 凭据按 URL 编码书写（@ -> %40），转发时要解码
        proxy = f"http://user:p%40ss@127.0.0.1:{upstream.server_address[1]}"
        with patch.dict(sys.modules, {"curl_cffi": None, "curl_cffi.requests": None}):
            self.local, stop = self.solver.make_page_proxy(proxy)
        self.assertTrue(self.local.startswith("http://127.0.0.1:"), self.local)
        self.addCleanup(stop)

    def test_https_tunnel_goes_through_upstream_with_credentials(self) -> None:
        relay = urlparse(self.local)
        with socket.create_connection((relay.hostname, relay.port), timeout=5) as client:
            target = f"127.0.0.1:{self.echo.port}"
            client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(4096)
            self.assertIn(b" 200 ", head.split(b"\r\n", 1)[0] + b" ")
            client.sendall(b"ping")
            echoed = b""
            while len(echoed) < 4:
                echoed += client.recv(4)

        self.assertEqual(echoed, b"ping")
        self.assertEqual(self.seen, [self.expected_auth])

    def test_plain_http_is_forwarded_with_credentials(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": self.local}))
        with opener.open("http://example.invalid/hello?x=1", timeout=5) as response:
            body = response.read()

        self.assertEqual(body, b"via-upstream http://example.invalid/hello?x=1")
        self.assertEqual(self.seen, [self.expected_auth])

    def test_proxy_without_credentials_is_handed_to_chrome_as_is(self) -> None:
        self.assertEqual(self.solver.make_page_proxy("http://10.0.0.1:8080"), ("http://10.0.0.1:8080", None))
        self.assertEqual(self.solver.make_page_proxy(""), ("", None))


if __name__ == "__main__":
    unittest.main()
