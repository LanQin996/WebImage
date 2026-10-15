from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService, ImageQuotaBusyError
from services.image_runtime_repository import ImageRuntimeRepository
from services.protocol import conversation
from services.protocol.conversation import (
    IMAGE_MODEL_NOT_FOUND_COOLDOWN_SECS,
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    is_cloudflare_challenge_error,
    is_image_model_not_found_error,
)
from services.storage.json_storage import JSONStorageBackend
from utils.helper import UpstreamHTTPError


MODEL_NOT_FOUND_BODY = {"detail": "Model not found"}
CHALLENGE_HTML = "<html><head><title>Just a moment...</title></head></html>"


class ImageModelNotFoundDetectorTests(unittest.TestCase):
    """伪装 404 识别：只认图片对话接口上精确的 404 + Model not found。"""

    def test_matches_conversation_and_prepare_contexts(self) -> None:
        for context in ("/backend-api/f/conversation", "/backend-api/f/conversation/prepare"):
            with self.subTest(context=context):
                error = UpstreamHTTPError(context, 404, MODEL_NOT_FOUND_BODY)
                self.assertTrue(is_image_model_not_found_error(error))

    def test_other_404_is_not_matched(self) -> None:
        # 普通 404（会话不存在、body 不是 dict、detail 不等于 Model not found）不能被当成模型名问题
        for context, body in (
            ("/backend-api/conversation/abc", {"detail": "Conversation not found"}),
            ("/backend-api/f/conversation", {"detail": "Not found"}),
            ("/backend-api/f/conversation", "<html>404</html>"),
            ("/backend-api/f/conversation", None),
            ("image_download", MODEL_NOT_FOUND_BODY),
        ):
            with self.subTest(context=context, body=body):
                self.assertFalse(is_image_model_not_found_error(UpstreamHTTPError(context, 404, body)))

    def test_conversation_like_context_without_model_detail_is_not_matched(self) -> None:
        # 只有图片对话接口本身（含 /prepare）算命中，其它 conversation 子路径不算
        error = UpstreamHTTPError("/backend-api/conversation/abc", 404, MODEL_NOT_FOUND_BODY)
        self.assertFalse(is_image_model_not_found_error(error))

    def test_status_and_error_type_must_match(self) -> None:
        self.assertFalse(
            is_image_model_not_found_error(UpstreamHTTPError("/backend-api/f/conversation/prepare", 403, MODEL_NOT_FOUND_BODY))
        )
        self.assertFalse(
            is_image_model_not_found_error(UpstreamHTTPError("/backend-api/f/conversation", 200, MODEL_NOT_FOUND_BODY))
        )
        self.assertFalse(is_image_model_not_found_error("status=404, body={'detail': 'Model not found'}"))
        self.assertFalse(is_image_model_not_found_error(RuntimeError("Model not found")))
        self.assertFalse(is_image_model_not_found_error(None))


class CloudflareChallengeDetectorTests(unittest.TestCase):
    """Cloudflare 挑战页识别：结构化字段优先，字符串回退保持可用。"""

    def test_structured_403_with_html_body_matches(self) -> None:
        error = UpstreamHTTPError("/backend-api/f/conversation", 403, CHALLENGE_HTML)
        self.assertTrue(is_cloudflare_challenge_error(error))

    def test_string_fallback_matches_helper_message_format(self) -> None:
        # helper 的拼接格式是 "{context} failed: status=403, body=..."，旧字符串匹配必须继续生效
        error = UpstreamHTTPError("/backend-api/f/conversation", 403, CHALLENGE_HTML)
        self.assertTrue(is_cloudflare_challenge_error(str(error)))

    def test_non_challenge_403_is_not_matched(self) -> None:
        for error in (
            UpstreamHTTPError("/backend-api/f/conversation", 403, {"detail": "Forbidden"}),
            UpstreamHTTPError("/backend-api/f/conversation", 200, CHALLENGE_HTML),
            "status=403, body={'detail': 'Forbidden'}",
            "",
            None,
        ):
            with self.subTest(error=repr(error)):
                self.assertFalse(is_cloudflare_challenge_error(error))


class _ImageAccountPool:
    """按调用顺序轮换 token 的账号池，记录每次健康上报。"""

    def __init__(self, tokens: list[str], *, busy_after: bool = False) -> None:
        self._tokens = list(tokens)
        self.busy_after = busy_after
        self.marks: list[tuple[str, bool, str, float | None]] = []
        self.used: list[str] = []

    def get_available_access_token(self, **kwargs: Any) -> str:
        if not self._tokens:
            raise ImageQuotaBusyError("all image accounts are busy")
        token = self._tokens.pop(0)
        self.used.append(token)
        return token

    def get_account(self, token: str) -> dict[str, str]:
        return {"email": f"{token}@example.com"}

    def mark_image_result(self, token: str, success: bool, **kwargs: Any) -> None:
        self.marks.append(
            (token, success, str(kwargs.get("failure_kind")), kwargs.get("cooldown_secs"))
        )

    def invalidate_image_validation_cache(self, token: str) -> None:
        return None

    def refresh_access_token(self, token: str, **kwargs: Any) -> str:
        return ""

    def remove_invalid_token(self, token: str, *args: Any) -> None:
        return None


class _NoopBackend:
    def __init__(self, access_token: str = "") -> None:
        self.progress_callback = None

    def close(self) -> None:
        return None


class ImageModelNotFoundHandlingTests(unittest.TestCase):
    """图片接口收到伪装 404：不计 transient 失败，短冷却换号重试，耗尽后报清原因。"""

    def run_task(
        self,
        tokens: list[str],
        *,
        failures: int,
        model: str = "gpt-image-2",
        capture_logs: bool = False,
    ) -> tuple[Any, _ImageAccountPool, list[float], list[dict[str, Any]]]:
        accounts = _ImageAccountPool(tokens)
        attempts = {"count": 0}
        logs: list[dict[str, Any]] = []

        def fake_stream(backend: Any, request: Any, index: int, total: int):
            attempts["count"] += 1
            if attempts["count"] <= failures:
                raise UpstreamHTTPError("/backend-api/f/conversation", 404, MODEL_NOT_FOUND_BODY)
            yield ImageOutput(
                kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}],
            )

        patchers = [
            patch.object(conversation, "account_service", accounts),
            patch.object(conversation, "OpenAIBackendAPI", _NoopBackend),
            patch.object(conversation, "stream_image_outputs", fake_stream),
            patch.object(conversation, "_remove_image_conversation_later", lambda *args, **kwargs: None),
        ]
        if capture_logs:
            logger_patcher = patch.object(conversation, "logger")
            logger = logger_patcher.start()
            self.addCleanup(logger_patcher.stop)
            logger.warning.side_effect = lambda payload, *args, **kwargs: logs.append(dict(payload))
            logger.info.side_effect = lambda *args, **kwargs: None
            logger.debug.side_effect = lambda *args, **kwargs: None
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(conversation.time, "sleep") as sleep:
            try:
                result: Any = conversation._generate_single_image(
                    ConversationRequest(model=model, prompt="cat"), 1, 1,
                )
            except conversation.ImageGenerationError as exc:
                result = exc
        self.assertEqual(attempts["count"], len(accounts.used))
        return result, accounts, [call.args[0] for call in sleep.call_args_list], logs

    def test_model_not_found_cools_down_account_and_switches(self) -> None:
        result, accounts, waits, _ = self.run_task(
            ["tok-1", "tok-2", "tok-3"], failures=2,
        )

        self.assertIsInstance(result, list)
        self.assertEqual(accounts.used, ["tok-1", "tok-2", "tok-3"], "伪装 404 必须换账号重试")
        self.assertEqual(
            accounts.marks,
            [
                ("tok-1", False, "model_unavailable", IMAGE_MODEL_NOT_FOUND_COOLDOWN_SECS),
                ("tok-2", False, "model_unavailable", IMAGE_MODEL_NOT_FOUND_COOLDOWN_SECS),
                ("tok-3", True, "transient", None),
            ],
        )
        self.assertEqual(waits, [], "换号重试不需要退避等待")

    def test_model_not_found_is_not_counted_as_transient_failure(self) -> None:
        _, accounts, _, _ = self.run_task(["tok-1", "tok-2"], failures=1)

        failure_marks = [item for item in accounts.marks if not item[1]]
        self.assertTrue(failure_marks)
        for token, _success, failure_kind, cooldown_secs in failure_marks:
            with self.subTest(token=token):
                self.assertNotEqual(failure_kind, "transient")
                self.assertEqual(failure_kind, "model_unavailable")
                self.assertEqual(cooldown_secs, IMAGE_MODEL_NOT_FOUND_COOLDOWN_SECS)

    def test_exhausted_retries_report_model_not_found(self) -> None:
        result, accounts, _, _ = self.run_task(
            [f"tok-{i}" for i in range(10)], failures=10,
        )

        self.assertIsInstance(result, ImageGenerationError)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.error_type, "server_error")
        self.assertEqual(result.code, "upstream_model_not_found")
        self.assertIn("Model not found", str(result))
        self.assertIn("gpt-image-2", str(result))
        self.assertIn(f"{accounts.used[-1]}@example.com", str(result))
        self.assertEqual(len(accounts.used), conversation.MAX_IMAGE_MODEL_NOT_FOUND_RETRIES + 1)
        self.assertTrue(all(item[2] == "model_unavailable" for item in accounts.marks))

    def test_model_not_found_warning_is_structured(self) -> None:
        _, accounts, _, logs = self.run_task(["tok-1", "tok-2"], failures=1, capture_logs=True)

        records = [item for item in logs if item.get("event") == "image_model_not_found"]
        self.assertTrue(records)
        record = records[0]
        self.assertEqual(record["request_token"], "tok-1")
        self.assertEqual(record["account_email"], "tok-1@example.com")
        self.assertEqual(record["index"], 1)
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["model"], "gpt-image-2")

    def test_single_account_pool_reports_model_not_found_instead_of_429(self) -> None:
        # 池里只有一个账号：冷却后没有可用账号，也必须回报真实原因而不是 429
        result, accounts, _, _ = self.run_task(["tok-1"], failures=10)

        self.assertIsInstance(result, ImageGenerationError)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.code, "upstream_model_not_found")
        self.assertIn("Model not found", str(result))
        self.assertEqual(len(accounts.used), 1)


class ImageCloudflareChallengeStillNeutralTests(unittest.TestCase):
    """Cloudflare 403 仍按 neutral + 退避重试处理，未被伪装 404 分支抢走。"""

    def run_task(self, failures: int) -> tuple[Any, _ImageAccountPool, list[float]]:
        accounts = _ImageAccountPool([f"tok-{i}" for i in range(10)])
        attempts = {"count": 0}

        def fake_stream(backend: Any, request: Any, index: int, total: int):
            attempts["count"] += 1
            if attempts["count"] <= failures:
                raise UpstreamHTTPError("/backend-api/f/conversation", 403, CHALLENGE_HTML)
            yield ImageOutput(
                kind="result", model=request.model, index=index, total=total, data=[{"b64_json": "x"}],
            )

        for target, value in (
            ("account_service", accounts),
            ("OpenAIBackendAPI", _NoopBackend),
            ("stream_image_outputs", fake_stream),
            ("_remove_image_conversation_later", lambda *args, **kwargs: None),
        ):
            patcher = patch.object(conversation, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(conversation.time, "sleep") as sleep:
            try:
                result: Any = conversation._generate_single_image(
                    ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1,
                )
            except conversation.ImageGenerationError as exc:
                result = exc
        return result, accounts, [call.args[0] for call in sleep.call_args_list]

    def test_transient_challenge_is_retried_with_backoff_and_neutral_mark(self) -> None:
        result, accounts, waits = self.run_task(failures=1)

        self.assertIsInstance(result, list)
        # 挑战页只做 neutral 上报（不冷却账号），重试时账号由调度器正常轮换
        self.assertEqual(accounts.marks, [("tok-0", False, "neutral", None), ("tok-1", True, "transient", None)])
        self.assertEqual(waits, [5.0])

    def test_persistent_challenge_reports_cloudflare(self) -> None:
        result, accounts, waits = self.run_task(failures=10)

        self.assertIsInstance(result, ImageGenerationError)
        self.assertIn("Cloudflare challenge", str(result))
        self.assertEqual(
            accounts.marks,
            [
                ("tok-0", False, "neutral", None),
                ("tok-1", False, "neutral", None),
                ("tok-2", False, "neutral", None),
            ],
        )
        self.assertEqual(waits, [5.0, 10.0])


class ImageModelUnavailableCooldownTests(unittest.TestCase):
    """伪装 404 的账号冷却复用既有健康熔断字段（state=open + cooldown_until），不计失败。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ImageRuntimeRepository(Path(self._tmp.name) / "runtime.db")
        repo_patcher = patch("services.account_service.image_runtime_repository", self.repo)
        repo_patcher.start()
        self.addCleanup(repo_patcher.stop)
        self.service = AccountService(JSONStorageBackend(Path(self._tmp.name) / "accounts.json"))
        self.service.add_account_items([
            {"access_token": "tok-1", "status": "正常", "quota": 5, "type": "Plus"},
            {"access_token": "tok-2", "status": "正常", "quota": 5, "type": "Plus"},
        ])

    def _ready_tokens(self) -> list[str]:
        health_map = self.repo.get_health_many(["tok-1", "tok-2"])
        return self.service._list_ready_candidate_tokens(health_map=health_map)

    def test_cooldown_opens_health_without_penalising_scores(self) -> None:
        before = self.repo.get_health("tok-1")

        self.service.mark_image_result(
            "tok-1", False, failure_kind="model_unavailable", error="Model not found", cooldown_secs=600.0,
        )

        after = self.repo.get_health("tok-1")
        self.assertEqual(after["health_state"], "open")
        self.assertAlmostEqual(float(after["health_cooldown_until"]), time.time() + 600.0, delta=30.0)
        self.assertEqual(after["health_score"], before["health_score"], "伪装 404 不应降低健康分")
        self.assertEqual(after["health_success_rate"], before["health_success_rate"])
        self.assertEqual(after["health_consecutive_failures"], 0, "伪装 404 不计 transient 失败")
        self.assertEqual(after["health_last_error"], "Model not found")

    def test_transient_failure_still_penalises_health(self) -> None:
        # 对照：普通 transient 失败照旧扣分并累计连续失败
        self.service.mark_image_result("tok-2", False, failure_kind="transient", error="boom")

        health = self.repo.get_health("tok-2")
        self.assertEqual(health["health_consecutive_failures"], 1)
        self.assertLess(int(health["health_score"]), 100)

    def test_cooldown_sidelines_account_only_for_its_window(self) -> None:
        self.service.mark_image_result(
            "tok-1", False, failure_kind="model_unavailable", cooldown_secs=0.05,
        )
        self.assertEqual(self._ready_tokens(), ["tok-2"], "冷却期内该账号必须被调度器跳过")

        time.sleep(0.1)
        self.assertEqual(self.repo.get_health("tok-1")["health_state"], "half_open")
        self.assertEqual(sorted(self._ready_tokens()), ["tok-1", "tok-2"], "冷却结束后回到 half_open 探测")


class ImageModelNotFoundRealSchedulerTests(unittest.TestCase):
    """接真实调度器（AccountService + image_runtime_repository）跑一遍：账号被 404 冷落后，
    调度器抛的是「没有可用账号」，客户端拿到的必须仍是 404 的真实原因而不是这条干扰信息。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ImageRuntimeRepository(Path(self._tmp.name) / "runtime.db")
        repo_patcher = patch("services.account_service.image_runtime_repository", self.repo)
        repo_patcher.start()
        self.addCleanup(repo_patcher.stop)
        self.service = AccountService(JSONStorageBackend(Path(self._tmp.name) / "accounts.json"))
        self.service.add_account_items([
            {"access_token": "tok-1", "status": "正常", "quota": 5, "type": "Plus"},
        ])
        # 账号校验会真发网络请求，这里换成直接读本地账号记录（取号逻辑保持真实）
        validate_patcher = patch.object(
            AccountService,
            "_validate_account_cached",
            lambda self, token: dict(self._accounts.get(token) or {}),
        )
        validate_patcher.start()
        self.addCleanup(validate_patcher.stop)

        def always_404(backend: Any, request: Any, index: int, total: int):
            raise UpstreamHTTPError("/backend-api/f/conversation", 404, MODEL_NOT_FOUND_BODY)
            yield

        for target, value in (
            ("account_service", self.service),
            ("OpenAIBackendAPI", _NoopBackend),
            ("stream_image_outputs", always_404),
            ("_remove_image_conversation_later", lambda *args, **kwargs: None),
        ):
            patcher = patch.object(conversation, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sidelined_only_account_reports_model_not_found_instead_of_no_account(self) -> None:
        with self.assertRaises(conversation.ImageGenerationError) as ctx:
            conversation._generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1,
            )

        error = ctx.exception
        self.assertEqual(error.status_code, 502)
        self.assertEqual(error.code, "upstream_model_not_found")
        self.assertIn("Model not found", str(error))
        self.assertNotIn("no available image quota", str(error))
        # 真实调度器：被冷落的账号在冷却期内不再出现在候选里
        self.assertEqual(self.service._list_ready_candidate_tokens(
            health_map=self.repo.get_health_many(["tok-1"]),
        ), [])
        self.assertEqual(self.repo.get_health("tok-1")["health_state"], "open")


if __name__ == "__main__":
    unittest.main()
