import sqlite3
import unittest
from datetime import datetime, timedelta

import repair_drive_gaps as repairer

sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
sqlite3.register_converter("TIMESTAMP", lambda raw: datetime.fromisoformat(raw.decode()))


def at(day, hour, minute=0, second=0, millisecond=0):
    return datetime(2026, 3, day, hour, minute, second, millisecond * 1000)


def steps(start, km, end, last_km):
    """1 km rises evenly spread from (start, km) to (end, last_km), the start excluded."""
    count = last_km - km
    return [
        (start + timedelta(seconds=round((end - start).total_seconds() * k / count)), str(km + k))
        for k in range(1, count + 1)
    ]


def ha(records):
    """A fetcher that answers like HA's history period: the state at the start, then the changes."""

    def fetch(start, end):
        before = [record for record in records if record[0] <= start]
        carried = [(start, *before[-1][1:])] if before else []
        return carried + [record for record in records if start < record[0] <= end]

    return fetch


# Two evenings: drive X misses the first 21 km, drive Y the first 6 km.
EVENINGS = [
    (at(10, 17), "10421"),
    (at(10, 18, 56, 10), "10422"),
    (at(10, 19, 1, 10), "10430"),
    (at(10, 19, 6, 11), "10438"),
    (at(10, 19, 10, 27), "10440"),
    *steps(at(10, 19, 10, 27), 10440, at(10, 19, 41, 27), 10453),
    (at(10, 22, 11, 25), "10455"),
    (at(10, 22, 16, 25), "10457"),
    (at(10, 22, 18, 27), "10458"),
    (at(10, 22, 19, 27), "10459"),
    (at(10, 22, 21, 47), "10460"),
    *steps(at(10, 22, 21, 47), 10460, at(10, 22, 45, 35), 10487),
]
EVENING_DRIVES = [
    (1, at(10, 12), at(10, 12, 30), 10400.0, 10421.0),
    (2, at(10, 19, 12, 15, 849), at(10, 19, 42, 5, 609), 10441.998967, 10453.00688),
    (3, at(10, 22, 19, 48, 500), at(10, 22, 22, 27, 888), 10458.99364, 10460.007526),
    (4, at(10, 22, 22, 41, 781), at(10, 22, 46, 27, 975), 10460.007526, 10486.996225),
]
EVENING_LOCATIONS = [
    (at(10, 12, 30), 13.0, 35.0),
    (at(10, 19, 45), 13.5, 35.5),
    (at(10, 22, 47), 13.0, 35.0),
]

# The next afternoon: a whole trip with no recorded drive.
TRIP = [
    (at(11, 15), "10487"),
    (at(11, 16, 51, 11), "10502"),
    (at(11, 16, 56, 11), "10505"),
    (at(11, 16, 57, 17), "unavailable"),
    (at(11, 16, 57, 18), "10505"),
    (at(11, 17, 7, 18), "10509"),
    (at(11, 17, 12, 19), "10517"),
    (at(11, 17, 17, 19), "10520"),
]
TRIP_LOCATIONS = [
    (at(11, 14), 12.0, 34.0),
    (at(11, 16, 55), 12.05, 34.1),
    (at(11, 17, 17), 12.2, 34.3),
]
PREVIOUS_DRIVE = EVENING_DRIVES[-1]
EVENING_RANGES = [
    (at(10, 17), "264"),
    (at(10, 18, 56, 10), "263"),
    (at(10, 19, 41, 27), "248"),
    (at(10, 22, 30), "unavailable"),
    (at(10, 22, 45, 35), "219"),
]
EVENING_BATTERIES = [(at(10, 17), "60")]


class _PsycopgStyleCursor:
    """Lets the psycopg-style %s queries run on SQLite."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        self._cursor.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class RepairDriveGapsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.executescript(
            """
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
                latitude REAL, longitude REAL, odometer REAL, battery_level INTEGER,
                usable_battery_level INTEGER, rated_battery_range_km REAL,
                ideal_battery_range_km REAL);
            CREATE TABLE charging_processes (
                id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP);
            """
        )
        self.cursor = _PsycopgStyleCursor(self.db.cursor())

    def drive(self, drive_id, start, end, start_km, end_km, address=None):
        self.db.execute(
            "INSERT INTO drives (id, car_id, start_date, end_date, start_km, end_km, distance,"
            " start_address_id) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            (drive_id, start, end, start_km, end_km,
             None if end_km is None else end_km - start_km, address),
        )

    def position(self, drive_id, moment, odometer):
        self.db.execute(
            "INSERT INTO positions (car_id, drive_id, date, latitude, longitude, odometer)"
            " VALUES (1, ?, ?, 12.0, 34.0, ?)",
            (drive_id, moment, odometer),
        )

    def repair(self, records, locations=(), now=at(11, 19), ranges=None, batteries=None):
        return repairer.repair_gaps(
            self.cursor, 1, ha(records), ha(list(locations)), now,
            None if ranges is None else ha(ranges), None if batteries is None else ha(batteries),
        )

    def row(self, table, row_id):
        cursor = self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        found = cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description], found)) if found else None

    def drives(self):
        return self.db.execute(
            "SELECT start_date, end_date, start_km, end_km FROM drives ORDER BY start_date"
        ).fetchall()

    def counts(self):
        return tuple(
            self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("drives", "positions", "xpengmate_drive_repairs")
        )

    def test_trip_with_no_recorded_drive_is_inserted_from_the_previous_drive_end(self):
        self.drive(*PREVIOUS_DRIVE)
        repairs = self.repair(TRIP, TRIP_LOCATIONS)
        self.assertEqual(
            [(r["kind"], r["km_from"], r["km_to"], r["start_at"], r["end_at"]) for r in repairs],
            [("inserted", 10486.996225, 10520.0, at(11, 16, 41, 11), at(11, 17, 17, 19))],
        )
        drive = self.row("drives", repairs[0]["drive_id"])
        self.assertEqual(
            (drive["start_date"], drive["end_date"], drive["start_km"], drive["end_km"],
             drive["duration_min"]),
            (at(11, 16, 41, 11), at(11, 17, 17, 19), 10486.996225, 10520.0, 36),
        )
        self.assertAlmostEqual(drive["distance"], 33.003775)
        self.assertIsNone(drive["start_rated_range_km"])
        start, end = (self.row("positions", drive[f"{side}_position_id"]) for side in ("start", "end"))
        self.assertEqual(
            (start["date"], start["latitude"], start["longitude"], start["odometer"], start["drive_id"]),
            (at(11, 16, 41, 11), 12.0, 34.0, 10486.996225, drive["id"]),
        )
        self.assertEqual(
            (end["date"], end["latitude"], end["longitude"], end["odometer"], end["drive_id"]),
            (at(11, 17, 17, 19), 12.2, 34.3, 10520.0, drive["id"]),
        )

    def test_drives_that_missed_their_first_km_are_extended_back(self):
        for drive in EVENING_DRIVES:
            self.drive(*drive, address=7)
        repairs = self.repair(EVENINGS, EVENING_LOCATIONS)
        self.assertEqual(
            [(r["kind"], r["drive_id"], r["km_from"], r["km_to"], r["start_at"]) for r in repairs],
            [
                ("extended_start", 2, 10421.0, 10441.998967, at(10, 18, 51, 10)),
                ("extended_start", 3, 10453.00688, 10458.99364, at(10, 22, 6, 25)),
            ],
        )
        self.assertEqual(
            self.drives(),
            [
                (at(10, 12), at(10, 12, 30), 10400.0, 10421.0),
                (at(10, 18, 51, 10), at(10, 19, 42, 5, 609), 10421.0, 10453.00688),
                (at(10, 22, 6, 25), at(10, 22, 22, 27, 888), 10453.00688, 10460.007526),
                (at(10, 22, 22, 41, 781), at(10, 22, 46, 27, 975), 10460.007526, 10486.996225),
            ],
        )
        x = self.row("drives", 2)
        self.assertAlmostEqual(x["distance"], 10453.00688 - 10421.0)
        self.assertEqual((x["duration_min"], x["start_address_id"]), (51, None))
        self.assertEqual(self.row("drives", 4)["start_address_id"], 7)
        start = self.row("positions", x["start_position_id"])
        self.assertEqual(
            (start["date"], start["latitude"], start["odometer"], start["drive_id"]),
            (at(10, 18, 51, 10), 13.0, 10421.0, 2),
        )

    def test_extended_start_takes_range_and_battery_in_effect_at_the_new_start(self):
        for drive in EVENING_DRIVES:
            self.drive(*drive)
        self.db.execute("UPDATE drives SET start_rated_range_km = 240, start_ideal_range_km = 240")
        self.repair(EVENINGS, EVENING_LOCATIONS, ranges=EVENING_RANGES, batteries=EVENING_BATTERIES)
        x, y = self.row("drives", 2), self.row("drives", 3)
        self.assertEqual((x["start_rated_range_km"], x["start_ideal_range_km"]), (264.0, 264.0))
        self.assertEqual((y["start_rated_range_km"], y["start_ideal_range_km"]), (248.0, 248.0))
        self.assertEqual(self.row("drives", 4)["start_rated_range_km"], 240.0)
        start = self.row("positions", x["start_position_id"])
        self.assertEqual(
            (start["battery_level"], start["usable_battery_level"],
             start["rated_battery_range_km"], start["ideal_battery_range_km"]),
            (60, 60, 264.0, 264.0),
        )

    def test_extension_without_range_entity_writes_null_start_range(self):
        for drive in EVENING_DRIVES:
            self.drive(*drive)
        self.db.execute("UPDATE drives SET start_rated_range_km = 240, start_ideal_range_km = 240")
        self.repair(EVENINGS, EVENING_LOCATIONS)
        x = self.row("drives", 2)
        self.assertEqual((x["start_rated_range_km"], x["start_ideal_range_km"]), (None, None))
        start = self.row("positions", x["start_position_id"])
        self.assertEqual((start["battery_level"], start["rated_battery_range_km"]), (None, None))

    def test_charge_that_ended_just_before_the_trip_moves_the_start_to_its_end(self):
        self.drive(*PREVIOUS_DRIVE)
        self.db.execute(
            "INSERT INTO charging_processes VALUES (1, 1, ?, ?)", (at(11, 15), at(11, 16, 49, 11))
        )
        repairs = self.repair(TRIP, TRIP_LOCATIONS)
        self.assertEqual([r["start_at"] for r in repairs], [at(11, 16, 49, 11)])

    def test_rerun_writes_nothing_even_without_the_repair_log(self):
        for drive in EVENING_DRIVES:
            self.drive(*drive)
        records, locations = EVENINGS + TRIP, EVENING_LOCATIONS + TRIP_LOCATIONS
        self.assertEqual(len(self.repair(records, locations)), 3)
        after_first_run = (self.drives(), self.counts())
        self.assertEqual(self.repair(records, locations), [])
        self.db.execute("DELETE FROM xpengmate_drive_repairs")
        self.assertEqual(self.repair(records, locations), [])
        self.assertEqual(self.drives(), after_first_run[0])
        self.assertEqual(self.counts()[:2], after_first_run[1][:2])

    def test_live_open_drive_blocks_the_car(self):
        self.drive(*PREVIOUS_DRIVE)
        self.drive(5, at(11, 18, 30), None, 10520.0, None)
        self.assertEqual(self.repair(TRIP, TRIP_LOCATIONS), [])
        self.assertEqual(len(self.drives()), 2)

    def test_orphan_with_a_later_drive_is_closed_from_its_positions(self):
        self.drive(5, at(9, 8), None, None, None)
        for moment, km in ((at(9, 8, 2), 10400.0), (at(9, 8, 10), 10405.0), (at(9, 8, 20), 10410.5)):
            self.position(5, moment, km)
        self.drive(6, at(9, 10), at(9, 10, 30), 10410.5, 10420.0)
        repairs = self.repair([])
        self.assertEqual([(r["kind"], r["drive_id"]) for r in repairs], [("closed_orphan", 5)])
        drive = self.row("drives", 5)
        self.assertEqual(
            (drive["start_date"], drive["end_date"], drive["start_km"], drive["end_km"],
             drive["distance"], drive["duration_min"], drive["start_position_id"],
             drive["end_position_id"]),
            (at(9, 8, 2), at(9, 8, 20), 10400.0, 10410.5, 10.5, 18, 1, 3),
        )

    def test_orphan_with_one_position_and_a_later_charge_is_deleted(self):
        self.drive(5, at(9, 8), None, None, None)
        self.position(5, at(9, 8, 2), 10400.0)
        self.db.execute("INSERT INTO charging_processes VALUES (1, 1, ?, NULL)", (at(9, 9),))
        repairs = self.repair([])
        self.assertEqual([(r["kind"], r["drive_id"]) for r in repairs], [("deleted_orphan", 5)])
        self.assertIsNone(self.row("drives", 5))
        self.assertIsNone(self.row("positions", 1)["drive_id"])

    def test_implausible_jump_is_skipped_and_later_rises_still_count(self):
        self.drive(*PREVIOUS_DRIVE)
        records = [
            (at(11, 15), "10487"),
            (at(11, 16), "10488"),
            (at(11, 16, 5), "99999"),
            (at(11, 16, 10), "10490"),
            (at(11, 16, 15), "10492"),
        ]
        with self.assertLogs(repairer._LOGGER, "WARNING") as logs:
            repairs = self.repair(records)
        self.assertIn("99999", logs.output[0])
        self.assertEqual([(r["km_from"], r["km_to"]) for r in repairs], [(10486.996225, 10492.0)])

    def test_last_1_km_step_9_seconds_after_the_previous_sample_is_kept(self):
        self.drive(*PREVIOUS_DRIVE)
        records = [
            (at(11, 15), "10487"),
            (at(11, 16), "10490"),
            (at(11, 16, 5, 53), "10495"),
            (at(11, 16, 6, 2), "10496"),
        ]
        repairs = self.repair(records)
        self.assertEqual([(r["km_from"], r["km_to"]) for r in repairs], [(10486.996225, 10496.0)])

    def test_missing_entity_env_turns_the_repair_off_with_one_info(self):
        environ = {"XPENG_HA_URL": "http://ha.invalid:8123", "XPENG_HA_TOKEN": "token"}
        with self.assertLogs(repairer._LOGGER, "INFO") as logs:
            self.assertIsNone(repairer.settings_from_env(environ))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("XPENG_ODOMETER_ENTITY, XPENG_LOCATION_ENTITY", logs.output[0])

    def test_flat_odometer_for_15_minutes_splits_into_two_inserted_drives(self):
        self.drive(*PREVIOUS_DRIVE)
        records = [
            (at(11, 15), "10487"),
            (at(11, 16), "10490"),
            (at(11, 16, 5), "10495"),
            (at(11, 16, 20), "10498"),
            (at(11, 16, 25), "10500"),
        ]
        repairs = self.repair(records)
        self.assertEqual(
            [(r["kind"], r["km_from"], r["km_to"], r["start_at"], r["end_at"]) for r in repairs],
            [
                ("inserted", 10486.996225, 10495.0, at(11, 15, 55), at(11, 16, 5)),
                ("inserted", 10495.0, 10500.0, at(11, 16, 15), at(11, 16, 25)),
            ],
        )

    def test_charging_session_flat_odometer_with_a_dip_creates_no_drive(self):
        self.drive(*PREVIOUS_DRIVE)
        self.db.execute(
            "INSERT INTO charging_processes VALUES (1, 1, ?, ?)", (at(11, 15), at(11, 17))
        )
        records = [
            (at(11, 15), "10487"),
            (at(11, 15, 30), "10487"),
            (at(11, 15, 40), "unavailable"),
            (at(11, 15, 41), "10486"),
            (at(11, 16, 10), "10487"),
            (at(11, 16, 50), "10487"),
        ]
        self.assertEqual(self.repair(records), [])
        self.assertEqual(len(self.drives()), 1)

    def test_episode_still_rising_near_the_window_end_waits_for_the_next_run(self):
        self.drive(*PREVIOUS_DRIVE)
        self.assertEqual(self.repair(TRIP, now=at(11, 17, 40)), [])
        repairs = self.repair(TRIP, now=at(11, 19))
        self.assertEqual([(r["km_from"], r["km_to"]) for r in repairs], [(10486.996225, 10520.0)])

    def parked_stop(self, next_start):
        """A drive ends in P, the next live frame comes later; HA rises 1 km per 5 to 12 minutes."""
        self.drive(1, at(11, 20), at(11, 20, 16, 0, 552), 29980.0, 29987.003)
        self.drive(2, next_start, at(11, 20, 40), 29988.001, 29995.0)
        records = [
            (at(11, 19, 55), "29980"),
            (at(11, 20, 10), "29987"),
            (at(11, 20, 22, 30), "29988"),
            *steps(at(11, 20, 22, 30), 29988, at(11, 20, 40), 29995),
        ]
        return self.repair(records, now=at(11, 23))

    def test_start_after_a_short_stop_comes_from_the_gap_km_not_the_previous_drive_end(self):
        repairs = self.parked_stop(at(11, 20, 22, 20, 10))
        self.assertEqual([r["kind"] for r in repairs], ["extended_start"])
        # 0.998 km at 90 km/h take 39.92 s.
        expected = at(11, 20, 22, 20, 10) - timedelta(seconds=39.92)
        self.assertAlmostEqual(repairs[0]["start_at"], expected, delta=timedelta(milliseconds=10))
        self.assertEqual(self.row("drives", 2)["start_date"], repairs[0]["start_at"])

    def test_start_estimated_from_the_gap_km_is_not_earlier_than_the_previous_drive_end(self):
        repairs = self.parked_stop(at(11, 20, 16, 30))
        self.assertEqual([r["start_at"] for r in repairs], [at(11, 20, 16, 0, 552)])


class HomeAssistantPayloadTest(unittest.TestCase):
    def test_history_payloads_become_naive_utc_records(self):
        odometer = [[
            {"entity_id": "sensor.odometer", "state": "10487", "last_changed": "2026-03-11T15:00:00+00:00"},
            {"state": "unavailable", "last_changed": "2026-03-11T16:57:17.5+01:00"},
        ]]
        self.assertEqual(
            repairer.odometer_records(odometer),
            [(at(11, 15), "10487"), (at(11, 15, 57, 17, 500), "unavailable")],
        )
        tracker = [[
            {"state": "home", "attributes": {"latitude": 12.0, "longitude": 34.0},
             "last_changed": "2026-03-11T14:00:00+00:00", "last_updated": "2026-03-11T16:55:00+00:00"},
            {"state": "not_home", "attributes": {}, "last_changed": "2026-03-11T17:00:00+00:00",
             "last_updated": "2026-03-11T17:00:00+00:00"},
        ]]
        self.assertEqual(repairer.location_records(tracker), [(at(11, 16, 55), 12.0, 34.0)])


if __name__ == "__main__":
    unittest.main()
