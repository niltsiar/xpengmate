import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import server


def _stored() -> list[str]:
    directory = server.EXPORT_UPLOAD_DIR
    return sorted(os.listdir(directory)) if os.path.isdir(directory) else []


class _FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", None


class _ServerCase(unittest.IsolatedAsyncioTestCase):
    """Runs the real app on a loopback port with the config in a temp dir."""

    stored_config: dict = {}
    env: dict = {}
    token = ""
    upload_token = ""

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
            "UPLOAD_TOKEN": self.upload_token,
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


class UploadWhileSyncRunsTest(_ServerCase):
    env = {"XPENG_VIN": "TESTVIN0000000001"}

    async def test_upload_is_refused_while_a_sync_runs_and_stores_nothing(self):
        self.service._task = asyncio.create_task(asyncio.sleep(60))
        self.addCleanup(self.service._task.cancel)
        response, spawn = await self.upload()
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), {"error": "a sync is already running"})
        spawn.assert_not_called()
        self.assertEqual(_stored(), [])


class SyncWhileExportImportRunsTest(_ServerCase):
    stored_config = {"mode": "api", "api_url": "https://example.test", "api_key": "test-key"}

    async def test_sync_is_refused_while_an_export_import_runs(self):
        self.service._export_task = asyncio.create_task(asyncio.sleep(60))
        self.addCleanup(self.service._export_task.cancel)
        with mock.patch.object(server.HistoryService, "_run_import", mock.AsyncMock()) as run:
            response = await self.client.post("/sync")
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), {"error": "an import is already running"})
        run.assert_not_called()


class ParallelUploadTest(_ServerCase):
    env = {"XPENG_VIN": "TESTVIN0000000001"}

    async def test_upload_that_starts_during_another_upload_is_refused(self):
        release = asyncio.Event()

        async def slow_body():
            yield (
                b"--B\r\nContent-Disposition: form-data; name=\"file\"; "
                b"filename=\"first.zip\"\r\n\r\nPK\x03\x04"
            )
            await release.wait()
            yield b"\r\n--B--\r\n"

        with mock.patch.object(
            server.asyncio, "create_subprocess_exec", return_value=_FakeProcess()
        ):
            first = asyncio.create_task(
                self.client.post(
                    "/exports",
                    data=slow_body(),
                    headers={"Content-Type": "multipart/form-data; boundary=B"},
                )
            )
            try:
                while not _stored():
                    await asyncio.sleep(0.01)
                second, _ = await self.upload()
            finally:
                release.set()
            first_response = await first
            if self.service._export_task is not None:
                await self.service._export_task
        self.assertEqual(second.status, 409)
        self.assertEqual(first_response.status, 200)
        self.assertEqual(_stored(), ["first.zip"])


class PartialUploadTest(_ServerCase):
    env = {"XPENG_VIN": "TESTVIN0000000001"}

    async def test_upload_cut_off_mid_stream_leaves_no_zip_behind(self):
        body = (
            b"--B\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"cut.zip\"\r\n\r\nPK\x03\x04"
        )
        with mock.patch.object(
            server.asyncio, "create_subprocess_exec", return_value=_FakeProcess()
        ) as spawn:
            response = await self.client.post(
                "/exports",
                data=body,
                headers={"Content-Type": "multipart/form-data; boundary=B"},
            )
        self.assertEqual(response.status, 400)
        spawn.assert_not_called()
        self.assertEqual(_stored(), [])

    def test_readers_of_the_upload_dir_ignore_a_part_file(self):
        directory = server.EXPORT_UPLOAD_DIR
        os.makedirs(directory)
        for name in ("done.zip", "half.zip.part"):
            with open(os.path.join(directory, name), "wb") as handle:
                handle.write(b"PK\x03\x04")
        with mock.patch.object(server, "DATABASE_URL", "postgresql://test"), mock.patch.object(
            server.psycopg, "connect", side_effect=OSError
        ):
            pending = self.service._pending_export_zips()
        self.assertEqual([os.path.basename(path) for path in pending], ["done.zip"])
        self.assertEqual(
            [path.name for path in Path(directory).glob("*.zip")], ["done.zip"]
        )


class UploadTokenTest(_ServerCase):
    token = "test-token"
    upload_token = "upload-token"

    async def test_upload_token_reaches_only_the_exports_routes(self):
        headers = {"Authorization": "Bearer upload-token"}
        self.assertEqual((await self.client.get("/exports", headers=headers)).status, 200)
        self.assertNotEqual((await self.client.post("/exports", headers=headers)).status, 401)
        self.assertEqual((await self.client.get("/config", headers=headers)).status, 401)
        self.assertEqual((await self.client.post("/sync", headers=headers)).status, 401)

    async def test_main_token_still_reaches_every_route(self):
        response = await self.client.get("/config", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status, 200)


class UploadWithoutVinTest(_ServerCase):
    async def test_no_vin_anywhere_is_rejected(self):
        response, spawn = await self.upload()
        self.assertEqual(response.status, 409)
        spawn.assert_not_called()


class ExportsStatusVinTest(_ServerCase):
    async def test_exports_status_reports_whether_a_vin_is_set(self):
        self.assertIs((await (await self.client.get("/exports")).json())["vin_set"], False)
        await self.client.put("/config", json={"vin": "TESTVIN0000000001"})
        self.assertIs((await (await self.client.get("/exports")).json())["vin_set"], True)


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
