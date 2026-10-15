from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from fastapi.testclient import TestClient

from api import app as app_module


class RegisterAppIntegrationTests(unittest.TestCase):
    def test_create_app_mounts_register_routes_before_web_fallback(self) -> None:
        app = app_module.create_app()
        paths = app.openapi()["paths"]
        for path in (
            "/api/register/settings",
            "/api/register/start",
            "/api/register/keeper",
            "/api/register/progress/{job_id}",
            "/api/register/jobs/latest",
            "/api/register/logs/{job_id}",
            "/api/register/stop/{job_id}",
        ):
            with self.subTest(path=path):
                self.assertIn(path, paths)
        self.assertNotIn("/{full_path:path}", paths)
        response = TestClient(app).get("/api/register/settings")
        self.assertEqual(response.status_code, 401)

    def test_lifespan_starts_and_stops_keeper_with_shared_event(self) -> None:
        watcher = mock.Mock()
        cleanup = mock.Mock()
        pool = mock.Mock()
        subscriptions = mock.Mock()
        keeper = mock.Mock()
        with mock.patch.object(app_module, "start_limited_account_watcher", return_value=watcher) as start_watcher, \
                mock.patch.object(app_module, "start_image_cleanup_scheduler", return_value=cleanup) as start_cleanup, \
                mock.patch.object(app_module, "start_pool_keeper", return_value=keeper) as start_keeper, \
                mock.patch.object(app_module.account_service, "activate_proxy_pool"), \
                mock.patch.object(app_module.proxy_pool_store, "start_health_loop", return_value=pool) as start_pool, \
                mock.patch.object(app_module.proxy_subscriptions, "start_loop", return_value=subscriptions) as start_subscriptions, \
                mock.patch.object(app_module.backup_service, "start"), \
                mock.patch.object(app_module.backup_service, "stop"), \
                mock.patch.object(app_module.config, "cleanup_old_images"), \
                mock.patch.object(app_module.pool_keeper, "wake") as wake, \
                mock.patch.object(app_module.register_manager, "stop_all") as stop_all:
            with TestClient(app_module.create_app()):
                stop_event = start_keeper.call_args.args[0]
                self.assertFalse(stop_event.is_set())
                for started in (start_watcher, start_cleanup, start_pool, start_subscriptions):
                    self.assertIs(started.call_args.args[0], stop_event)
            self.assertTrue(stop_event.is_set())
            wake.assert_called_once_with()
            stop_all.assert_called_once_with()
            for thread in (watcher, cleanup, pool, subscriptions, keeper):
                thread.join.assert_called_once_with(timeout=1)


if __name__ == "__main__":
    unittest.main()
