"""握手一致性回归：UA 单一来源、首页预热、模型名安全映射。

上游对网页生图启用 Sentinel 握手评分，握手不合格时统一回
`404 {"detail": "Model not found"}` 这种伪装错误，所以这里守住三件事：
请求头 / PoW / solver 浏览器环境共用同一个 UA、每个 Session 真访问过首页、
配置里的上游模型名必须是生图 slug。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.config import DEFAULT_PROXY_RUNTIME, ConfigStore, config
from services.openai_backend_api import (
    FINGERPRINT_UA,
    OpenAIBackendAPI,
    _BOOTSTRAP_CACHE,
)
from services.proxy_service import ProxySettingsStore
from utils.helper import UpstreamHTTPError

POW_SDK_URL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
HOMEPAGE = f'<html data-build="build-1"><body><script src="{POW_SDK_URL}"></script></body></html>'
CUSTOM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


class _ProxyConfig:
    """没有配置任何代理：出口固定是直连。"""

    def get_proxy_settings(self) -> str:
        return ""

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return copy.deepcopy(DEFAULT_PROXY_RUNTIME)


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = HOMEPAGE, payload: object = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}
        self._payload = payload if payload is not None else {}

    def json(self) -> object:
        return self._payload


class _FakeSession:
    """假 curl_cffi 会话：只需要 headers；请求一律走被替换的 ``_fetch``。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        pass


class _BackendTestBase(unittest.TestCase):
    """构造不碰网络的 OpenAIBackendAPI：代理 / 账号 / curl_cffi 会话全部替换成假的。"""

    def make_api(self, account: dict[str, object] | None = None) -> OpenAIBackendAPI:
        resolved = copy.deepcopy(account or {})
        accounts = SimpleNamespace(get_account=lambda token: resolved)
        for target, value in (
            ("services.openai_backend_api.proxy_settings", ProxySettingsStore(_ProxyConfig())),
            ("services.openai_backend_api.account_service", accounts),
            ("services.openai_backend_api.requests.Session", _FakeSession),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return OpenAIBackendAPI("tok")


class ImageModelMappingTests(unittest.TestCase):
    """`gpt-image-2` 映射到的配置值必须是网页生图 slug，否则上游回伪装 404。"""

    @staticmethod
    def _settings(model: str) -> tuple[str, str]:
        return OpenAIBackendAPI._image_model_settings(OpenAIBackendAPI.__new__(OpenAIBackendAPI), model)

    def configure_upstream(self, upstream_model: str, thinking_effort: str = "auto") -> None:
        patcher = patch.dict(config.data, {
            "default_upstream_model_name": upstream_model,
            "default_thinking_effort": thinking_effort,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_configured_auto_is_used_unchanged(self) -> None:
        self.configure_upstream("auto", thinking_effort="standard")

        self.assertEqual(self._settings("gpt-image-2"), ("auto", "standard"))

    def test_configured_auto_with_effort_suffix_is_not_rewritten(self) -> None:
        self.configure_upstream("auto-standard")

        self.assertEqual(self._settings("gpt-image-2"), ("auto", "standard"))

    def test_configured_web_image_model_is_used_unchanged(self) -> None:
        self.configure_upstream("gpt-image-2.5-flare")

        self.assertEqual(self._settings("gpt-image-2"), ("gpt-image-2.5-flare", ""))

    def test_configured_web_image_model_with_effort_suffix_keeps_working(self) -> None:
        self.configure_upstream("gpt-image-2.5-sunburst-extended")

        self.assertEqual(self._settings("gpt-image-2"), ("gpt-image-2.5-sunburst", "extended"))

    def test_text_model_name_falls_back_to_auto_and_drops_thinking_effort(self) -> None:
        self.configure_upstream("gpt-5-5", thinking_effort="extended")

        with patch("services.openai_backend_api.logger.warning") as warning:
            settings = self._settings("gpt-image-2")

        self.assertEqual(settings, ("auto", ""))
        events = [call.args[0] for call in warning.call_args_list]
        self.assertEqual(events, [{
            "event": "image_model_mapping_fallback",
            "requested_model": "gpt-image-2",
            "ignored_upstream_model": "gpt-5-5",
            "fallback_model": "auto",
        }])

    def test_builtin_and_other_text_model_names_also_fall_back(self) -> None:
        for value in ("gpt-6", "gpt-5-5-thinking", "o3"):
            with self.subTest(upstream_model=value):
                self.configure_upstream(value)
                with patch("services.openai_backend_api.logger.warning") as warning:
                    self.assertEqual(self._settings("gpt-image-2"), ("auto", ""))
                self.assertTrue(any(
                    call.args[0].get("event") == "image_model_mapping_fallback"
                    for call in warning.call_args_list
                ))

    def test_explicit_web_image_models_are_never_downgraded(self) -> None:
        self.configure_upstream("gpt-5-5")

        with patch("services.openai_backend_api.logger.warning") as warning:
            flare = self._settings("gpt-image-2.5-flare")
            sunburst = self._settings("gpt-image-2.5-sunburst")
            codex = self._settings("codex-gpt-image-2")

        self.assertEqual(flare, ("gpt-image-2.5-flare", ""))
        self.assertEqual(sunburst, ("gpt-image-2.5-sunburst", ""))
        self.assertEqual(codex, ("codex-gpt-image-2", ""))
        warning.assert_not_called()


class ImageRequestModelTests(unittest.TestCase):
    """端到端确认发往故障接口 `/backend-api/f/conversation` 的模型名不会是文本模型。"""

    def make_store(self, **overrides: object) -> ConfigStore:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        path = Path(tmp_dir.name) / "config.json"
        path.write_text(json.dumps({"auth-key": "test-auth", **overrides}), encoding="utf-8")
        return ConfigStore(path)

    def sent_payload(self, store: ConfigStore, model: str) -> dict[str, object]:
        posts: list[tuple[str, dict[str, object]]] = []
        api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        api.base_url = "https://chatgpt.com"
        api.session = SimpleNamespace(
            headers={},
            post=lambda url, **kwargs: posts.append((url, kwargs)) or SimpleNamespace(status_code=200),
        )
        requirements = SimpleNamespace(token="sentinel", proof_token="")

        with patch("services.openai_backend_api.config", store):
            api._start_image_generation("draw a cat", requirements, "conduit", model)

        [(url, kwargs)] = posts
        self.assertEqual(url, "https://chatgpt.com/backend-api/f/conversation")
        return kwargs["json"]

    def test_text_model_configuration_is_replaced_by_auto(self) -> None:
        store = self.make_store(default_upstream_model_name="gpt-5-5")

        with patch("services.openai_backend_api.logger.warning"):
            payload = self.sent_payload(store, "gpt-image-2")

        self.assertEqual(payload["model"], "auto")
        self.assertNotIn("thinking_effort", payload)

    def test_explicit_web_model_still_reaches_the_upstream_unchanged(self) -> None:
        store = self.make_store(default_upstream_model_name="gpt-5-5")

        payload = self.sent_payload(store, "gpt-image-2.5-flare")

        self.assertEqual(payload["model"], "gpt-image-2.5-flare")
        self.assertNotIn("thinking_effort", payload)


class BootstrapWarmupTests(_BackendTestBase):
    """PoW 结果可以按出口共享，Cookie 不行：每个 Session 都要真访问一次首页。"""

    def cache_key(self, api: OpenAIBackendAPI) -> str:
        return str(api._egress_profile().proxy_url or "direct")

    def cached_entry(self, sources: list[str], data_build: str) -> tuple[float, list[str], str]:
        return (time.time(), list(sources), data_build)

    def test_pow_cache_hit_still_visits_the_homepage_only_once(self) -> None:
        api = self.make_api()
        key = self.cache_key(api)
        self.assertEqual(key, "direct")

        with (
            patch.dict(_BOOTSTRAP_CACHE, {key: self.cached_entry(["https://cdn.example/sdk.js"], "cached-build")}, clear=True),
            patch.object(api, "_fetch", return_value=_FakeResponse()) as fetch,
        ):
            api._bootstrap()
            first_round = fetch.call_count
            api._bootstrap()
            second_round = fetch.call_count

        self.assertEqual((first_round, second_round), (1, 1))
        self.assertEqual(fetch.call_args.args, ("get", "https://chatgpt.com/"))
        self.assertEqual(fetch.call_args.kwargs["headers"]["User-Agent"], api.user_agent)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 30)
        # 命中的是 PoW 缓存：不再重新解析首页 HTML
        self.assertEqual(api.pow_script_sources, ["https://cdn.example/sdk.js"])
        self.assertEqual(api.pow_data_build, "cached-build")
        self.assertTrue(api._bootstrapped)

    def test_new_session_warms_the_homepage_even_with_a_warm_pow_cache(self) -> None:
        first = self.make_api()
        key = self.cache_key(first)
        with patch.dict(_BOOTSTRAP_CACHE, {}, clear=True):
            with patch.object(first, "_fetch", return_value=_FakeResponse()) as first_fetch:
                first._bootstrap()
                first._bootstrap()
            self.assertIn(key, _BOOTSTRAP_CACHE)  # 出口级 PoW 缓存已被第一个实例写热
            second = self.make_api()
            with patch.object(second, "_fetch", return_value=_FakeResponse()) as second_fetch:
                second._bootstrap()

        self.assertEqual(first_fetch.call_count, 1)
        self.assertEqual(second_fetch.call_count, 1)

    def test_cache_miss_parses_the_homepage_and_fills_the_cache(self) -> None:
        api = self.make_api()
        key = self.cache_key(api)

        with patch.dict(_BOOTSTRAP_CACHE, {}, clear=True), patch.object(api, "_fetch", return_value=_FakeResponse()):
            api._bootstrap()
            cached = _BOOTSTRAP_CACHE.get(key)

        self.assertEqual(api.pow_script_sources, [POW_SDK_URL])
        self.assertEqual(api.pow_data_build, "build-1")
        self.assertIsNotNone(cached)
        self.assertEqual((cached[1], cached[2]), ([POW_SDK_URL], "build-1"))

    def test_failed_homepage_visit_is_not_marked_as_bootstrapped(self) -> None:
        api = self.make_api()
        key = self.cache_key(api)
        responses = [_FakeResponse(403), _FakeResponse()]

        with (
            patch.dict(_BOOTSTRAP_CACHE, {key: self.cached_entry([POW_SDK_URL], "cached-build")}, clear=True),
            patch.object(api, "_fetch", side_effect=responses) as fetch,
        ):
            with self.assertRaises(UpstreamHTTPError):
                api._bootstrap()
            self.assertFalse(api._bootstrapped)
            api._bootstrap()

        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(api._bootstrapped)


class HandshakeUserAgentTests(_BackendTestBase):
    """请求头 / PoW / solver 必须共用同一个 UA，身份混用会被判为握手不合格。"""

    def test_handshake_user_agent_is_the_session_user_agent(self) -> None:
        api = self.make_api({"fp": {"user-agent": CUSTOM_UA}})

        self.assertEqual(api._sentinel_handshake_ua(), CUSTOM_UA)
        self.assertEqual(api.session.headers["User-Agent"], CUSTOM_UA)
        self.assertEqual(api._bootstrap_headers()["User-Agent"], CUSTOM_UA)

    def test_handshake_user_agent_falls_back_to_the_chrome_fingerprint(self) -> None:
        api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)

        self.assertEqual(api._sentinel_handshake_ua(), FINGERPRINT_UA)

    def test_solver_is_called_with_the_handshake_user_agent(self) -> None:
        api = self.make_api({"fp": {"user-agent": CUSTOM_UA}})
        captured: dict[str, object] = {}

        def fake_solve(requirements: object, p_token: str, **kwargs: object) -> str:
            captured.update(kwargs)
            captured["p_token"] = p_token
            return "TURNSTILE-TOKEN"

        with patch("services.openai_backend_api.sentinel_solver_service.solve_turnstile", fake_solve):
            token = api._solve_turnstile({"turnstile": {"required": True, "dx": "dx"}}, "p-token")

        self.assertEqual(token, "TURNSTILE-TOKEN")
        self.assertEqual(captured["p_token"], "p-token")
        self.assertEqual(captured["user_agent"], CUSTOM_UA)
        self.assertEqual(captured["user_agent"], api.user_agent)
        self.assertEqual(captured["user_agent"], api.session.headers["User-Agent"])

    def test_requirements_prepare_and_proof_tokens_use_the_handshake_user_agent(self) -> None:
        api = self.make_api({"fp": {"user-agent": CUSTOM_UA}})
        prepare = _FakeResponse(payload={
            "prepare_token": "prepare-token",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "difficulty"},
            "turnstile": {},
        })
        finalize = _FakeResponse(payload={"token": "chat-requirements-token"})

        with (
            patch("services.openai_backend_api.build_legacy_requirements_token", return_value="p") as legacy,
            patch("services.openai_backend_api.build_proof_token", return_value="proof") as proof,
            patch.object(api, "_fetch", side_effect=[prepare, finalize]),
        ):
            requirements = api._get_chat_requirements()

        self.assertEqual(requirements.token, "chat-requirements-token")
        self.assertEqual(requirements.proof_token, "proof")
        self.assertEqual(legacy.call_args.args[0], CUSTOM_UA)
        self.assertEqual(proof.call_args.args[2], CUSTOM_UA)
