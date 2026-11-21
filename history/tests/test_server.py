import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import server


class _FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", None


class _ServerCase(unittest.IsolatedAsyncioTestCase):
    """Runs the real app on a loopback port with the config in a temp dir."""

    stored_config: dict = {}
    env: dict = {}
    token = ""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config_path = os.path.join(self.tmp.name, "history.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(self.stored_config, handle)
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for key in ("XPENG_VIN", "XPENG_USABLE_BATTERY_KWH", "XPENG_CAR_NAME"):
            os.environ.pop(key, None)
        os.environ.update(self.env)
        for name, value in {
            "CONFIG_PATH": config_path,
            "EXPORT_UPLOAD_DIR": os.path.join(self.tmp.name, "uploaded"),
            "EXPORT_LOG": os.path.join(self.tmp.name, "exports.log"),
            "EXPORT_LAST_RUN": os.path.join(self.tmp.name, "export_last_run.json"),
            "DATABASE_URL": "",
            "TOKEN": self.token,
        }.items():
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = server.HistoryService()
        self.client = TestClient(TestServer(server.build_app(self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def upload(self):
        data = FormData()
        data.add_field("file", b"PK\x03\x04", filename="export.zip")
        with mock.patch.object(
            server.asyncio, "create_subprocess_exec", return_value=_FakeProcess()
        ) as spawn:
            response = await self.client.post("/exports", data=data)
            if self.service._export_task is not None:
                await self.service._export_task
        return response, spawn


class UploadSeededFromEnvTest(_ServerCase):
    env = {"XPENG_VIN": "TESTVIN0000000001", "XPENG_USABLE_BATTERY_KWH": "80.0"}

    async def test_vin_from_env_is_accepted_and_reaches_the_importer(self):
        response, spawn = await self.upload()
        self.assertEqual(response.status, 200)
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["XPENG_VIN"], "TESTVIN0000000001")
        self.assertEqual(environment["XPENG_USABLE_BATTERY_KWH"], "80.0")


class ConfigShowsEnvDefaultsTest(_ServerCase):
    env = {"XPENG_VIN": "TESTVIN0000000001"}

    async def test_get_config_returns_the_vin_that_only_the_environment_sets(self):
        response = await self.client.get("/config")
        self.assertEqual((await response.json())["vin"], "TESTVIN0000000001")


class UploadStoredValueWinsTest(_ServerCase):
    stored_config = {"vin": "TESTVIN0000000002", "usable_battery_kwh": 70}
    env = {"XPENG_VIN": "TESTVIN0000000001", "XPENG_USABLE_BATTERY_KWH": "80.0"}

    async def test_stored_values_win_over_env(self):
        response, spawn = await self.upload()
        self.assertEqual(response.status, 200)
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["XPENG_VIN"], "TESTVIN0000000002")
        self.assertEqual(environment["XPENG_USABLE_BATTERY_KWH"], "70")


class UploadWithoutVinTest(_ServerCase):
    async def test_no_vin_anywhere_is_rejected(self):
        response, spawn = await self.upload()
        self.assertEqual(response.status, 409)
        spawn.assert_not_called()


class HealthzTest(_ServerCase):
    token = "test-token"

    async def test_healthz_needs_no_credentials_and_reveals_nothing(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True})

    async def test_status_still_requires_the_token(self):
        response = await self.client.get("/status")
        self.assertEqual(response.status, 401)


class OrphanCloserLoopTest(_ServerCase):
    async def test_runs_at_start_and_then_on_the_interval(self):
        runs = []
        with (
            mock.patch.object(server, "DATABASE_URL", "postgresql://test"),
            mock.patch.object(server, "ORPHAN_CLOSE_INTERVAL", 0.01),
            mock.patch.object(server.HistoryService, "_close_orphans", lambda self: runs.append(1)),
        ):
            await self.service.start()
            await asyncio.sleep(0.1)
            await self.service.stop()
        self.assertGreaterEqual(len(runs), 2)

    async def test_does_not_run_without_a_database(self):
        with mock.patch.object(server.HistoryService, "_close_orphans") as close:
            await self.service.start()
            await self.service.stop()
        close.assert_not_called()


class SeedFromEnvTest(unittest.TestCase):
    def test_env_fills_only_missing_values_and_keeps_the_stored_config(self):
        stored = {"vin": "TESTVIN0000000002"}
        with mock.patch.dict(
            os.environ, {"XPENG_VIN": "TESTVIN0000000001", "XPENG_CAR_NAME": "Test Car"}
        ):
            seeded = server.seed_from_env(stored)
        self.assertEqual(seeded["vin"], "TESTVIN0000000002")
        self.assertEqual(seeded["car_name"], "Test Car")
        self.assertEqual(stored, {"vin": "TESTVIN0000000002"})


if __name__ == "__main__":
    unittest.main()
