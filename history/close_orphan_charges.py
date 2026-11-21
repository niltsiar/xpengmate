"""Close charging processes that a restart left open.

The app keeps the open charging process of a car in memory, and only
``complete_charging_process`` sets ``end_date``. A restart in the middle of a
charge therefore leaves ``end_date`` NULL for good, and the app opens a new
process when the car reports again.

An open process is an orphan when the same car has a later charging process or
a later drive: the app holds at most one open process per car, so the live one
never has a successor. Energy fields are left alone.

The queries use only plain columns, ORDER BY and LIMIT, so they run on
PostgreSQL and on SQLite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger("xpengmate.history.close_orphan_charges")

_SUCCESSOR_QUERIES = (
    "SELECT start_date FROM charging_processes"
    " WHERE car_id = %s AND start_date > %s ORDER BY start_date LIMIT 1",
    "SELECT start_date FROM drives"
    " WHERE car_id = %s AND start_date > %s ORDER BY start_date LIMIT 1",
)


def pick_end_date(
    start: datetime,
    successor_start: datetime,
    last_charge: datetime | None,
    last_position: datetime | None,
) -> tuple[datetime, str]:
    """End of an orphan: last charge sample, else last position, else the successor's start."""
    if last_charge is not None:
        return last_charge, "charges"
    if last_position is not None:
        return last_position, "positions"
    return successor_start, "successor"


def duration_min(start: datetime, end: datetime) -> int:
    """Whole minutes, rounded half up like the ``::integer`` cast the app uses."""
    return int((end - start).total_seconds() / 60 + 0.5)


def _first(cursor: Any, sql: str, params: tuple[Any, ...]) -> Any:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


def close_orphans(cursor: Any) -> list[dict[str, Any]]:
    """Close every orphaned charging process; return what was closed."""
    cursor.execute(
        "SELECT id, car_id, start_date FROM charging_processes"
        " WHERE end_date IS NULL ORDER BY start_date"
    )
    closed = []
    for process_id, car_id, start in cursor.fetchall():
        starts = [
            found
            for sql in _SUCCESSOR_QUERIES
            if (found := _first(cursor, sql, (car_id, start))) is not None
        ]
        if not starts:
            continue
        successor = min(starts)
        last_charge = _first(
            cursor,
            "SELECT date FROM charges WHERE charging_process_id = %s ORDER BY date DESC LIMIT 1",
            (process_id,),
        )
        last_position = _first(
            cursor,
            "SELECT date FROM positions WHERE car_id = %s AND date >= %s AND date < %s"
            " ORDER BY date DESC LIMIT 1",
            (car_id, start, successor),
        )
        end, source = pick_end_date(start, successor, last_charge, last_position)
        minutes = duration_min(start, end)
        cursor.execute(
            "UPDATE charging_processes SET end_date = %s, duration_min = %s"
            " WHERE id = %s AND end_date IS NULL",
            (end, minutes, process_id),
        )
        _LOGGER.info(
            "closed orphaned charging process %s of car %s: end_date=%s (from %s), duration_min=%s",
            process_id,
            car_id,
            end.isoformat(),
            source,
            minutes,
        )
        closed.append({"id": process_id, "car_id": car_id, "end_date": end, "source": source})
    return closed
