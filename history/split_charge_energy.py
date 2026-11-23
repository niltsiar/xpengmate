"""Split each charge's energy into energy used and energy added.

The engine sends the car's own session energy (``charge_capacity``) as
``charge_energy_added`` on every charge sample. That is the energy at the car's
inlet, which TeslaMate calls energy used. The energy added to the battery is
known only from the battery levels: (end - start) / 100 x usable kWh.

For each completed charging process of the car that this job has not handled:

* with a ``charge_energy_added`` (the car's inlet energy): ``charge_energy_used``
  takes that value and ``charge_energy_added`` takes the battery-level estimate,
  or stays as it is when a battery level is NULL,
* without one: ``charge_energy_added`` takes the battery-level estimate and
  ``charge_energy_used`` (TeslaMate's own V x I integration) stays.

Each handled process is a row in ``xpengmate_charge_energy_fixes``, so a rerun
changes nothing. Processes that ended before ``xpengmate_charge_energy_job`` was
created keep the values they were imported with.

The queries use only plain columns, so they run on PostgreSQL and on SQLite.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from repair_drive_gaps import find_car

_LOGGER = logging.getLogger("xpengmate.history.split_charge_energy")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS xpengmate_charge_energy_job (
        created_at  timestamp  NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xpengmate_charge_energy_fixes (
        charging_process_id  bigint     PRIMARY KEY,
        car_id               bigint     NOT NULL,
        handled_at           timestamp  NOT NULL
    )
    """,
)


def parse_usable_kwh(value: Any) -> float | None:
    """The configured usable capacity, or None when it is unset or not a positive number."""
    try:
        kwh = float(value)
    except (TypeError, ValueError):
        return None
    return kwh if math.isfinite(kwh) and kwh > 0 else None


def soc_energy_kwh(start_level: Any, end_level: Any, usable_kwh: float) -> float | None:
    """Energy added to the battery from the battery levels; None when a level is NULL."""
    if start_level is None or end_level is None:
        return None
    return round(max(0, end_level - start_level) / 100 * usable_kwh, 2)


def _first(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


def split_charge_energy(
    cursor: Any, car_id: int, usable_kwh: float, now: datetime
) -> list[dict[str, Any]]:
    """Handle the car's completed charging processes; return what was changed."""
    for statement in SCHEMA:
        cursor.execute(statement)
    since = _first(cursor, "SELECT created_at FROM xpengmate_charge_energy_job ORDER BY created_at LIMIT 1")
    if since is None:
        cursor.execute("INSERT INTO xpengmate_charge_energy_job (created_at) VALUES (%s)", (now,))
        since = now
    cursor.execute(
        "SELECT id, charge_energy_added, start_battery_level, end_battery_level"
        " FROM charging_processes p"
        " WHERE car_id = %s AND end_date IS NOT NULL AND end_date >= %s"
        " AND NOT EXISTS (SELECT 1 FROM xpengmate_charge_energy_fixes f"
        " WHERE f.charging_process_id = p.id)"
        " ORDER BY id",
        (car_id, since),
    )
    handled = []
    for process_id, car_value, start_level, end_level in cursor.fetchall():
        fields: dict[str, Any] = {}
        estimate = soc_energy_kwh(start_level, end_level, usable_kwh)
        if car_value is not None:
            fields["charge_energy_used"] = car_value
        if estimate is not None:
            fields["charge_energy_added"] = estimate
        if fields:
            cursor.execute(
                f"UPDATE charging_processes SET {', '.join(f'{column} = %s' for column in fields)}"
                " WHERE id = %s",
                (*fields.values(), process_id),
            )
        cursor.execute(
            "INSERT INTO xpengmate_charge_energy_fixes (charging_process_id, car_id, handled_at)"
            " VALUES (%s, %s, %s)",
            (process_id, car_id, now),
        )
        if fields:
            _LOGGER.info(
                "split energy of charging process %s of car %s: %s",
                process_id,
                car_id,
                ", ".join(f"{column}={value}" for column, value in fields.items()),
            )
            handled.append({"id": process_id, "car_id": car_id, **fields})
    return handled


def run(connection: Any, vin: str, usable_kwh: float) -> list[dict[str, Any]]:
    """One pass in one transaction, so a process is never changed without its record."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with connection.transaction():
        with connection.cursor() as cursor:
            car_id = find_car(cursor, vin)
            if car_id is None:
                _LOGGER.info("charge energy split found no car: set XPENG_VIN")
                return []
            return split_charge_energy(cursor, car_id, usable_kwh, now)
