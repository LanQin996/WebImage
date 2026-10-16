from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.config import config
from services.register_runner import _is_valid
from services.storage.json_storage import JSONStorageBackend


def _image_gen(remaining: int) -> dict:
    return {"feature_name": "image_gen", "remaining": remaining}


class QuotaUnknownStateTests(unittest.TestCase):
    """「额度未知」态：quota == 0 且 limits_progress 为空。"""

    def setUp(self) -> None:
        self._auto_remove = config.data.get("auto_remove_rate_limited_accounts")
        config.data["auto_remove_rate_limited_accounts"] = False
        self._tmp = tempfile.TemporaryDirectory()
        self.service = AccountService(JSONStorageBackend(Path(self._tmp.name) / "accounts.json"))

    def tearDown(self) -> None:
        if self._auto_remove is None:
            config.data.pop("auto_remove_rate_limited_accounts", None)
        else:
            config.data["auto_remove_rate_limited_accounts"] = self._auto_remove
        self._tmp.cleanup()

    def test_unknown_quota_account_stays_eligible(self) -> None:
        self.service.add_accounts(["token-quota-unknown"])
        account = self.service.get_account("token-quota-unknown")

        self.assertEqual(account["quota"], 0)
        self.assertEqual(account["limits_progress"], [])
        self.assertFalse(self.service._quota_known(account))
        self.assertTrue(self.service._is_image_account_available(account))
        self.assertEqual(self.service._remaining_quota_locked("token-quota-unknown"), 1)
        self.assertEqual(self.service.list_unknown_quota_tokens(), ["token-quota-unknown"])
        self.assertEqual(self.service.unknown_quota_count(), 1)
        with mock.patch("services.account_service.image_runtime_repository.get_health_many", return_value={}):
            self.assertEqual(self.service.image_dynamic_capacity(), 1)

    def test_exhausted_account_is_still_unavailable(self) -> None:
        self.service.add_account_items([
            {
                "access_token": "token-quota-exhausted",
                "status": "正常",
                "quota": 1,
                "limits_progress": [_image_gen(1)],
            },
        ])
        self.service.mark_image_result("token-quota-exhausted", success=True)
        account = self.service.get_account("token-quota-exhausted")

        self.assertEqual(account["quota"], 0)
        self.assertEqual(account["status"], "限流")
        self.assertTrue(account["limits_progress"])
        self.assertTrue(self.service._quota_known(account))
        self.assertFalse(self.service._is_image_account_available(account))
        self.assertEqual(self.service._remaining_quota_locked("token-quota-exhausted"), 0)
        self.assertEqual(self.service.list_unknown_quota_tokens(), [])
        self.assertEqual(self.service.unknown_quota_count(), 0)

    def test_success_on_unknown_quota_does_not_mark_rate_limited(self) -> None:
        """额度未知时不能本地扣减：0 会被误判成耗尽，把刚出图的号封成限流。"""
        self.service.add_accounts(["token-success-unknown"])
        with mock.patch("services.account_service.image_runtime_repository"):
            updated = self.service.mark_image_result("token-success-unknown", success=True)

        self.assertIsNotNone(updated)
        self.assertEqual(updated["success"], 1)
        self.assertEqual(updated["quota"], 0)
        self.assertEqual(updated["status"], "正常")
        self.assertEqual(updated["limits_progress"], [])
        self.assertFalse(self.service._quota_known(updated))
        self.assertTrue(self.service._is_image_account_available(updated))

    def test_degraded_probe_does_not_overwrite_known_reading(self) -> None:
        self.service.add_account_items([{"access_token": "token-quota-known"}])
        self.service.update_account(
            "token-quota-known",
            {
                "status": "正常",
                "quota": 25,
                "limits_progress": [_image_gen(25)],
                "restore_at": "2026-10-16T00:00:00+08:00",
            },
        )

        updated = self.service.update_account(
            "token-quota-known",
            {"status": "正常", "quota": 0, "limits_progress": []},
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["quota"], 25)
        self.assertEqual(updated["limits_progress"], [_image_gen(25)])
        self.assertEqual(updated["restore_at"], "2026-10-16T00:00:00+08:00")
        self.assertEqual(updated["status"], "正常")
        self.assertTrue(self.service._quota_known(updated))

    def test_genuine_exhausted_reading_is_applied(self) -> None:
        self.service.add_account_items([{"access_token": "token-quota-zero"}])
        self.service.update_account(
            "token-quota-zero",
            {"status": "正常", "quota": 25, "limits_progress": [_image_gen(25)]},
        )

        updated = self.service.update_account(
            "token-quota-zero",
            {"status": "限流", "quota": 0, "limits_progress": [_image_gen(0)]},
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["quota"], 0)
        self.assertEqual(updated["limits_progress"], [_image_gen(0)])
        self.assertEqual(updated["status"], "限流")

    def test_keeper_treats_unknown_quota_as_valid(self) -> None:
        unknown = {"status": "正常", "quota": 0, "limits_progress": [], "source_type": "web"}
        exhausted = {"status": "正常", "quota": 0, "limits_progress": [_image_gen(0)], "source_type": "web"}

        self.assertTrue(_is_valid(unknown, 1, "web"))
        self.assertFalse(_is_valid(exhausted, 1, "web"))

    def test_limits_progress_without_image_gen_is_still_unknown(self) -> None:
        """降级响应可能带回别的 feature 但缺 image_gen，不能据此判定已耗尽。"""
        self.service.add_account_items([
            {
                "access_token": "token-no-image-gen",
                "status": "正常",
                "quota": 0,
                "limits_progress": [{"feature_name": "gpt-4", "remaining": 3}],
            },
        ])
        account = self.service.get_account("token-no-image-gen")

        self.assertFalse(self.service._quota_known(account))
        self.assertTrue(self.service._is_image_account_available(account))
        self.assertEqual(self.service._remaining_quota_locked("token-no-image-gen"), 1)
        self.assertEqual(self.service.list_unknown_quota_tokens(), ["token-no-image-gen"])
        self.assertEqual(self.service.unknown_quota_count(), 1)
        self.assertTrue(_is_valid(account, 1, "web"))
        # 对照：同样 quota=0，但带 image_gen 条目才算真耗尽
        self.assertTrue(self.service._quota_known({"quota": 0, "limits_progress": [_image_gen(0)]}))

    def test_refresh_failure_is_visible_on_account(self) -> None:
        self.service.add_accounts(["token-refresh-failed"])

        def _boom(*_args, **_kwargs):
            raise RuntimeError("image_gen missing in limits_progress")

        self.service.fetch_remote_info = _boom
        result = self.service.refresh_accounts(["token-refresh-failed"])
        account = self.service.get_account("token-refresh-failed")

        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(account["last_refresh_error"], "image_gen missing in limits_progress")
        self.assertIsNotNone(account["last_refresh_error_at"])


if __name__ == "__main__":
    unittest.main()
