import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import export_enrich
import export_import
import reconcile_drive_edges
from export_parser import Drive, ExportData, MergedExport


def epoch(hour, minute=0, second=0):
    return int(datetime(2026, 3, 5, hour, minute, second, tzinfo=timezone.utc).timestamp())


class _Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _Connection:
    def __init__(self, db, events=None):
        self.db = db
        self.events = events if events is not None else []

    def cursor(self):
        return _Cursor(self.db.cursor())

    def commit(self):
        self.events.append("commit")
        self.db.commit()

    def rollback(self):
        self.events.append("rollback")
        self.db.rollback()


class _Store:
    def __init__(self, dsn, vin, usable_kwh):
        self.db = sqlite3.connect(":memory:")
        self.conn = _Connection(self.db)
        self.car_id = 1
        self.vin = vin

    def remember(self, kind, key, target_id):
        self.db.execute("INSERT INTO xpengmate_import VALUES (?, ?, ?)", (kind, key, target_id))

    def close(self):
        pass


class ImportDrivesTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(export_import, "Store", _Store):
            self.importer = export_import.Importer("", "", 87.5)
        self.db = self.importer.store.db
        self.db.executescript(
            """
            CREATE TABLE drives (
                id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP,
                distance REAL, duration_min INTEGER, start_km REAL, end_km REAL, speed_max INTEGER,
                power_max INTEGER, power_min INTEGER, outside_temp_avg REAL, inside_temp_avg REAL,
                start_ideal_range_km REAL, end_ideal_range_km REAL, start_rated_range_km REAL,
                end_rated_range_km REAL, ascent INTEGER, descent INTEGER);
            CREATE TABLE xpengmate_import (kind TEXT, source_key TEXT, target_id INTEGER);
            """
        )
        # The first trip below ends before this drive starts, so only its km match.
        self.db.execute(
            "INSERT INTO drives (id, car_id, start_date, end_date, start_km, end_km)"
            " VALUES (1, 1, '2026-03-05 09:12:30', '2026-03-05 09:17:30', 5000.0, 5004.0)"
        )

    def import_trips(self, *trips):
        self.importer.import_drives(list(trips), [(1, epoch(9, 12, 30), epoch(9, 17, 30))])
        return self.db.execute("SELECT start_km, end_km FROM drives ORDER BY start_km").fetchall()

    def test_trip_whose_km_a_drive_holds_is_skipped_and_a_new_trip_is_inserted(self):
        drives = self.import_trips(
            Drive(epoch(9, 5), epoch(9, 10), 4.0, 5000.1, 5003.9),
            Drive(epoch(11), epoch(11, 20), 15.0, 5004.0, 5019.0),
        )

        self.assertEqual(drives, [(5000.0, 5004.0), (5004.0, 5019.0)])
        self.assertEqual((self.importer.added_drives, self.importer.skipped_drives), (1, 1))


class KmCoveredTest(unittest.TestCase):
    TRIP = Drive(epoch(9), epoch(9, 10), 10.0, 5000.0, 5010.0)

    def test_existing_km_cover_a_trip_only_above_half_of_its_km(self):
        self.assertTrue(export_import._km_covered(self.TRIP, [(5000.0, 5005.1)]))
        self.assertFalse(export_import._km_covered(self.TRIP, [(5000.0, 5004.9)]))


class ImportExportsTest(unittest.TestCase):
    """The import flow with the database and the parsing replaced by small fakes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = []
        for name in ("a.zip", "b.zip"):
            path = os.path.join(directory.name, name)
            with open(path, "wb") as handle:
                handle.write(name.encode())
            self.paths.append(path)
        self.db = sqlite3.connect(":memory:")
        self.db.create_function("now", 0, lambda: "now")
        self.db.executescript(
            """
            CREATE TABLE xpengmate_export_imports (
                filename TEXT PRIMARY KEY, sha256 TEXT UNIQUE, vin TEXT, filesize INTEGER,
                timer_start INTEGER, timer_end INTEGER, samples INTEGER, drives INTEGER,
                charges INTEGER, tyre_hours INTEGER, imported_at TEXT);
            """
        )
        self.events = []
        self.report = {"changed": [], "skipped": [], "positions_moved": 0, "positions_linked": 0}
        self.inserts = mock.Mock()
        self.import_drives = mock.Mock()
        self.enrich = mock.Mock(return_value=0)
        self.energy = mock.Mock(return_value=set())

        def make_store(dsn, vin, usable_kwh):
            store = _Store(dsn, vin, usable_kwh)
            store.db = self.db
            store.conn = _Connection(self.db, self.events)
            return store

        def reconcile(cursor, car_id, trips, samples_for):
            self.events.append("reconcile")
            return self.report

        def parse_files(paths):
            return MergedExport(
                files=[ExportData(path=path, samples=1) for path in paths],
                drives=[self.drive_of(path) for path in paths],
            )

        for patch in (
            mock.patch.object(export_import, "Store", make_store),
            mock.patch.object(export_import.Importer, "prepare", lambda importer: None),
            mock.patch.object(export_import.Importer, "existing_windows", lambda importer, *args: []),
            mock.patch.object(export_import.Importer, "import_drives", self.import_drives),
            mock.patch.object(export_import.Importer, "import_charges", self.inserts),
            mock.patch.object(export_import.Importer, "import_states", self.inserts),
            mock.patch.object(export_import.Importer, "import_tyres", self.inserts),
            mock.patch.object(export_import, "parse_files", parse_files),
            mock.patch.object(reconcile_drive_edges, "reconcile", reconcile),
            mock.patch.object(export_enrich, "enrich", self.enrich),
            mock.patch.object(export_enrich, "store_drive_energy", self.energy),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def drive_of(self, path):
        hour = 9 if path.endswith("a.zip") else 10
        return Drive(epoch(hour), epoch(hour, 10), 4.0, 5000.0, 5004.0)

    def recorded(self):
        return [row[0] for row in self.db.execute("SELECT filename FROM xpengmate_export_imports ORDER BY filename")]

    def run_import(self, **options):
        return export_import.import_exports("", "", 87.5, self.paths, **options)

    def test_a_zip_whose_enrichment_fails_is_not_recorded_and_the_run_is_not_ok(self):
        def enrich(store, path):
            if path.endswith("b.zip"):
                raise RuntimeError("enrichment failed")
            return 0

        self.enrich.side_effect = enrich

        with self.assertLogs("xpengmate.export_import", "WARNING"):
            summary = self.run_import()

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["enrich_failed"], ["b.zip"])
        self.assertEqual(self.recorded(), ["a.zip"])
        self.assertIn("rollback", self.events)

    def test_a_dry_run_rolls_the_reconcile_back_and_imports_and_records_nothing(self):
        summary = self.run_import(dry_run=True)

        self.assertEqual(self.events, ["reconcile", "rollback"])
        self.assertTrue(summary["dry_run"])
        self.assertEqual(self.recorded(), [])
        self.enrich.assert_not_called()
        self.import_drives.assert_not_called()

    def test_a_zip_imported_before_is_enriched_again_and_its_drives_are_not_inserted_again(self):
        self.db.execute(
            "INSERT INTO xpengmate_export_imports (filename, sha256) VALUES (?, ?)",
            ("a.zip", export_import.file_sha256(self.paths[0])),
        )

        summary = self.run_import()

        self.assertEqual([call.args[1] for call in self.enrich.call_args_list], self.paths)
        self.assertEqual(self.import_drives.call_args.args[0], [self.drive_of(self.paths[1])])
        self.assertEqual((summary["duplicates"], summary["files_imported"]), (1, 1))

    def test_a_drive_two_zips_both_cover_counts_once_in_the_summary(self):
        self.energy.side_effect = [{1, 2}, {2, 3}]

        summary = self.run_import()

        self.assertEqual(summary["drive_energies"], 3)

    def test_the_log_gives_the_reconcile_as_counts_and_the_summary_keeps_the_report(self):
        self.report = {
            "changed": [{"drive_id": n} for n in range(30)],
            "skipped": [{"drive_id": 40}],
            "positions_moved": 25,
            "positions_linked": 7,
        }

        with self.assertLogs("xpengmate.export_import", "INFO") as logs:
            summary = self.run_import()

        self.assertIn(
            "30 edges changed, 1 guesses skipped, 25 positions moved, 7 positions linked", logs.output[0]
        )
        self.assertEqual(summary["reconciled"], self.report)

    def test_the_command_exits_1_when_the_import_is_not_ok(self):
        for ok, code in ((True, 0), (False, 1)):
            with mock.patch.object(export_import, "import_exports", return_value={"ok": ok}):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = export_import.main(["--database-url", "x", "--vin", "v", "a.zip"])
            self.assertEqual(result, code)


if __name__ == "__main__":
    unittest.main()
