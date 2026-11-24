import contextlib
import io
import json
import math
import sqlite3
import unittest
from datetime import datetime
from decimal import Decimal
from unittest import mock

import backfill_teslamate as backfill
from test_repair_drive_gaps import _PsycopgStyleCursor

sqlite3.register_adapter(Decimal, str)

SCHEMA = """
CREATE TABLE cars (id INTEGER PRIMARY KEY, vin TEXT);
CREATE TABLE geofences (
    id INTEGER PRIMARY KEY, latitude REAL, longitude REAL, radius REAL,
    billing_type TEXT, cost_per_unit REAL, session_fee REAL);
CREATE TABLE positions (id INTEGER PRIMARY KEY, latitude REAL, longitude REAL);
CREATE TABLE drives (
    id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP,
    start_position_id INTEGER, end_position_id INTEGER,
    start_geofence_id INTEGER, end_geofence_id INTEGER);
CREATE TABLE charging_processes (
    id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP,
    position_id INTEGER, geofence_id INTEGER, cost REAL, charge_energy_used REAL,
    charge_energy_added REAL, duration_min INTEGER);
"""

START, END = datetime(2026, 3, 10, 18), datetime(2026, 3, 10, 20)
# Along a meridian 0.0001 degrees of latitude are 11.13 m on the earthdistance sphere.
CENTER = (10.0, 20.0)
NEAR = (10.0002, 20.0)
FAR = (10.001, 20.0)


class _ContextCursor(_PsycopgStyleCursor):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PsycopgStyleConnection:
    def __init__(self, db):
        self._db = db

    def cursor(self):
        return _ContextCursor(self._db.cursor())

    def commit(self):
        self._db.commit()

    def rollback(self):
        self._db.rollback()


class BoundaryGeofenceTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO cars VALUES (1, 'TESTVIN')")

    def geofence(self, geofence_id, point, radius, billing="per_kwh", unit=0.10, fee=None):
        self.db.execute(
            "INSERT INTO geofences VALUES (?, ?, ?, ?, ?, ?, ?)",
            (geofence_id, *point, radius, billing, unit, fee),
        )

    def position(self, point):
        return self.db.execute(
            "INSERT INTO positions (latitude, longitude) VALUES (?, ?)", point
        ).lastrowid

    def charge(self, charge_id, point, end=END, geofence=None, cost=None, minutes=90):
        self.db.execute(
            "INSERT INTO charging_processes VALUES (?, 1, ?, ?, ?, ?, ?, 12.5, 11.0, ?)",
            (charge_id, START, end, self.position(point), geofence, cost, minutes),
        )

    def charge_row(self, *columns):
        return self.db.execute(
            f"SELECT {', '.join(columns)} FROM charging_processes WHERE id = 1"
        ).fetchone()

    def drive(self, drive_id, start_point, end_point, end=END):
        self.db.execute(
            "INSERT INTO drives VALUES (?, 1, ?, ?, ?, ?, NULL, NULL)",
            (drive_id, START, end, self.position(start_point), self.position(end_point)),
        )

    def fill(self):
        return backfill.fill_geofences_and_costs(_PsycopgStyleCursor(self.db.cursor()), 1)

    def test_closed_charge_inside_the_radius_gets_the_geofence_and_the_per_kwh_cost(self):
        self.geofence(1, CENTER, 60, unit=0.10, fee=0.50)
        self.charge(1, NEAR)
        self.fill()
        # GREATEST(used 12.5, added 11.0) * 0.10 + 0.50
        self.assertEqual(self.charge_row("geofence_id", "cost"), (1, 1.75))

    def test_charge_outside_every_geofence_gets_no_geofence_and_no_cost(self):
        self.geofence(1, CENTER, 60)
        self.charge(1, FAR)
        self.assertEqual(self.fill(), [])
        self.assertEqual(self.charge_row("geofence_id", "cost"), (None, None))

    def test_existing_geofence_and_existing_cost_are_kept(self):
        self.geofence(1, CENTER, 60)
        self.geofence(2, FAR, 60, unit=0.20)
        self.charge(1, NEAR, geofence=2)
        self.charge(2, NEAR, cost=9.99)
        self.fill()
        self.assertEqual(
            self.db.execute(
                "SELECT id, geofence_id, cost FROM charging_processes ORDER BY id"
            ).fetchall(),
            [(1, 2, 2.5), (2, 1, 9.99)],
        )

    def test_open_drive_and_open_charge_are_not_touched(self):
        self.geofence(1, CENTER, 60)
        self.charge(1, NEAR, end=None)
        self.drive(1, NEAR, NEAR, end=None)
        self.assertEqual(self.fill(), [])
        self.assertEqual(self.charge_row("geofence_id", "cost"), (None, None))
        self.assertEqual(
            self.db.execute("SELECT start_geofence_id, end_geofence_id FROM drives").fetchall(),
            [(None, None)],
        )

    def test_drive_start_and_end_inside_get_their_geofences(self):
        self.geofence(1, CENTER, 60)
        self.geofence(2, FAR, 60)
        self.drive(1, NEAR, FAR)
        self.drive(2, (11.0, 21.0), NEAR)
        self.fill()
        self.assertEqual(
            self.db.execute(
                "SELECT id, start_geofence_id, end_geofence_id FROM drives ORDER BY id"
            ).fetchall(),
            [(1, 1, 2), (2, None, 1)],
        )

    def test_nearest_of_two_overlapping_geofences_wins(self):
        self.geofence(1, CENTER, 200)
        self.geofence(2, (10.0003, 20.0), 200)
        self.charge(1, NEAR)
        self.fill()
        self.assertEqual(self.charge_row("geofence_id"), (2,))

    def test_point_at_the_radius_is_outside_like_teslamate(self):
        radius = backfill.earth_distance(CENTER, NEAR)
        self.assertAlmostEqual(radius, 22.26, places=2)
        self.geofence(1, CENTER, radius)
        self.charge(1, NEAR)
        self.assertEqual(self.fill(), [])
        self.db.execute("UPDATE geofences SET radius = ?", (math.nextafter(radius, math.inf),))
        self.fill()
        self.assertEqual(self.charge_row("geofence_id"), (1,))

    def test_per_minute_cost_is_the_session_fee_plus_the_price_per_minute(self):
        self.geofence(1, CENTER, 60, billing="per_minute", unit=0.05, fee=1.00)
        self.charge(1, NEAR, minutes=90)
        self.fill()
        self.assertEqual(self.charge_row("cost"), (5.5,))

    def test_geofence_without_prices_leaves_the_cost_null(self):
        self.geofence(1, CENTER, 60, unit=None, fee=None)
        self.charge(1, NEAR)
        self.fill()
        self.assertEqual(self.charge_row("geofence_id", "cost"), (1, None))

    def test_dry_run_prints_the_changes_and_writes_nothing(self):
        self.geofence(1, CENTER, 60)
        self.charge(1, NEAR)
        self.db.commit()
        output = io.StringIO()
        connection = _PsycopgStyleConnection(self.db)
        arguments = ["--dry-run", "--database-url", "postgres://test", "--vin", "TESTVIN"]
        with mock.patch.object(backfill.psycopg, "connect", return_value=connection), \
                contextlib.redirect_stdout(output):
            self.assertEqual(backfill.main(arguments), 0)
        lines = [line for line in output.getvalue().splitlines() if line.startswith('{"dry_run"')]
        self.assertEqual(
            [(c["table"], c["id"], c["column"], c["value"]) for c in map(json.loads, lines)],
            [("charging_processes", 1, "geofence_id", 1), ("charging_processes", 1, "cost", "1.25")],
        )
        self.assertEqual(self.charge_row("geofence_id", "cost"), (None, None))


if __name__ == "__main__":
    unittest.main()
