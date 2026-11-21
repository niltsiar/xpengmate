"""Close charging processes that a restart left open.

The app keeps the open charging process of a car in memory, and only
``complete_charging_process`` sets ``end_date``. A restart in the middle of a
charge therefore leaves ``end_date`` NULL for good, and the app opens a new
process when the car reports again.

An open process is an orphan when the same car has a later charging process or
a later drive: the app holds at most one open process per car, so the live one
never has a successor. When the orphan has charge samples, the closer fills the
fields ``complete_charging_process`` fills from them: start and end date,
duration, ranges, battery level, average outside temperature, energy added and
energy used. Cost and car efficiency stay untouched. Without samples only
``end_date`` and ``duration_min`` are set.

The queries use only plain columns, ORDER BY and LIMIT, so they run on
PostgreSQL and on SQLite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger("xpengmate.history.close_orphan_charges")

_SAMPLE_COLUMNS = (
    "date",
    "ideal_battery_range_km",
    "rated_battery_range_km",
    "battery_level",
    "outside_temp",
    "charge_energy_added",
    "charger_power",
    "charger_phases",
    "charger_actual_current",
    "charger_voltage",
)

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


def _number(value: Any) -> float | None:
    """Float for arithmetic: PostgreSQL returns Decimal for numeric columns."""
    return None if value is None else float(value)


def energy_added(values: list[Any]) -> float | None:
    """Last counter value (the max if the last is 0 or NULL) minus the first; None if NULL or negative."""
    numbers = [_number(value) for value in values]
    last = numbers[-1] or max((n for n in numbers if n is not None), default=None)
    first = numbers[0]
    if last is None or first is None or last - first < 0:
        return None
    return last - first


def energy_used(samples: list[dict[str, Any]]) -> float | None:
    """kWh from power and the time since the previous sample; None without a usable term."""
    # Simpler than the app's determine_phases: phases is the rounded average, with no corrections.
    phase_values = [s["charger_phases"] for s in samples if s["charger_phases"] is not None]
    phases = int(sum(phase_values) / len(phase_values) + 0.5) if phase_values else None
    terms = []
    for previous, sample in zip(samples, samples[1:]):
        power = _number(sample["charger_power"])
        current = _number(sample["charger_actual_current"])
        voltage = _number(sample["charger_voltage"])
        if sample["charger_phases"] is not None and None not in (phases, current, voltage):
            power = phases * current * voltage / 1000
        if power is None:
            continue
        term = power * (sample["date"] - previous["date"]).total_seconds() / 3600
        if term >= 0:
            terms.append(term)
    return sum(terms) if terms else None


def sample_fields(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Columns of ``charging_processes`` that the app derives from the process's samples."""
    first, last = samples[0], samples[-1]
    temperatures = [_number(s["outside_temp"]) for s in samples if s["outside_temp"] is not None]
    fields: dict[str, Any] = {
        "start_date": first["date"],
        "outside_temp_avg": sum(temperatures) / len(temperatures) if temperatures else None,
        "charge_energy_added": energy_added([s["charge_energy_added"] for s in samples]),
        "charge_energy_used": energy_used(samples),
    }
    for column, suffix in (
        ("ideal_battery_range_km", "ideal_range_km"),
        ("rated_battery_range_km", "rated_range_km"),
        ("battery_level", "battery_level"),
    ):
        fields["start_" + suffix] = first[column]
        fields["end_" + suffix] = last[column]
    return fields


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
        cursor.execute(
            f"SELECT {', '.join(_SAMPLE_COLUMNS)} FROM charges"
            " WHERE charging_process_id = %s ORDER BY date",
            (process_id,),
        )
        samples = [dict(zip(_SAMPLE_COLUMNS, row)) for row in cursor.fetchall()]
        last_position = _first(
            cursor,
            "SELECT date FROM positions WHERE car_id = %s AND date >= %s AND date < %s"
            " ORDER BY date DESC LIMIT 1",
            (car_id, start, successor),
        )
        end, source = pick_end_date(
            start, successor, samples[-1]["date"] if samples else None, last_position
        )
        fields = sample_fields(samples) if samples else {}
        start = fields.get("start_date", start)
        fields["end_date"] = end
        fields["duration_min"] = minutes = duration_min(start, end)
        cursor.execute(
            f"UPDATE charging_processes SET {', '.join(f'{column} = %s' for column in fields)}"
            " WHERE id = %s AND end_date IS NULL",
            (*fields.values(), process_id),
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
