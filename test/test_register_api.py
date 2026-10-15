from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from api import register as register_api
from services import register_config
from services.register_protocol.service import BatchJob


def route_for(path: str, method: str):
    return next(
        route for route in register_api.create_router().routes
        if route.path == path and method in route.methods
    )


class RegisterConfigTests(unittest.TestCase):
    def test_public_settings_hide_mail_key_and_runtime_proxy_pool(self) -> None:
        data = {"register_settings": {
            "mail_api_base": "https://mail.example.invalid/",
            "mail_auth_key": "secret-only-for-test",
            "proxy_pool_text": "http://proxy.example.invalid:8080",
        }}
        with mock.patch.object(register_config, "config", SimpleNamespace(data=data)):
            settings = register_config.get_public_register_settings()
        self.assertTrue(settings["ready"])
        self.assertTrue(settings["has_mail_auth_key"])
        self.assertEqual(settings["mail_auth_key"], "")
        self.assertNotIn("proxy_pool", settings)
        self.assertNotIn("domain_pool", settings)

    def test_blank_key_update_preserves_secret_and_ignores_unknown_fields(self) -> None:
        data = {"register_settings": {"mail_auth_key": "existing-secret"}}

        def update(changes: dict) -> None:
            data.update(changes)

        with mock.patch.object(register_config, "config", SimpleNamespace(data=data, update=update)):
            public = register_config.update_register_settings({
                "mail_auth_key": "", "keeper_enabled": True, "not_a_register_option": "ignored",
            })
        self.assertEqual(data["register_settings"]["mail_auth_key"], "existing-secret")
        self.assertNotIn("not_a_register_option", data["register_settings"])
        self.assertEqual(public["mail_auth_key"], "")
        self.assertTrue(public["keeper_enabled"])


class RegisterApiBoundaryTests(unittest.TestCase):
    def test_start_rejects_missing_mail_config_without_probing_node(self) -> None:
        route = route_for("/api/register/start", "POST")
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api, "get_register_settings", return_value={"ready": False}), \
                mock.patch.object(register_api, "node_health") as node_health, \
                mock.patch.object(register_api.register_manager, "start_batch") as start_batch:
            with self.assertRaises(HTTPException) as context:
                asyncio.run(route.endpoint(register_api.RegisterStartRequest(count=3), authorization="Bearer test"))
        self.assertEqual(context.exception.status_code, 400)
        node_health.assert_not_called()
        start_batch.assert_not_called()

    def test_start_rejects_missing_node_without_starting_job(self) -> None:
        route = route_for("/api/register/start", "POST")
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api, "get_register_settings", return_value={"ready": True}), \
                mock.patch.object(register_api, "node_health", return_value={"available": False, "error": "missing"}), \
                mock.patch.object(register_api.register_manager, "start_batch") as start_batch:
            with self.assertRaises(HTTPException) as context:
                asyncio.run(route.endpoint(register_api.RegisterStartRequest(count=3), authorization="Bearer test"))
        self.assertEqual(context.exception.status_code, 400)
        start_batch.assert_not_called()

    def test_start_clamps_batch_limits_and_uses_saved_password(self) -> None:
        route = route_for("/api/register/start", "POST")
        settings = {"ready": True, "fixed_password": "saved-test-password"}
        job = SimpleNamespace(job_id="job-test", total=200)
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api, "get_register_settings", return_value=settings), \
                mock.patch.object(register_api, "node_health", return_value={"available": True}), \
                mock.patch.object(register_api.register_manager, "start_batch", return_value=job) as start_batch:
            result = asyncio.run(route.endpoint(
                register_api.RegisterStartRequest(count=999, concurrency=99, password=" "),
                authorization="Bearer test",
            ))
        self.assertEqual(result, {"job_id": "job-test", "total": 200})
        self.assertEqual(start_batch.call_args.kwargs["count"], 200)
        self.assertEqual(start_batch.call_args.kwargs["concurrency"], 8)
        self.assertEqual(start_batch.call_args.kwargs["password"], "saved-test-password")
        self.assertIs(start_batch.call_args.kwargs["on_account"], register_api.save_account)

    def test_admin_check_precedes_registration(self) -> None:
        route = route_for("/api/register/start", "POST")
        with mock.patch.object(register_api, "require_admin", side_effect=HTTPException(status_code=401)), \
                mock.patch.object(register_api, "get_register_settings") as settings, \
                mock.patch.object(register_api.register_manager, "start_batch") as start_batch:
            with self.assertRaises(HTTPException) as context:
                asyncio.run(route.endpoint(register_api.RegisterStartRequest(), authorization=None))
        self.assertEqual(context.exception.status_code, 401)
        settings.assert_not_called()
        start_batch.assert_not_called()

    def test_unknown_job_returns_404_for_progress_logs_and_stop(self) -> None:
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api.register_manager, "get_job", return_value=None), \
                mock.patch.object(register_api.register_manager, "stop_job", return_value=False):
            for path, method in (
                ("/api/register/progress/{job_id}", "GET"),
                ("/api/register/logs/{job_id}", "GET"),
                ("/api/register/stop/{job_id}", "POST"),
            ):
                with self.subTest(path=path), self.assertRaises(HTTPException) as context:
                    route = route_for(path, method)
                    if path.endswith("logs/{job_id}"):
                        asyncio.run(route.endpoint("missing", after=0, authorization="Bearer test"))
                    else:
                        asyncio.run(route.endpoint("missing", authorization="Bearer test"))
                self.assertEqual(context.exception.status_code, 404)

    def test_sse_after_cursor_only_replays_newer_logs(self) -> None:
        route = route_for("/api/register/logs/{job_id}", "GET")
        job = BatchJob("replay-test", total=1)
        job.add_log("info", "first-entry")
        job.add_log("info", "second-entry")
        job.finished = True

        async def collect() -> str:
            with mock.patch.object(register_api, "require_admin"), \
                    mock.patch.object(register_api.register_manager, "get_job", return_value=job):
                response = await route.endpoint("replay-test", after=1, authorization="Bearer test")
                return "".join([chunk async for chunk in response.body_iterator])

        output = asyncio.run(collect())
        self.assertNotIn("first-entry", output)
        self.assertIn("second-entry", output)
        self.assertIn("event: done", output)


if __name__ == "__main__":
    unittest.main()
