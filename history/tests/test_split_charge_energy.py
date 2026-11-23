import sqlite3
import unittest
from datetime import datetime, timedelta

import split_charge_energy as splitter

sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
sqlite3.register_converter("TIMESTAMP", lambda raw: datetime.fromisoformat(raw.decode()))

USABLE_KWH = 87.5
NOW = datetime(2026, 3, 10, 12)


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


class SocEnergyTest(unittest.TestCase):
    def test_level_difference_of_the_usable_capacity_rounded_to_two_decimals(self):
        self.assertEqual(splitter.soc_energy_kwh(40, 50, 87.5), 8.75)
        self.assertEqual(splitter.soc_energy_kwh(10, 13, 87.4), 2.62)

    def test_a_falling_level_adds_nothing(self):
        self.assertEqual(splitter.soc_energy_kwh(50, 45, 87.5), 0)

    def test_a_missing_level_gives_none(self):
        self.assertIsNone(splitter.soc_energy_kwh(None, 50, 87.5))
        self.assertIsNone(splitter.soc_energy_kwh(40, None, 87.5))


class ParseUsableKwhTest(unittest.TestCase):
    def test_only_a_positive_number_is_configured(self):
        self.assertEqual(splitter.parse_usable_kwh("87.5"), 87.5)
        for value in (None, "", "abc", 0, "-3", "nan"):
            self.assertIsNone(splitter.parse_usable_kwh(value), value)


class SplitChargeEnergyTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.execute(
            "CREATE TABLE charging_processes (id INTEGER PRIMARY KEY, car_id INTEGER,"
            " end_date TIMESTAMP, charge_energy_added REAL, charge_energy_used REAL,"
            " start_battery_level INTEGER, end_battery_level INTEGER)"
        )
        self.cursor = _PsycopgStyleCursor(self.db.cursor())
        # The first run creates the job table, and only later charges count.
        splitter.split_charge_energy(self.cursor, 1, USABLE_KWH, NOW - timedelta(days=1))

    def process(self, pid, added, used, start, end, ended=NOW - timedelta(hours=1), car=1):
        self.db.execute(
            "INSERT INTO charging_processes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, car, ended, added, used, start, end),
        )

    def row(self, pid):
        return self.db.execute(
            "SELECT charge_energy_added, charge_energy_used FROM charging_processes WHERE id = ?",
            (pid,),
        ).fetchone()

    def run_job(self, now=NOW):
        return splitter.split_charge_energy(self.cursor, 1, USABLE_KWH, now)

    def test_the_cars_value_becomes_energy_used_and_the_levels_give_energy_added(self):
        self.process(1, 26.6, 24.0, 40, 50)
        self.process(2, 3.0, 2.0, 50, 45)
        self.run_job()
        self.assertEqual(self.row(1), (8.75, 26.6))
        self.assertEqual(self.row(2), (0, 3.0))

    def test_without_the_cars_value_the_levels_fill_added_and_used_stays(self):
        self.process(1, None, 5.5, 40, 50)
        self.run_job()
        self.assertEqual(self.row(1), (8.75, 5.5))

    def test_a_missing_battery_level_still_sets_used_and_leaves_added(self):
        self.process(1, 12.0, 3.0, None, 50)
        self.process(2, None, 4.0, 40, None)
        self.run_job()
        self.assertEqual(self.row(1), (12.0, 12.0))
        self.assertEqual(self.row(2), (None, 4.0))

    def test_a_second_run_changes_nothing(self):
        self.process(1, 26.6, 24.0, 40, 50)
        first = self.run_job()
        after_first = self.row(1)
        second = self.run_job(NOW + timedelta(hours=1))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.row(1), after_first)
        self.assertEqual(after_first, (8.75, 26.6))

    def test_processes_ended_before_the_job_existed_open_ones_and_other_cars_are_untouched(self):
        job_created = NOW - timedelta(days=1)
        self.process(1, 26.6, 24.0, 40, 50, ended=job_created - timedelta(minutes=1))
        self.process(2, 26.6, 24.0, 40, 50, ended=None)
        self.process(3, 26.6, 24.0, 40, 50, car=2)
        self.process(4, 26.6, 24.0, 40, 50, ended=job_created)
        self.run_job()
        for pid in (1, 2, 3):
            self.assertEqual(self.row(pid), (26.6, 24.0), pid)
        self.assertEqual(self.row(4), (8.75, 26.6))


if __name__ == "__main__":
    unittest.main()
