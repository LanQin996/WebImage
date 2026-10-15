from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import OpenAIBackendAPI


class _FakeSession:
    """假 curl_cffi 会话：只需要 headers。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        pass


class QuotaUnknownReadingTests(unittest.TestCase):
    """conversation/init 里没有 image_gen 读数时必须表达为"配额未知"，而不是"配额耗尽"。"""

    def make_api(self, init_payload: dict[str, object]) -> OpenAIBackendAPI:
        proxy_settings = SimpleNamespace(
            get_profile=lambda **kwargs: {},
            build_session_kwargs=lambda **kwargs: {},
            track_session=lambda *args, **kwargs: None,
        )
        accounts = SimpleNamespace(get_account=lambda token: {})
        for target, value in (
            ("services.openai_backend_api.proxy_settings", proxy_settings),
            ("services.openai_backend_api.account_service", accounts),
            ("services.openai_backend_api.requests.Session", _FakeSession),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        api = OpenAIBackendAPI("tok")
        api._get_me = lambda: {"email": "user@example.com", "id": "user-1"}
        api._get_conversation_init = lambda: init_payload
        api._get_default_account = lambda: {"plan_type": "plus"}
        return api

    def test_extract_quota_returns_none_when_image_gen_is_missing(self) -> None:
        self.assertEqual(
            OpenAIBackendAPI._extract_quota_and_restore_at([{"feature_name": "gpt-4", "remaining": 3}]),
            (None, None),
        )
        self.assertEqual(OpenAIBackendAPI._extract_quota_and_restore_at([]), (None, None))

    def test_degraded_probe_is_unknown_quota_not_rate_limited(self) -> None:
        api = self.make_api({"limits_progress": []})

        result = api.get_user_info()

        self.assertEqual(result["quota"], 0)
        self.assertEqual(result["limits_progress"], [])
        self.assertEqual(result["status"], "正常")

    def test_genuine_exhausted_reading_still_reports_rate_limited(self) -> None:
        api = self.make_api({"limits_progress": [{"feature_name": "image_gen", "remaining": 0}]})

        result = api.get_user_info()

        self.assertEqual(result["quota"], 0)
        self.assertEqual(result["limits_progress"], [{"feature_name": "image_gen", "remaining": 0}])
        self.assertEqual(result["status"], "限流")

    def test_present_image_gen_reading_is_reported_as_is(self) -> None:
        api = self.make_api({
            "limits_progress": [{"feature_name": "image_gen", "remaining": 17, "reset_after": "2026-10-17T00:00:00Z"}],
        })

        result = api.get_user_info()

        self.assertEqual(result["quota"], 17)
        self.assertEqual(result["restore_at"], "2026-10-17T00:00:00Z")
        self.assertEqual(result["status"], "正常")


if __name__ == "__main__":
    unittest.main()
