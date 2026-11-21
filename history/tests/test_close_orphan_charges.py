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
                end_date TIMESTAMP, duration_min INTEGER, charge_energy_added REAL);
            CREATE TABLE drives (id INTEGER PRIMARY KEY, car_id INTEGER, start_date TIMESTAMP);
            CREATE TABLE charges (id INTEGER PRIMARY KEY, charging_process_id INTEGER, date TIMESTAMP);
            CREATE TABLE positions (id INTEGER PRIMARY KEY, car_id INTEGER, date TIMESTAMP);
            """
        )
        self.cursor = _PsycopgStyleCursor(self.db.cursor())

    def process(self, pid, car, start, end=None, energy=7.5):
        self.db.execute(
            "INSERT INTO charging_processes VALUES (?, ?, ?, ?, NULL, ?)",
            (pid, car, start, end, energy),
        )

    def row(self, pid):
        return self.db.execute(
            "SELECT end_date, duration_min, charge_energy_added FROM charging_processes WHERE id = ?",
            (pid,),
        ).fetchone()

    def test_process_with_a_later_charge_is_closed_at_its_last_sample(self):
        self.process(1, 1, at(2, 44))
        self.process(2, 1, at(9, 37))
        self.db.execute("INSERT INTO charges VALUES (1, 1, ?)", (at(4, 10),))
        self.db.execute("INSERT INTO charges VALUES (2, 1, ?)", (at(5, 14),))
        closed = closer.close_orphans(self.cursor)
        self.assertEqual([c["id"] for c in closed], [1])
        self.assertEqual(self.row(1), (at(5, 14), 150, 7.5))
        self.assertEqual(self.row(2)[0], None)

    def test_process_with_a_later_drive_is_closed(self):
        self.process(1, 1, at(2))
        self.db.execute("INSERT INTO drives VALUES (1, 1, ?)", (at(8),))
        closer.close_orphans(self.cursor)
        self.assertEqual(self.row(1)[:2], (at(8), 360))

    def test_process_without_a_successor_is_left_open(self):
        self.process(1, 1, at(2))
        self.db.execute("INSERT INTO charges VALUES (1, 1, ?)", (at(3),))
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
