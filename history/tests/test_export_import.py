import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

import export_import
from export_parser import Drive


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
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cursor(self.db.cursor())

    def commit(self):
        self.db.commit()


class _Store:
    def __init__(self, dsn, vin, usable_kwh):
        self.db = sqlite3.connect(":memory:")
        self.conn = _Connection(self.db)
        self.car_id = 1

    def remember(self, kind, key, target_id):
        self.db.execute("INSERT INTO xpengmate_import VALUES (?, ?, ?)", (kind, key, target_id))


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


if __name__ == "__main__":
    unittest.main()
