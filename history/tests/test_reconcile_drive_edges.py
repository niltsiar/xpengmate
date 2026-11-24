import sqlite3
import unittest
from datetime import datetime

import reconcile_drive_edges as reconciler
import repair_drive_gaps as repairer
from test_repair_drive_gaps import PREVIOUS_DRIVE, TRIP, TRIP_LOCATIONS, _PsycopgStyleCursor, at, ha

SCHEMA = """
CREATE TABLE drives (
    id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP,
    start_km REAL, end_km REAL, distance REAL, duration_min INTEGER,
    start_position_id INTEGER, end_position_id INTEGER,
    start_address_id INTEGER, end_address_id INTEGER,
    start_geofence_id INTEGER, end_geofence_id INTEGER,
    start_rated_range_km REAL, end_rated_range_km REAL,
    start_ideal_range_km REAL, end_ideal_range_km REAL);
CREATE TABLE positions (
    id INTEGER PRIMARY KEY, car_id INTEGER, drive_id INTEGER, date TIMESTAMP,
    latitude REAL, longitude REAL, speed INTEGER, odometer REAL, battery_level INTEGER,
    usable_battery_level INTEGER, rated_battery_range_km REAL,
    ideal_battery_range_km REAL, est_battery_range_km REAL);
CREATE TABLE charging_processes (
    id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP);
CREATE TABLE xpengmate_drive_repairs (
    id INTEGER PRIMARY KEY, car_id INTEGER, drive_id INTEGER, kind TEXT, km_from REAL,
    km_to REAL, start_at TIMESTAMP, end_at TIMESTAMP, estimated BOOLEAN, created_at TIMESTAMP,
    confirmed_start_at TIMESTAMP, confirmed_end_at TIMESTAMP);
"""


class _Cursor(_PsycopgStyleCursor):
    @property
    def rowcount(self):
        return self._cursor.rowcount


class ReconcileDriveEdgesTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.executescript(SCHEMA)
        self.cursor = _Cursor(self.db.cursor())
        self.samples = {}

    # -- fixtures ---------------------------------------------

    def drive(self, drive_id, start, end, start_km, end_km):
        self.db.execute(
            "INSERT INTO drives (id, car_id, start_date, end_date, start_km, end_km, distance,"
            " start_address_id, start_rated_range_km, start_ideal_range_km)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, 7, 300.0, 300.0)",
            (drive_id, start, end, start_km, end_km, end_km - start_km),
        )

    def position(self, drive_id, moment, odometer, speed=None):
        return self.db.execute(
            "INSERT INTO positions (car_id, drive_id, date, latitude, longitude, speed, odometer,"
            " battery_level, rated_battery_range_km, ideal_battery_range_km)"
            " VALUES (1, ?, ?, 12.0, 34.0, ?, ?, 60, 300.0, 300.0)",
            (drive_id, moment, speed, odometer),
        ).lastrowid

    def repair(self, drive_id, kind, start_at, end_at):
        self.db.execute(
            "INSERT INTO xpengmate_drive_repairs (car_id, drive_id, kind, km_from, km_to, start_at,"
            " end_at, estimated, created_at) VALUES (1, ?, ?, 0, 0, ?, ?, 1, ?)",
            (drive_id, kind, start_at, end_at, at(20, 0)),
        )

    def inserted(self, drive_id, start, end, start_km, end_km):
        """A drive the repair job inserted, with its two estimated positions."""
        self.drive(drive_id, start, end, start_km, end_km)
        self.position(drive_id, start, start_km)
        self.position(drive_id, end, end_km)
        self.repair(drive_id, "inserted", start, end)

    def extended_start(self, drive_id, guess, live_start, end, start_km, end_km, estimated_position=True):
        """A live drive the repair job moved back from ``live_start`` to ``guess``."""
        self.drive(drive_id, guess, end, start_km, end_km)
        if estimated_position:
            self.position(drive_id, guess, start_km)
        self.position(drive_id, live_start, start_km + 2, speed=50)
        self.position(drive_id, end, end_km, speed=0)
        self.repair(drive_id, "extended_start", guess, live_start)

    def extended_end(self, drive_id, new_end, end_km):
        """The repair job moved the end of an existing drive on to ``new_end``."""
        old_end = self.window(drive_id)[1]
        self.db.execute(
            "UPDATE drives SET end_date = ?, end_km = ? WHERE id = ?", (new_end, end_km, drive_id)
        )
        self.position(drive_id, new_end, end_km)
        self.repair(drive_id, "extended_end", old_end, new_end)

    def sample(self, moment, soc, odometer, range_km):
        self.samples[moment] = (soc, odometer, range_km)

    def reconcile(self, *trips):
        return reconciler.reconcile(self.cursor, 1, list(trips), lambda moments: self.samples)

    # -- readers ----------------------------------------------

    def window(self, drive_id):
        return self.db.execute(
            "SELECT start_date, end_date FROM drives WHERE id = ?", (drive_id,)
        ).fetchone()

    def positions(self, drive_id):
        return self.db.execute(
            "SELECT date, speed, odometer, battery_level, rated_battery_range_km,"
            " ideal_battery_range_km, est_battery_range_km FROM positions"
            " WHERE drive_id = ? ORDER BY date",
            (drive_id,),
        ).fetchall()

    def snapshot(self):
        return tuple(
            self.db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("drives", "positions", "xpengmate_drive_repairs")
        )

    # -- matches ----------------------------------------------

    def test_inserted_drive_takes_both_trip_ends_and_its_positions_move_with_them(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.sample(at(5, 9, 10), 71, 5000.2, 251.5)
        self.sample(at(5, 9, 15), 70, 5003.9, 248.0)

        report = self.reconcile(reconciler.Trip(at(5, 9, 10), at(5, 9, 15), 5000.2, 5003.9))

        self.assertEqual(self.window(1), (at(5, 9, 10), at(5, 9, 15)))
        drive = self.db.execute(
            "SELECT duration_min, start_km, end_km, start_rated_range_km, start_ideal_range_km,"
            " end_rated_range_km, end_ideal_range_km, start_address_id FROM drives WHERE id = 1"
        ).fetchone()
        self.assertEqual(drive, (5, 5000.0, 5004.0, 251.5, 251.5, 248.0, 248.0, 7))
        self.assertEqual(
            self.positions(1),
            [
                (at(5, 9, 10), None, 5000.2, 71, 251.5, 251.5, 251.5),
                (at(5, 9, 15), None, 5003.9, 70, 248.0, 248.0, 248.0),
            ],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT start_at, end_at, confirmed_start_at, confirmed_end_at FROM xpengmate_drive_repairs"
            ).fetchone(),
            (at(5, 9, 12, 30), at(5, 9, 17, 30), at(5, 9, 10), at(5, 9, 15)),
        )
        self.assertEqual(
            [(c["drive_id"], c["side"], c["to"]) for c in report["changed"]],
            [(1, "start", at(5, 9, 10).isoformat()), (1, "end", at(5, 9, 15).isoformat())],
        )
        self.assertEqual(report["positions_moved"], 2)

    def test_extended_start_takes_only_the_trip_start(self):
        self.extended_start(2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0)

        self.reconcile(reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40, 30), 2000.1, 2030.0))

        self.assertEqual(self.window(2), (at(17, 11, 58, 30), at(17, 12, 40)))
        self.assertEqual(
            [row[:3] for row in self.positions(2)],
            [(at(17, 11, 58, 30), None, 2000.0), (at(17, 12, 10), 50, 2002.0), (at(17, 12, 40), 0, 2030.0)],
        )
        self.assertEqual(
            self.db.execute("SELECT confirmed_start_at, confirmed_end_at FROM xpengmate_drive_repairs").fetchone(),
            (at(17, 11, 58, 30), None),
        )

    def test_live_position_after_the_trip_end_widens_the_end(self):
        self.inserted(1, at(16, 10), at(16, 10, 30), 1000.0, 1020.0)
        self.position(1, at(16, 10, 30, 20), 1019.9, speed=0)

        self.reconcile(reconciler.Trip(at(16, 10, 2, 15), at(16, 10, 29, 40), 1000.2, 1019.8))

        self.assertEqual(self.window(1), (at(16, 10, 2, 15), at(16, 10, 30, 20)))
        self.assertCountEqual(
            [row[:2] for row in self.positions(1)],
            [(at(16, 10, 2, 15), None), (at(16, 10, 30, 20), None), (at(16, 10, 30, 20), 0)],
        )

    def test_live_position_the_repair_reused_as_its_start_is_not_moved(self):
        self.extended_start(
            2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0, estimated_position=False
        )
        before = self.positions(2)

        report = self.reconcile(reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40), 2000.1, 2030.0))

        self.assertEqual(self.window(2)[0], at(17, 11, 58, 30))
        self.assertEqual(self.positions(2), before)
        self.assertEqual(report["positions_moved"], 0)

    def test_start_edge_still_reconciles_after_a_later_repair_moved_the_end_again(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.extended_end(1, at(5, 9, 30), 5010.0)

        report = self.reconcile(reconciler.Trip(at(5, 9, 10), at(5, 9, 15), 5000.2, 5003.9))

        self.assertEqual(self.window(1), (at(5, 9, 10), at(5, 9, 30)))
        self.assertEqual(
            [row[:3] for row in self.positions(1)],
            [(at(5, 9, 10), None, 5000.0), (at(5, 9, 17, 30), None, 5004.0), (at(5, 9, 30), None, 5010.0)],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT kind, confirmed_start_at, confirmed_end_at FROM xpengmate_drive_repairs ORDER BY id"
            ).fetchall(),
            [("inserted", at(5, 9, 10), None), ("extended_end", None, None)],
        )
        self.assertEqual([(c["side"]) for c in report["changed"]], ["start"])

    def test_guess_whose_every_edge_moved_again_is_skipped(self):
        self.extended_start(2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0)
        self.db.execute("UPDATE drives SET start_date = ? WHERE id = 2", (at(17, 11, 50),))

        report = self.reconcile(reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40, 30), 2000.1, 2030.0))

        self.assertEqual(self.window(2), (at(17, 11, 50), at(17, 12, 40)))
        self.assertEqual(
            [(s["drive_id"], s["reason"]) for s in report["skipped"]],
            [(2, "drive edge changed after the repair")],
        )

    # -- rejected matches, each next to a drive that changes ---

    MATCHING_TRIP = reconciler.Trip(at(5, 9, 10), at(5, 9, 15), 5000.2, 5003.9)

    def assert_only_the_inserted_drive_changed(self, report, drive_id, window, reason):
        self.assertEqual(self.window(1), (at(5, 9, 10), at(5, 9, 15)))
        self.assertEqual(self.window(drive_id), window)
        self.assertIn(
            (drive_id, reason), [(s["drive_id"], s["reason"]) for s in report["skipped"]]
        )

    def test_live_drive_without_a_repair_keeps_its_times(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.drive(2, at(17, 12, 10), at(17, 12, 40), 2002.0, 2030.0)

        self.reconcile(self.MATCHING_TRIP, reconciler.Trip(at(17, 12), at(17, 12, 40), 2000.0, 2030.0))

        self.assertEqual(self.window(1), (at(5, 9, 10), at(5, 9, 15)))
        self.assertEqual(self.window(2), (at(17, 12, 10), at(17, 12, 40)))

    def test_trip_more_than_1_km_off_the_drive_is_no_match(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.inserted(3, at(17, 10), at(17, 10, 30), 3000.0, 3010.0)

        report = self.reconcile(
            self.MATCHING_TRIP, reconciler.Trip(at(17, 10, 5), at(17, 10, 28), 3001.5, 3010.0)
        )

        self.assert_only_the_inserted_drive_changed(report, 3, (at(17, 10), at(17, 10, 30)), "0 matching trips")

    def test_two_candidate_trips_are_no_match(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.extended_start(4, at(17, 12), at(17, 12, 10), at(17, 12, 40), 4000.0, 4030.0)

        report = self.reconcile(
            self.MATCHING_TRIP,
            reconciler.Trip(at(17, 12, 0, 10), at(17, 12, 1), 4000.0, 4000.4),
            reconciler.Trip(at(17, 12, 3), at(17, 12, 40), 4000.5, 4030.0),
        )

        self.assert_only_the_inserted_drive_changed(report, 4, (at(17, 12), at(17, 12, 40)), "2 matching trips")

    def test_new_start_inside_the_previous_drive_is_dropped(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.drive(5, at(17, 11), at(17, 11, 59), 1990.0, 2000.0)
        self.extended_start(2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0)

        report = self.reconcile(
            self.MATCHING_TRIP, reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40), 2000.1, 2030.0)
        )

        self.assert_only_the_inserted_drive_changed(
            report, 2, (at(17, 12), at(17, 12, 40)), "new times overlap another drive or a charge"
        )
        self.assertIsNone(
            self.db.execute("SELECT confirmed_start_at FROM xpengmate_drive_repairs WHERE drive_id = 2").fetchone()[0]
        )

    def test_new_start_inside_a_charge_is_dropped(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.db.execute(
            "INSERT INTO charging_processes (car_id, start_date, end_date) VALUES (1, ?, ?)",
            (at(17, 11, 30), at(17, 11, 59)),
        )
        self.extended_start(2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0)

        report = self.reconcile(
            self.MATCHING_TRIP, reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40), 2000.1, 2030.0)
        )

        self.assert_only_the_inserted_drive_changed(
            report, 2, (at(17, 12), at(17, 12, 40)), "new times overlap another drive or a charge"
        )

    def test_one_trip_claimed_by_two_drives_moves_neither(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.inserted(6, at(17, 10), at(17, 10, 30), 3000.0, 3010.0)
        self.inserted(7, at(17, 10, 40), at(17, 11), 3000.4, 3010.6)

        report = self.reconcile(
            self.MATCHING_TRIP, reconciler.Trip(at(17, 10, 5), at(17, 10, 28), 3000.2, 3010.3)
        )

        self.assert_only_the_inserted_drive_changed(
            report, 6, (at(17, 10), at(17, 10, 30)), "trip matches another drive"
        )
        self.assertEqual(self.window(7), (at(17, 10, 40), at(17, 11)))

    # -- reruns -----------------------------------------------

    def test_second_run_changes_nothing(self):
        self.inserted(1, at(5, 9, 12, 30), at(5, 9, 17, 30), 5000.0, 5004.0)
        self.extended_start(2, at(17, 12), at(17, 12, 10), at(17, 12, 40), 2000.0, 2030.0)
        trips = (self.MATCHING_TRIP, reconciler.Trip(at(17, 11, 58, 30), at(17, 12, 40), 2000.1, 2030.0))
        first = self.reconcile(*trips)
        after_first = self.snapshot()

        second = self.reconcile(*trips)

        self.assertEqual(len(first["changed"]), 3)
        self.assertEqual(second["changed"], [])
        self.assertEqual(self.snapshot(), after_first)

    def test_repair_run_after_reconcile_writes_nothing(self):
        self.db.execute(
            "INSERT INTO drives (id, car_id, start_date, end_date, start_km, end_km, distance)"
            " VALUES (?, 1, ?, ?, ?, ?, 0)",
            PREVIOUS_DRIVE,
        )
        now = at(11, 19)
        inserted = repairer.repair_gaps(self.cursor, 1, ha(TRIP), ha(TRIP_LOCATIONS), now)
        self.assertEqual([r["kind"] for r in inserted], ["inserted"])
        repair_row = self.db.execute("SELECT start_at, end_at FROM xpengmate_drive_repairs").fetchone()
        self.reconcile(reconciler.Trip(at(11, 16, 44, 30), at(11, 17, 16, 58), 10487.0, 10519.9))
        self.assertEqual(self.window(inserted[0]["drive_id"]), (at(11, 16, 44, 30), at(11, 17, 16, 58)))
        after_reconcile = self.snapshot()

        again = repairer.repair_gaps(self.cursor, 1, ha(TRIP), ha(TRIP_LOCATIONS), now)

        self.assertEqual(again, [])
        self.assertEqual(self.snapshot(), after_reconcile)
        self.assertEqual(
            self.db.execute("SELECT start_at, end_at FROM xpengmate_drive_repairs").fetchone(), repair_row
        )


if __name__ == "__main__":
    unittest.main()
