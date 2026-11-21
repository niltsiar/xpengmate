import sqlite3
import unittest
from datetime import datetime

import close_orphan_charges as closer

sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
sqlite3.register_converter("TIMESTAMP", lambda raw: datetime.fromisoformat(raw.decode()))


def at(hour, minute=0, day=1):
    return datetime(2026, 1, day, hour, minute)


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


class PickEndDateTest(unittest.TestCase):
    def test_charge_sample_beats_position_and_successor(self):
        end, source = closer.pick_end_date(at(2), at(9), at(3), at(4))
        self.assertEqual((end, source), (at(3), "charges"))

    def test_position_beats_successor_when_no_charge_sample(self):
        end, source = closer.pick_end_date(at(2), at(9), None, at(4))
        self.assertEqual((end, source), (at(4), "positions"))

    def test_successor_start_is_the_last_resort(self):
        end, source = closer.pick_end_date(at(2), at(9), None, None)
        self.assertEqual((end, source), (at(9), "successor"))


class DurationTest(unittest.TestCase):
    def test_rounds_to_the_nearest_minute_like_postgres(self):
        self.assertEqual(closer.duration_min(at(2, 0), datetime(2026, 1, 1, 2, 1, 29)), 1)
        self.assertEqual(closer.duration_min(at(2, 0), datetime(2026, 1, 1, 2, 1, 30)), 2)
        self.assertEqual(closer.duration_min(at(2, 0), datetime(2026, 1, 1, 2, 0, 30)), 1)


class CloseOrphansTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.executescript(
            """
            CREATE TABLE charging_processes (
                id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP,
                end_date TIMESTAMP, duration_min INTEGER, charge_energy_added REAL,
                charge_energy_used REAL, outside_temp_avg REAL,
                start_ideal_range_km REAL, end_ideal_range_km REAL,
                start_rated_range_km REAL, end_rated_range_km REAL,
                start_battery_level INTEGER, end_battery_level INTEGER);
            CREATE TABLE drives (id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP);
            CREATE TABLE charges (
                id INTEGER PRIMARY KEY, charging_process_id INTEGER, date TIMESTAMP,
                ideal_battery_range_km REAL, rated_battery_range_km REAL, battery_level INTEGER,
                outside_temp REAL, charge_energy_added REAL, charger_power INTEGER,
                charger_phases INTEGER, charger_actual_current INTEGER, charger_voltage INTEGER);
            CREATE TABLE positions (id INTEGER PRIMARY KEY, car_id INTEGER, date TIMESTAMP);
            """
        )
        self.cursor = _PsycopgStyleCursor(self.db.cursor())

    def process(self, pid, car, start, end=None, energy=7.5):
        self.db.execute(
            "INSERT INTO charging_processes (id, car_id, start_date, end_date, charge_energy_added)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid, car, start, end, energy),
        )

    def sample(self, pid, moment, **values):
        columns = {"charging_process_id": pid, "date": moment, **values}
        self.db.execute(
            f"INSERT INTO charges ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(columns.values()),
        )

    def filled(self, pid):
        cursor = self.db.execute("SELECT * FROM charging_processes WHERE id = ?", (pid,))
        return dict(zip([c[0] for c in cursor.description], cursor.fetchone()))

    def row(self, pid):
        return self.db.execute(
            "SELECT end_date, duration_min, charge_energy_added FROM charging_processes WHERE id = ?",
            (pid,),
        ).fetchone()

    def test_process_with_a_later_charge_is_closed_at_its_last_sample(self):
        self.process(1, 1, at(2, 44))
        self.process(2, 1, at(9, 37))
        self.sample(1, at(4, 10), charge_energy_added=1.0)
        self.sample(1, at(5, 14), charge_energy_added=8.5)
        closed = closer.close_orphans(self.cursor)
        self.assertEqual([c["id"] for c in closed], [1])
        self.assertEqual(self.row(1), (at(5, 14), 64, 7.5))
        self.assertEqual(self.row(2)[0], None)

    def test_process_with_a_later_drive_is_closed(self):
        self.process(1, 1, at(2))
        self.db.execute("INSERT INTO drives VALUES (1, 1, ?)", (at(8),))
        closer.close_orphans(self.cursor)
        self.assertEqual(self.row(1)[:2], (at(8), 360))

    def test_samples_fill_energy_soc_and_ranges(self):
        self.process(1, 1, at(2), energy=None)
        self.process(2, 1, at(9))
        self.sample(1, at(3, 0), ideal_battery_range_km=100.0, rated_battery_range_km=90.0,
                    battery_level=20, outside_temp=10.0, charge_energy_added=0.0,
                    charger_power=10)
        self.sample(1, at(4, 0), ideal_battery_range_km=150.0, rated_battery_range_km=135.0,
                    battery_level=40, outside_temp=None, charge_energy_added=9.0,
                    charger_power=10)
        self.sample(1, at(5, 0), ideal_battery_range_km=200.0, rated_battery_range_km=180.0,
                    battery_level=60, outside_temp=14.0, charge_energy_added=18.0,
                    charger_power=-5)
        closer.close_orphans(self.cursor)
        row = self.filled(1)
        self.assertEqual((row["start_date"], row["end_date"], row["duration_min"]),
                         (at(3), at(5), 120))
        self.assertEqual((row["start_ideal_range_km"], row["end_ideal_range_km"]), (100.0, 200.0))
        self.assertEqual((row["start_rated_range_km"], row["end_rated_range_km"]), (90.0, 180.0))
        self.assertEqual((row["start_battery_level"], row["end_battery_level"]), (20, 60))
        self.assertEqual(row["outside_temp_avg"], 12.0)
        self.assertEqual(row["charge_energy_added"], 18.0)
        # 10 kW for 1 h; the negative-power term is dropped.
        self.assertEqual(row["charge_energy_used"], 10.0)

    def test_energy_used_of_a_single_phase_charge_comes_from_voltage_and_current(self):
        self.process(1, 1, at(2), energy=None)
        self.process(2, 1, at(9))
        for moment in (at(3), at(4), at(5)):
            self.sample(1, moment, charger_phases=1, charger_actual_current=16,
                        charger_voltage=230, charger_power=99)
        closer.close_orphans(self.cursor)
        self.assertAlmostEqual(self.filled(1)["charge_energy_used"], 2 * 16 * 230 / 1000)

    def test_energy_counter_reset_to_zero_at_the_end_uses_the_max(self):
        self.process(1, 1, at(2), energy=None)
        self.process(2, 1, at(9))
        for moment, kwh in ((at(3), 2.0), (at(4), 12.0), (at(5), 0.0)):
            self.sample(1, moment, charge_energy_added=kwh)
        closer.close_orphans(self.cursor)
        self.assertEqual(self.filled(1)["charge_energy_added"], 10.0)

    def test_negative_energy_added_is_stored_as_null(self):
        self.process(1, 1, at(2))
        self.process(2, 1, at(9))
        self.sample(1, at(3), charge_energy_added=5.0)
        self.sample(1, at(4), charge_energy_added=3.0)
        closer.close_orphans(self.cursor)
        self.assertIsNone(self.filled(1)["charge_energy_added"])

    def test_process_without_samples_keeps_its_energy_fields(self):
        self.process(1, 1, at(2))
        self.process(2, 1, at(9))
        closer.close_orphans(self.cursor)
        row = self.filled(1)
        self.assertEqual((row["start_date"], row["end_date"]), (at(2), at(9)))
        self.assertEqual(row["charge_energy_added"], 7.5)
        self.assertIsNone(row["charge_energy_used"])

    def test_process_without_a_successor_is_left_open(self):
        self.process(1, 1, at(2))
        self.sample(1, at(3))
        self.db.execute("INSERT INTO drives VALUES (1, 1, ?)", (at(1),))
        self.assertEqual(closer.close_orphans(self.cursor), [])
        self.assertEqual(self.row(1)[:2], (None, None))

    def test_successor_of_another_car_does_not_count(self):
        self.process(1, 1, at(2))
        self.process(2, 2, at(9))
        self.assertEqual([c["id"] for c in closer.close_orphans(self.cursor)], [])
        self.assertEqual(self.row(1)[0], None)

    def test_only_positions_between_the_start_and_the_successor_count(self):
        self.process(1, 1, at(2))
        self.process(2, 1, at(9))
        for number, moment in enumerate([at(1), at(5), at(6), at(9, 30)], start=1):
            self.db.execute("INSERT INTO positions VALUES (?, 1, ?)", (number, moment))
        closer.close_orphans(self.cursor)
        self.assertEqual(self.row(1)[:2], (at(6), 240))

    def test_closes_with_the_successor_start_without_charges_or_positions(self):
        self.process(1, 1, at(2))
        self.process(2, 1, at(9))
        closer.close_orphans(self.cursor)
        self.assertEqual(self.row(1)[:2], (at(9), 420))

    def test_already_closed_process_is_untouched(self):
        self.process(1, 1, at(2), end=at(3))
        self.process(2, 1, at(9), end=at(10))
        self.assertEqual(closer.close_orphans(self.cursor), [])
        self.assertEqual(self.row(1)[:2], (at(3), None))

    def test_second_run_closes_nothing_more(self):
        self.process(1, 1, at(2))
        self.process(2, 1, at(9))
        self.assertEqual(len(closer.close_orphans(self.cursor)), 1)
        self.assertEqual(closer.close_orphans(self.cursor), [])


if __name__ == "__main__":
    unittest.main()
