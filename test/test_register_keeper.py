from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from api import register as register_api
from services import register_config, register_runner
from services.register_protocol.service import BatchJob, RegisterManager


class KeeperSettingsTests(unittest.TestCase):
    def test_keeper_concurrency_is_independent_and_clamped(self) -> None:
        source = {"enabled": False, "keeper_enabled": True, "default_concurrency": 7,
                  "keeper_concurrency": 4, "mail_api_base": "https://mail.example.com"}
        with mock.patch.object(register_config, "config", SimpleNamespace(data={"register_settings": source})):
            settings = register_config.get_register_settings()
            self.assertEqual(settings["keeper_concurrency"], 4)
            self.assertEqual(settings["default_concurrency"], 7)
            source["keeper_concurrency"] = 99
            self.assertEqual(register_config.get_register_settings()["keeper_concurrency"], 8)
            source["keeper_concurrency"] = 0
            self.assertEqual(register_config.get_register_settings()["keeper_concurrency"], 1)

    def test_checked_keeper_runs_even_when_legacy_enabled_is_false(self) -> None:
        settings = {
            "enabled": False, "keeper_enabled": True, "ready": True,
            "keeper_min_quota": 1, "keeper_source_type": "",
            "keeper_check_interval": 10, "keeper_target_accounts": 3,
            "keeper_target_quota": 0, "keeper_estimate_quota": 3,
            "keeper_daily_limit": 50, "keeper_max_per_round": 3,
            "keeper_concurrency": 4, "default_concurrency": 1,
            "keeper_fail_threshold": 5, "fixed_password": "",
        }
        keeper = register_runner.PoolKeeper()
        with mock.patch.object(register_runner, "get_register_settings", return_value=settings), \
                mock.patch.object(register_runner, "_valid_snapshot", return_value=(0, 0, 0)), \
                mock.patch.object(register_runner, "node_health", return_value={"available": True}), \
                mock.patch.object(register_runner.register_manager, "inflight_count", return_value=0), \
                mock.patch.object(register_runner.register_manager, "daily_used", return_value=0), \
                mock.patch.object(register_runner.register_manager, "daily_success", return_value=0), \
                mock.patch.object(register_runner.register_manager, "daily_remaining", return_value=50), \
                mock.patch.object(register_runner.register_manager, "prune_finished_jobs"), \
                mock.patch.object(register_runner.register_manager, "start_batch") as start_batch:
            self.assertTrue(keeper.status()["enabled"])
            self.assertEqual(keeper._run_round(), 600)

        self.assertEqual(start_batch.call_args.kwargs["count"], 3)
        self.assertEqual(start_batch.call_args.kwargs["concurrency"], 4)
        self.assertEqual(start_batch.call_args.kwargs["source"], "keeper")

    def test_latest_job_tracks_manual_and_keeper_batches(self) -> None:
        manager = RegisterManager()
        self.assertIsNone(manager.latest_job())
        manual = BatchJob("manual-job", total=2)
        keeper = BatchJob("keeper-job", total=3, source="keeper")
        manager._jobs[manual.job_id] = manual
        manager._jobs[keeper.job_id] = keeper
        keeper.add_log("info", "开始补水")

        self.assertIs(manager.latest_job(), keeper)
        self.assertEqual(keeper.snapshot()["source"], "keeper")
        self.assertEqual(keeper.logs_since(0)[0][0]["message"], "开始补水")

    def test_stop_all_signals_active_batches_only(self) -> None:
        manager = RegisterManager()
        active = BatchJob("active-job", total=2)
        finished = BatchJob("finished-job", total=1)
        finished.finished = True
        manager._jobs = {active.job_id: active, finished.job_id: finished}

        manager.stop_all()

        self.assertTrue(active.stop_event.is_set())
        self.assertFalse(finished.stop_event.is_set())

    def test_unchecked_keeper_never_starts_batch(self) -> None:
        keeper = register_runner.PoolKeeper()
        settings = {"enabled": True, "keeper_enabled": False, "keeper_check_interval": 10}
        with mock.patch.object(register_runner, "get_register_settings", return_value=settings), \
                mock.patch.object(register_runner.register_manager, "start_batch") as start_batch:
            self.assertEqual(keeper._run_round(), 600)
        self.assertEqual(keeper.last_round["message"], "守护未启用")
        start_batch.assert_not_called()

    def test_settings_api_wakes_keeper_after_save(self) -> None:
        route = next(
            route for route in register_api.create_router().routes
            if route.path == "/api/register/settings" and "POST" in route.methods
        )
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api, "update_register_settings", return_value={"keeper_enabled": True}) as update, \
                mock.patch.object(register_api.pool_keeper, "wake") as wake:
            response = asyncio.run(route.endpoint(
                register_api.RegisterSettingsUpdateRequest(keeper_enabled=True, keeper_concurrency=4),
                authorization="Bearer test",
            ))

        self.assertEqual(response["settings"]["keeper_enabled"], True)
        self.assertEqual(update.call_args.args[0]["keeper_concurrency"], 4)
        wake.assert_called_once_with()

    def test_latest_job_api_returns_snapshot(self) -> None:
        route = next(
            route for route in register_api.create_router().routes
            if route.path == "/api/register/jobs/latest" and "GET" in route.methods
        )
        job = BatchJob("keeper-job", total=3, source="keeper")
        with mock.patch.object(register_api, "require_admin"), \
                mock.patch.object(register_api.register_manager, "latest_job", return_value=job):
            response = asyncio.run(route.endpoint(authorization="Bearer test"))

        self.assertEqual(response["job"]["job_id"], "keeper-job")
        self.assertEqual(response["job"]["source"], "keeper")

    def test_log_stream_replays_history_after_page_reload(self) -> None:
        route = next(
            route for route in register_api.create_router().routes
            if route.path == "/api/register/logs/{job_id}" and "GET" in route.methods
        )
        job = BatchJob("keeper-job", total=1, source="keeper")
        job.add_log("info", "开始补水")
        job.finished = True

        async def collect() -> str:
            with mock.patch.object(register_api, "require_admin"), \
                    mock.patch.object(register_api.register_manager, "get_job", return_value=job):
                response = await route.endpoint("keeper-job", after=0, authorization="Bearer test")
                return "".join([chunk async for chunk in response.body_iterator])

        output = asyncio.run(collect())
        self.assertIn('"message": "开始补水"', output)
        self.assertIn("event: done", output)


if __name__ == "__main__":
    unittest.main()

