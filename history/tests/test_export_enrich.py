import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from unittest import mock

import export_enrich
from test_export_import import _Connection


def samples(*timers):
    return iter([(timer, {"odometer_km": float(timer)}) for timer in timers])


def pairs(positions, stream):
    return [(position_id, sample[0]) for position_id, sample in export_enrich.nearest_samples(positions, stream)]


class NearestSampleTest(unittest.TestCase):
    def test_position_takes_the_sample_at_its_own_second(self):
        self.assertEqual(pairs([(1, 1000)], samples(*range(995, 1006))), [(1, 1000)])

    def test_position_between_samples_takes_the_closer_one(self):
        self.assertEqual(pairs([(1, 1000)], samples(997, 1002)), [(1, 1002)])
        self.assertEqual(pairs([(1, 1000)], samples(999, 1004)), [(1, 999)])

    def test_position_with_no_sample_within_five_seconds_is_left_out(self):
        self.assertEqual(pairs([(1, 1000), (2, 2000)], samples(994, 1006, 2003)), [(2, 2003)])

    def test_positions_in_one_gap_each_take_their_own_nearest_sample(self):
        self.assertEqual(
            pairs([(1, 1000), (2, 1004), (3, 1009)], samples(999, 1005, 1010)),
            [(1, 999), (2, 1005), (3, 1010)],
        )


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def export_zip(directory, name, rows):
    """A minimal export with (timer, odometer, soc, range, volt, amp) rows."""
    path = os.path.join(directory, name)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "car_driving_operation_di.csv",
            "timer,cdcu_totalodometer\n" + "".join(f"{row[0]},{row[1]}\n" for row in rows),
        )
        archive.writestr(
            "car_driving_power_energy_di.csv",
            "timer,ldcu_bms_soc_disp,ldcu_dstbatdisp_dynamic,bms_battvolt,bms_battcurr\n"
            + "".join(f"{t},{soc},{rng},{volt},{amp}\n" for t, _odo, soc, rng, volt, amp in rows),
        )
    return path


class SamplesAtTest(unittest.TestCase):
    def test_each_moment_takes_its_nearest_sample_from_the_first_zip_that_has_one(self):
        first_moment = utc(1000)
        second_moment = utc(5000)
        missing = utc(9000)
        with tempfile.TemporaryDirectory() as directory:
            older = export_zip(
                directory, "older.zip", [(999, 100.04, 80, 300.0, 400, 0), (1001, 100.1, 79, 299.0, 400, 0)]
            )
            newer = export_zip(
                directory, "newer.zip", [(1000, 555.0, 1, 1.0, 400, 0), (5002, 120.0, 70, 250.555, 400, 0)]
            )

            found = export_enrich.samples_at([older, newer], [second_moment, first_moment, missing])

        self.assertEqual(found, {first_moment: (80, 100.0, 300.0), second_moment: (70, 120.0, 250.56)})


class IterSamplesTest(unittest.TestCase):
    def test_a_day_block_out_of_place_in_one_dataset_still_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "export.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "car_driving_operation_di.csv",
                    "timer,cdcu_totalodometer\n3,12.0\n4,13.0\n1,10.0\n2,11.0\n",
                )
                archive.writestr(
                    "car_driving_power_energy_di.csv",
                    "timer,ldcu_bms_soc_disp,ldcu_dstbatdisp_dynamic,bms_battvolt,bms_battcurr\n"
                    "1,80,300,400,1\n2,80,300,400,2\n3,80,300,400,3\n4,80,300,,4\n",
                )

            joined = [(timer, sample["odometer_km"], sample["amp"], sample["volt"]) for timer, sample in export_enrich.iter_samples(path)]

        self.assertEqual(joined, [(1, 10.0, 1.0, 400.0), (2, 11.0, 2.0, 400.0), (3, 12.0, 3.0, 400.0), (4, 13.0, 4.0, None)])


def power(*rows):
    """Samples from (timer, volt, amp) rows."""
    return iter([(timer, {"volt": volt, "amp": amp}) for timer, volt, amp in rows])


class DriveEnergyTest(unittest.TestCase):
    def test_used_and_recovered_integrate_the_samples_inside_the_window(self):
        energy = export_enrich.drive_energy(
            [(1, 100.0, 115.0)],
            power(
                (99, 400, 500),  # before the drive
                (100, 400, 90),  # 36 kW, the first sample has no step
                (101, 400, 90),
                (102, 400, 90),
                (103, 400, -45),  # -18 kW: regeneration
                (104, 400, -45),
                (105, 1023, 90),  # voltage not available: dropped
                (106, 400, 90),  # 2 s after the last valid sample
                (112, 400, 90),  # 6 s gap in the export: no energy for the step
                (116, 400, 500),  # after the drive
            ),
        )

        self.assertEqual(list(energy), [1])
        self.assertAlmostEqual(energy[1].used_kwh, 36 * 4 / 3600)
        self.assertAlmostEqual(energy[1].recovered_kwh, 18 * 2 / 3600)
        self.assertEqual((energy[1].power_max, energy[1].power_min), (36.0, -18.0))
        self.assertEqual(energy[1].seconds, 6)

    def test_a_gap_in_the_middle_of_a_window_loses_its_step_and_keeps_both_parts(self):
        energy = export_enrich.drive_energy(
            [(1, 100.0, 120.0)],
            power(*[(timer, 400, 90) for timer in (*range(100, 104), *range(110, 114))]),
        )

        self.assertAlmostEqual(energy[1].used_kwh, 36 * 6 / 3600)
        self.assertEqual(energy[1].seconds, 6)

    def test_two_drives_in_one_trip_each_take_only_their_own_steps(self):
        energy = export_enrich.drive_energy(
            [(2, 103.0, 105.0), (1, 100.0, 102.0)],
            power(*[(timer, 400, 90) for timer in range(100, 106)]),
        )

        self.assertAlmostEqual(energy[1].used_kwh, 36 * 2 / 3600)
        self.assertAlmostEqual(energy[2].used_kwh, 36 * 2 / 3600)


class StoreDriveEnergyTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.executescript(
            """
            CREATE TABLE drives (id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP,
                                 end_date TIMESTAMP, power_max INTEGER, power_min INTEGER);
            """
        )
        self.db.execute(export_enrich.DRIVE_ENERGY_SCHEMA)
        self.store = type("Store", (), {"conn": _Connection(self.db), "car_id": 1})()
        self.db.execute("INSERT INTO drives VALUES (1, 1, ?, ?, 0, 0)", (utc(1002), utc(1006)))
        # Starts before the export does, so the export has only part of it.
        self.db.execute("INSERT INTO drives VALUES (2, 1, ?, ?, 0, 0)", (utc(990), utc(1004)))

    def test_drives_the_export_covers_whole_get_energy_and_power_extremes(self):
        rows = [(t, 100.0, 80, 300.0, 400, 90 if t != 1004 else -45) for t in range(1000, 1010)]
        with tempfile.TemporaryDirectory() as directory:
            path = export_zip(directory, "export.zip", rows)
            export_enrich.store_drive_energy(self.store, path, 1000, 1009)
            stored = export_enrich.store_drive_energy(self.store, path, 1000, 1009)

        self.assertEqual(stored, 1)
        self.assertEqual(
            self.db.execute("SELECT * FROM xpengmate_drive_energy").fetchall(),
            [(1, round(36 * 3 / 3600, 3), round(18 / 3600, 3))],
        )
        self.assertEqual(
            self.db.execute("SELECT id, power_max, power_min FROM drives ORDER BY id").fetchall(),
            [(1, 36, -18), (2, 0, 0)],
        )


    def test_a_drive_with_a_hole_over_ten_percent_gets_no_energy_and_keeps_its_power_extremes(self):
        self.db.execute("INSERT INTO drives VALUES (3, 1, ?, ?, 0, 0)", (utc(3000), utc(3060)))
        rows = [(t, 100.0, 80, 300.0, 400, 90) for t in range(1000, 3100) if not 3020 < t < 3030]
        with tempfile.TemporaryDirectory() as directory:
            path = export_zip(directory, "export.zip", rows)
            with self.assertLogs("xpengmate.export", "WARNING") as logs:
                stored = export_enrich.store_drive_energy(self.store, path, 1000, 3100)

        self.assertEqual(stored, 1)
        self.assertEqual([row[0] for row in self.db.execute("SELECT drive_id FROM xpengmate_drive_energy")], [1])
        self.assertEqual(self.db.execute("SELECT power_max, power_min FROM drives WHERE id = 3").fetchone(), (0, 0))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("[3]", logs.output[0])

    def test_a_drive_the_export_covers_at_95_percent_gets_its_energy(self):
        self.db.execute("INSERT INTO drives VALUES (4, 1, ?, ?, 0, 0)", (utc(2000), utc(2120)))
        rows = [(t, 100.0, 80, 300.0, 400, 90) for t in range(1000, 2200) if not 2050 < t < 2056]
        with tempfile.TemporaryDirectory() as directory:
            path = export_zip(directory, "export.zip", rows)
            export_enrich.store_drive_energy(self.store, path, 1000, 2200)

        self.assertEqual(
            self.db.execute("SELECT energy_used_kwh FROM xpengmate_drive_energy WHERE drive_id = 4").fetchone(),
            (round(36 * 114 / 3600, 3),),
        )
        self.assertEqual(self.db.execute("SELECT power_max FROM drives WHERE id = 4").fetchone(), (36,))


class _EnrichCursor:
    """SQLite for the position UPDATE; the PostgreSQL-only statements are answered by hand."""

    def __init__(self, db, positions):
        self.db = db
        self.positions = positions
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if "extract(epoch" in sql:
            self.rows = self.positions
        elif "DISTINCT ON" not in sql:
            self.db.execute(sql.replace("%s", "?"), params)

    def executemany(self, sql, rows):
        self.db.executemany(sql.replace("%s", "?"), rows)

    def fetchall(self):
        return self.rows


class EnrichTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY, battery_level INTEGER, usable_battery_level INTEGER,
                odometer REAL, est_battery_range_km REAL, ideal_battery_range_km REAL,
                rated_battery_range_km REAL, power INTEGER);
            INSERT INTO positions VALUES (1, NULL, NULL, NULL, NULL, NULL, 240.0, NULL);
            INSERT INTO positions VALUES (2, NULL, NULL, NULL, NULL, NULL, 245.0, NULL);
            """
        )
        cursor = _EnrichCursor(self.db, [(1, 1000, True), (2, 2000, True)])
        connection = type("Connection", (), {"cursor": lambda self: cursor, "commit": lambda self: None})()
        self.store = type("Store", (), {"conn": connection, "car_id": 1})()

    def test_the_export_range_sets_rated_and_ideal_and_a_sample_without_range_keeps_rated(self):
        samples = iter(
            [
                (1000, {"soc": 80.0, "odometer_km": 100.0, "range_km": 300.0}),
                (2000, {"soc": 70.0}),
            ]
        )
        with mock.patch.object(export_enrich, "resolve_zip", lambda path: path):
            with mock.patch.object(export_enrich, "iter_samples", lambda path: samples):
                export_enrich.enrich(self.store, "export.zip")

        self.assertEqual(
            self.db.execute(
                "SELECT id, ideal_battery_range_km, rated_battery_range_km FROM positions ORDER BY id"
            ).fetchall(),
            [(1, 300.0, 300.0), (2, None, 245.0)],
        )


class PositionUpdatesTest(unittest.TestCase):
    def test_only_drive_positions_take_the_pack_power(self):
        stream = iter(
            [
                (1000, {"soc": 80.0, "odometer_km": 100.0, "range_km": 300.0, "volt": 400.0, "amp": 90.0}),
                (2000, {"soc": 79.0, "odometer_km": 101.0, "range_km": 299.0, "volt": 400.0, "amp": -45.0}),
                (3000, {"soc": 78.0, "odometer_km": 102.0, "range_km": 298.0, "volt": 1023.0, "amp": 90.0}),
            ]
        )

        updates = export_enrich.position_updates([(1, 1000, True), (2, 2000, False), (3, 3000, True)], stream)

        self.assertEqual(
            updates,
            [(1, 80, 100.0, 300.0, 36), (2, 79, 101.0, 299.0, None), (3, 78, 102.0, 298.0, None)],
        )


if __name__ == "__main__":
    unittest.main()
