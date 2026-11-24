"""Fill the odometer gaps that the live recorder missed.

The car's live data starts minutes into each drive, so the app loses the first
kilometres of drives and sometimes whole drives. Home Assistant keeps the car's
odometer and location history, and this job compares it with the recorded
drives after the fact. Each run of rising odometer samples is a motion episode;
the kilometres of an episode that no drive covers are a gap:

* a gap that runs into a recorded drive extends that drive back,
* a gap after the last recorded drive of the episode extends that drive forward,
* an episode with no recorded drive becomes a new drive with two positions.

The dates of a repaired part are estimates, because the odometer reports only
every few minutes, until the export import confirms them in the repair row
(``confirmed_start_at``, ``confirmed_end_at``). A moved drive end and each new
position take the range and battery level in effect at their date from the
optional HA sensors, else NULL; the old value of a moved end describes another
moment. Each repair is a row in
``xpengmate_drive_repairs``, so a rerun finds nothing new.

Open drives with a later drive or charging process are closed first, from their
positions, like the app's ``close_drive``. A car with a live drive is skipped.

The queries use only plain columns, ORDER BY and LIMIT, so they run on
PostgreSQL and on SQLite.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode

import psycopg

from close_orphan_charges import duration_min

_LOGGER = logging.getLogger("xpengmate.history.repair_drive_gaps")

SETTLE = timedelta(minutes=30)
LOOKBACK = timedelta(days=10)
SPLIT_FLAT_SECONDS = 900
MIN_LEAD_SECONDS = 300
LEAD_SPEED_KMH = 90
MAX_SPEED_KMH = 250
KM_TOLERANCE = 0.5
PRECISE_LOWER_KM = 1.0

GAP_KINDS = ("inserted", "extended_start", "extended_end")

SCHEMA = """
CREATE TABLE IF NOT EXISTS xpengmate_drive_repairs (
    id          bigserial        PRIMARY KEY,
    car_id      bigint           NOT NULL,
    drive_id    bigint,
    kind        text             NOT NULL,
    km_from     double precision,
    km_to       double precision,
    start_at    timestamp,
    end_at      timestamp,
    estimated   boolean          NOT NULL,
    created_at  timestamp        NOT NULL,
    confirmed_start_at timestamp,
    confirmed_end_at   timestamp,
    UNIQUE (car_id, kind, km_from, km_to)
)
"""

#: Adds the columns a table from before them lacks (PostgreSQL only).
ADD_CONFIRMED_COLUMNS = (
    "ALTER TABLE xpengmate_drive_repairs ADD COLUMN IF NOT EXISTS confirmed_start_at timestamp,"
    " ADD COLUMN IF NOT EXISTS confirmed_end_at timestamp"
)

#: The advisory lock this job and the export import take before they write drives.
DRIVES_LOCK_KEY = 74_706_013

_SUCCESSOR_QUERIES = (
    "SELECT start_date FROM drives"
    " WHERE car_id = %s AND start_date > %s ORDER BY start_date LIMIT 1",
    "SELECT start_date FROM charging_processes"
    " WHERE car_id = %s AND start_date > %s ORDER BY start_date LIMIT 1",
)

_DRIVE_COLUMNS = (
    "id",
    "start_date",
    "end_date",
    "start_km",
    "end_km",
    "start_position_id",
    "end_position_id",
)

OdometerFetcher = Callable[[datetime, datetime], list[tuple[datetime, str]]]
LocationFetcher = Callable[[datetime, datetime], list[tuple[datetime, float, float]]]


@dataclass
class Episode:
    """A run of rising odometer samples; ``km_from`` is the value before the first rise."""

    km_from: float
    km_to: float
    previous_change_at: datetime
    first_rise_at: datetime
    first_rise_km: float
    last_rise_at: datetime


@dataclass
class Gap:
    """Kilometres no drive covers, with the drive that ends at ``lower`` and the one starting at ``upper``."""

    lower: float
    upper: float
    before: dict[str, Any] | None
    after: dict[str, Any] | None


# -- pure helpers -------------------------------------------


def _utc(text: str) -> datetime:
    """Naive UTC, like TeslaMate's ``timestamp`` columns."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _km(state: Any) -> float | None:
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def odometer_records(payload: Any) -> list[tuple[datetime, str]]:
    """(time, state) pairs from a minimal-response history period payload."""
    rows = payload[0] if payload else []
    return [(_utc(row["last_changed"]), row["state"]) for row in rows]


def location_records(payload: Any) -> list[tuple[datetime, float, float]]:
    """(time, latitude, longitude) from a device tracker history payload."""
    records = []
    for row in payload[0] if payload else []:
        attributes = row.get("attributes") or {}
        latitude, longitude = _km(attributes.get("latitude")), _km(attributes.get("longitude"))
        if latitude is None or longitude is None:
            continue
        records.append((_utc(row.get("last_updated") or row["last_changed"]), latitude, longitude))
    return records


def motion_episodes(records: list[tuple[datetime, str]], window_start: datetime) -> list[Episode]:
    """Episodes from odometer history whose first numeric record is the state carried in."""
    episodes: list[Episode] = []
    value: float | None = None
    changed_at = window_start
    for moment, state in records:
        km = _km(state)
        if km is None:
            continue
        if value is None:
            value, changed_at = km, max(moment, window_start)
            continue
        if km <= value:
            continue
        hours = (moment - changed_at).total_seconds() / 3600
        # A whole-km odometer can cross the next km seconds after the last crossing.
        step = max(0.0, km - value - 1)
        if step > 0 and (hours <= 0 or step / hours > MAX_SPEED_KMH):
            _LOGGER.warning(
                "skipped odometer jump %s -> %s km at %s: faster than %s km/h",
                value, km, moment.isoformat(), MAX_SPEED_KMH,
            )
            continue
        if not episodes or (moment - changed_at).total_seconds() >= SPLIT_FLAT_SECONDS:
            episodes.append(Episode(value, km, changed_at, moment, km - value, moment))
        else:
            episodes[-1].km_to, episodes[-1].last_rise_at = km, moment
        value, changed_at = km, moment
    return episodes


def estimate_start(
    episode: Episode, previous_drive_end: datetime | None, previous_charge_end: datetime | None
) -> datetime:
    """First rise minus the time the first rise's km take at 90 km/h, at least 5 minutes."""
    lead = max(MIN_LEAD_SECONDS, episode.first_rise_km / LEAD_SPEED_KMH * 3600)
    start = max(episode.first_rise_at - timedelta(seconds=lead), episode.previous_change_at)
    return max([start, *(end for end in (previous_drive_end, previous_charge_end) if end)])


def find_gaps(episode: Episode, drives: list[dict[str, Any]]) -> tuple[float, list[Gap]]:
    """The episode's lower end and the km intervals of the episode no drive covers."""
    lower = episode.km_from
    ended = [d["end_km"] for d in drives if d["end_km"] <= episode.km_from + KM_TOLERANCE]
    # HA has whole km, drives decimals: the previous drive's end is the precise start.
    if ended and abs(max(ended) - episode.km_from) <= PRECISE_LOWER_KM:
        lower = max(ended)
    inside = sorted(
        (
            d
            for d in drives
            if d["start_km"] < episode.km_to - KM_TOLERANCE
            and d["end_km"] > episode.km_from + KM_TOLERANCE
        ),
        key=lambda d: d["start_km"],
    )
    gaps = []
    cursor, before = lower, None
    for drive in inside:
        if drive["start_km"] - cursor > KM_TOLERANCE:
            gaps.append(Gap(cursor, drive["start_km"], before, drive))
        if drive["end_km"] > cursor:
            cursor, before = drive["end_km"], drive
    if episode.km_to - cursor > KM_TOLERANCE:
        gaps.append(Gap(cursor, episode.km_to, before, None))
    return lower, gaps


def location_at(
    locations: list[tuple[datetime, float, float]], moment: datetime
) -> tuple[float, float] | None:
    """The tracker state in effect at ``moment``, else the first one after it."""
    earlier = [record for record in locations if record[0] <= moment]
    if earlier:
        record = max(earlier, key=lambda r: r[0])
    elif locations:
        record = min(locations, key=lambda r: r[0])
    else:
        return None
    return record[1], record[2]


def state_at(records: list[tuple[datetime, str]], moment: datetime) -> float | None:
    """The last numeric state recorded at or before ``moment``."""
    earlier = [(time, _km(state)) for time, state in records if time <= moment]
    numeric = [(time, value) for time, value in earlier if value is not None]
    return max(numeric, key=lambda r: r[0])[1] if numeric else None


# -- database ------------------------------------------------


def _first(cursor: Any, sql: str, params: tuple[Any, ...]) -> Any:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


def _record(cursor: Any, car_id: int, now: datetime, repair: dict[str, Any]) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO xpengmate_drive_repairs"
        " (car_id, drive_id, kind, km_from, km_to, start_at, end_at, estimated, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            car_id, repair["drive_id"], repair["kind"], repair["km_from"], repair["km_to"],
            repair["start_at"], repair["end_at"], repair["estimated"], now,
        ),
    )
    _LOGGER.info(
        "%s drive %s of car %s: %s -> %s km, %s -> %s%s",
        repair["kind"], repair["drive_id"], car_id, repair["km_from"], repair["km_to"],
        repair["start_at"].isoformat() if repair["start_at"] else None,
        repair["end_at"].isoformat() if repair["end_at"] else None,
        " (estimated)" if repair["estimated"] else "",
    )
    return {"car_id": car_id, **repair}


def close_orphan_drives(cursor: Any, car_id: int, now: datetime) -> list[dict[str, Any]]:
    """Close or delete the car's open drives that have a later drive or charging process."""
    cursor.execute(
        "SELECT id, start_date FROM drives WHERE car_id = %s AND end_date IS NULL"
        " ORDER BY start_date",
        (car_id,),
    )
    repairs = []
    for drive_id, start in cursor.fetchall():
        if all(_first(cursor, sql, (car_id, start)) is None for sql in _SUCCESSOR_QUERIES):
            continue
        cursor.execute(
            "SELECT id, date, odometer FROM positions WHERE drive_id = %s ORDER BY date",
            (drive_id,),
        )
        positions = cursor.fetchall()
        kms = [row[2] for row in positions if row[2] is not None]
        if len(positions) >= 2 and kms and kms[-1] - kms[0] >= 0.01:
            first, last = positions[0], positions[-1]
            cursor.execute(
                "UPDATE drives SET start_date = %s, end_date = %s, start_km = %s, end_km = %s,"
                " distance = %s, duration_min = %s, start_position_id = %s, end_position_id = %s"
                " WHERE id = %s AND end_date IS NULL",
                (
                    first[1], last[1], kms[0], kms[-1], kms[-1] - kms[0],
                    duration_min(first[1], last[1]), first[0], last[0], drive_id,
                ),
            )
            repair = {"kind": "closed_orphan", "km_from": kms[0], "km_to": kms[-1],
                      "start_at": first[1], "end_at": last[1]}
        else:
            cursor.execute("UPDATE positions SET drive_id = NULL WHERE drive_id = %s", (drive_id,))
            cursor.execute("DELETE FROM drives WHERE id = %s", (drive_id,))
            repair = {"kind": "deleted_orphan", "km_from": None, "km_to": None, "start_at": start,
                      "end_at": positions[-1][1] if positions else start}
        repairs.append(_record(cursor, car_id, now, {"drive_id": drive_id, "estimated": False, **repair}))
    return repairs


def _location(
    cursor: Any, car_id: int, locations: list[tuple[datetime, float, float]],
    moment: datetime, at_start: bool,
) -> tuple[float, float] | None:
    """HA location at that time, else the last position before a start or the first after an end."""
    found = location_at(locations, moment)
    if found is not None:
        return found
    if at_start:
        sql = ("SELECT latitude, longitude FROM positions WHERE car_id = %s AND date <= %s"
               " ORDER BY date DESC LIMIT 1")
    else:
        sql = ("SELECT latitude, longitude FROM positions WHERE car_id = %s AND date >= %s"
               " ORDER BY date LIMIT 1")
    cursor.execute(sql, (car_id, moment))
    row = cursor.fetchone()
    return (row[0], row[1]) if row else None


def _add_position(
    cursor: Any, car_id: int, drive_id: int, moment: datetime, km: float,
    location: tuple[float, float] | None, charge: tuple[float | None, int | None],
) -> int | None:
    if location is None:
        return None
    range_km, battery = charge
    return _first(
        cursor,
        "INSERT INTO positions (date, latitude, longitude, odometer, car_id, drive_id,"
        " battery_level, usable_battery_level, rated_battery_range_km, ideal_battery_range_km)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (moment, location[0], location[1], km, car_id, drive_id,
         battery, battery, range_km, range_km),
    )


def _already_repaired(cursor: Any, car_id: int, kind: str, km_from: float, km_to: float) -> bool:
    return _first(
        cursor,
        "SELECT id FROM xpengmate_drive_repairs"
        " WHERE car_id = %s AND kind = %s AND km_from = %s AND km_to = %s LIMIT 1",
        (car_id, kind, km_from, km_to),
    ) is not None


def _repair_gap(
    cursor: Any, car_id: int, episode: Episode, gap: Gap, start: datetime,
    locate: Callable[[datetime, bool], tuple[float, float] | None],
    measure: Callable[[datetime], tuple[float | None, int | None]],
    drives: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Write one gap repair and keep ``drives`` in step; None when it was done before."""
    kind = "extended_start" if gap.after else "extended_end" if gap.before else "inserted"
    if _already_repaired(cursor, car_id, kind, gap.lower, gap.upper):
        return None
    if gap.after is not None:
        drive = gap.after
        if gap.before:
            # Both edges are decimal drive km, so no whole-km lead; the car sat parked after the drive.
            lead = timedelta(hours=(gap.upper - gap.lower) / LEAD_SPEED_KMH)
            start = max(drive["start_date"] - lead, gap.before["end_date"])
        new_start = min(start, drive["start_date"])
        charge = measure(new_start)
        position_id = _add_position(
            cursor, car_id, drive["id"], new_start, gap.lower, locate(new_start, True), charge
        ) or drive["start_position_id"]
        repair = {"start_at": new_start, "end_at": drive["start_date"]}
        drive.update(start_date=new_start, start_km=gap.lower, start_position_id=position_id)
        cursor.execute(
            "UPDATE drives SET start_date = %s, start_km = %s, distance = %s, duration_min = %s,"
            " start_position_id = %s, start_address_id = NULL, start_geofence_id = NULL,"
            " start_rated_range_km = %s, start_ideal_range_km = %s WHERE id = %s",
            (new_start, gap.lower, drive["end_km"] - gap.lower,
             duration_min(new_start, drive["end_date"]), position_id, charge[0], charge[0],
             drive["id"]),
        )
    elif gap.before is not None:
        drive = gap.before
        new_end = max(episode.last_rise_at, drive["end_date"])
        charge = measure(new_end)
        position_id = _add_position(
            cursor, car_id, drive["id"], new_end, gap.upper, locate(new_end, False), charge
        ) or drive["end_position_id"]
        repair = {"start_at": drive["end_date"], "end_at": new_end}
        drive.update(end_date=new_end, end_km=gap.upper, end_position_id=position_id)
        cursor.execute(
            "UPDATE drives SET end_date = %s, end_km = %s, distance = %s, duration_min = %s,"
            " end_position_id = %s, end_address_id = NULL, end_geofence_id = NULL,"
            " end_rated_range_km = %s, end_ideal_range_km = %s WHERE id = %s",
            (new_end, gap.upper, gap.upper - drive["start_km"],
             duration_min(drive["start_date"], new_end), position_id, charge[0], charge[0],
             drive["id"]),
        )
    else:
        end = episode.last_rise_at
        start_charge, end_charge = measure(start), measure(end)
        drive_id = _first(
            cursor,
            "INSERT INTO drives (car_id, start_date, end_date, start_km, end_km, distance,"
            " duration_min, start_rated_range_km, start_ideal_range_km, end_rated_range_km,"
            " end_ideal_range_km) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (car_id, start, end, gap.lower, gap.upper, gap.upper - gap.lower,
             duration_min(start, end), start_charge[0], start_charge[0], end_charge[0],
             end_charge[0]),
        )
        start_id = _add_position(
            cursor, car_id, drive_id, start, gap.lower, locate(start, True), start_charge
        )
        end_id = _add_position(cursor, car_id, drive_id, end, gap.upper, locate(end, False), end_charge)
        cursor.execute(
            "UPDATE drives SET start_position_id = %s, end_position_id = %s WHERE id = %s",
            (start_id, end_id, drive_id),
        )
        drive = {"id": drive_id, "start_date": start, "end_date": end, "start_km": gap.lower,
                 "end_km": gap.upper, "start_position_id": start_id, "end_position_id": end_id}
        drives.append(drive)
        repair = {"start_at": start, "end_at": end}
    return {"kind": kind, "drive_id": drive["id"], "km_from": gap.lower, "km_to": gap.upper,
            "estimated": True, **repair}


def repair_gaps(
    cursor: Any, car_id: int, fetch_odometer: OdometerFetcher,
    fetch_location: LocationFetcher, now: datetime,
    fetch_range: OdometerFetcher | None = None, fetch_battery: OdometerFetcher | None = None,
) -> list[dict[str, Any]]:
    """Close orphan drives, then repair the car's odometer gaps; return the repairs.

    Without a range or battery fetcher, the moved drive ends and new positions get NULL there.
    """
    cursor.execute(SCHEMA)
    repairs = close_orphan_drives(cursor, car_id, now)
    if _first(cursor, "SELECT id FROM drives WHERE car_id = %s AND end_date IS NULL LIMIT 1",
              (car_id,)) is not None:
        _LOGGER.info("drive gap repair skipped car %s: a drive is in progress", car_id)
        return repairs
    watermark = _first(
        cursor,
        "SELECT end_at FROM xpengmate_drive_repairs WHERE car_id = %s"
        f" AND kind IN ({', '.join(['%s'] * len(GAP_KINDS))}) ORDER BY end_at DESC LIMIT 1",
        (car_id, *GAP_KINDS),
    )
    window_end = now - SETTLE
    window_start = max(watermark or now - LOOKBACK, now - LOOKBACK)
    # An episode is complete only after a flat period that splits episodes.
    episodes = [
        episode
        for episode in motion_episodes(fetch_odometer(window_start, window_end), window_start)
        if (window_end - episode.last_rise_at).total_seconds() >= SPLIT_FLAT_SECONDS
    ]
    cursor.execute(
        f"SELECT {', '.join(_DRIVE_COLUMNS)} FROM drives WHERE car_id = %s"
        " AND end_date IS NOT NULL ORDER BY start_date",
        (car_id,),
    )
    rows = [dict(zip(_DRIVE_COLUMNS, row)) for row in cursor.fetchall()]
    drives = [d for d in rows if d["start_km"] is not None and d["end_km"] is not None]
    unmeasured = [d for d in rows if d["start_km"] is None or d["end_km"] is None]
    locations: list[tuple[datetime, float, float]] | None = None

    def locate(moment: datetime, at_start: bool) -> tuple[float, float] | None:
        nonlocal locations
        if locations is None:
            locations = fetch_location(window_start, window_end)
        return _location(cursor, car_id, locations, moment, at_start)

    histories: dict[str, list[tuple[datetime, str]]] = {}

    def history(name: str, fetch: OdometerFetcher | None) -> list[tuple[datetime, str]]:
        if fetch is not None and name not in histories:
            histories[name] = fetch(window_start, window_end)
        return histories.get(name, [])

    def measure(moment: datetime) -> tuple[float | None, int | None]:
        """HA range (km) and battery (%) in effect at ``moment``."""
        battery = state_at(history("battery", fetch_battery), moment)
        return (state_at(history("range", fetch_range), moment),
                None if battery is None else round(battery))

    covered_km = repaired_km = 0.0
    for episode in episodes:
        if any(d["start_date"] <= episode.last_rise_at
               and d["end_date"] >= episode.previous_change_at for d in unmeasured):
            _LOGGER.warning(
                "skipped odometer episode %s -> %s km: a drive in it has no odometer values",
                episode.km_from, episode.km_to,
            )
            continue
        lower, gaps = find_gaps(episode, drives)
        covered_km += max(0.0, episode.km_to - lower - sum(g.upper - g.lower for g in gaps))
        ended = [d["end_date"] for d in drives if d["end_date"] <= episode.first_rise_at]
        charge_end = _first(
            cursor,
            "SELECT end_date FROM charging_processes WHERE car_id = %s AND end_date <= %s"
            " ORDER BY end_date DESC LIMIT 1",
            (car_id, episode.first_rise_at),
        )
        start = estimate_start(episode, max(ended, default=None), charge_end)
        for gap in gaps:
            repair = _repair_gap(cursor, car_id, episode, gap, start, locate, measure, drives)
            if repair is not None:
                repaired_km += gap.upper - gap.lower
                repairs.append(_record(cursor, car_id, now, repair))
    _LOGGER.info(
        "drive gap repair for car %s: %d episodes, %.1f km in recorded drives, %.1f km repaired",
        car_id, len(episodes), covered_km, repaired_km,
    )
    return repairs


# -- Home Assistant and the entry points ----------------------


def settings_from_env(environ: Any) -> dict[str, str] | None:
    """HA access and entities for the repair, or None (logged once) when one is unset."""
    names = {
        "ha_url": "XPENG_HA_URL",
        "ha_token": "XPENG_HA_TOKEN",
        "odometer_entity": "XPENG_ODOMETER_ENTITY",
        "location_entity": "XPENG_LOCATION_ENTITY",
    }
    settings = {key: environ.get(name, "").strip() for key, name in names.items()}
    missing = [names[key] for key, value in settings.items() if not value]
    if missing:
        _LOGGER.info("drive gap repair is off: set %s", ", ".join(missing))
        return None
    settings["range_entity"] = environ.get("XPENG_RANGE_ENTITY", "").strip()
    settings["battery_entity"] = environ.get("XPENG_BATTERY_ENTITY", "").strip()
    return settings


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=timezone.utc).isoformat()


def ha_fetchers(
    settings: dict[str, str],
) -> tuple[OdometerFetcher, LocationFetcher, OdometerFetcher | None, OdometerFetcher | None]:
    """Odometer, location, range and battery fetchers; an unset optional entity gives None."""

    def history(entity: str, start: datetime, end: datetime, flags: str) -> Any:
        url = (
            settings["ha_url"].rstrip("/") + "/api/history/period/" + quote(_iso(start))
            + "?" + urlencode({"end_time": _iso(end), "filter_entity_id": entity}) + flags
        )
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {settings['ha_token']}"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def states(entity: str) -> OdometerFetcher:
        def fetch(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
            return odometer_records(history(entity, start, end, "&minimal_response&no_attributes"))

        return fetch

    def fetch_location(start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
        # A device tracker's moves are attribute changes, which HA leaves out by default.
        payload = history(settings["location_entity"], start, end, "&significant_changes_only=0")
        return location_records(payload)

    optional = [
        states(settings[key]) if settings.get(key) else None
        for key in ("range_entity", "battery_entity")
    ]
    return states(settings["odometer_entity"]), fetch_location, *optional


def find_car(cursor: Any, vin: str) -> int | None:
    """The car with this VIN, or the only car when no VIN is set."""
    if vin:
        return _first(cursor, "SELECT id FROM cars WHERE vin = %s", (vin,))
    cursor.execute("SELECT id FROM cars ORDER BY id LIMIT 2")
    rows = cursor.fetchall()
    return rows[0][0] if len(rows) == 1 else None


def run(dsn: str, settings: dict[str, str], vin: str, dry_run: bool = False) -> list[dict[str, Any]]:
    """One repair pass in one transaction; a dry run rolls it back."""
    fetch_odometer, fetch_location, fetch_range, fetch_battery = ha_fetchers(settings)
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (DRIVES_LOCK_KEY,))
            # After the lock, which can wait for a whole export import.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            car_id = find_car(cursor, vin)
            if car_id is None:
                _LOGGER.info("drive gap repair found no car: set XPENG_VIN")
                return []
            repairs = repair_gaps(
                cursor, car_id, fetch_odometer, fetch_location, now, fetch_range, fetch_battery
            )
        if dry_run:
            connection.rollback()
    return repairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the repairs, write nothing")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    dsn = os.environ.get("XPENGMATE_DATABASE_URL", "")
    if not dsn:
        print("XPENGMATE_DATABASE_URL is required", file=sys.stderr)
        return 2
    settings = settings_from_env(os.environ)
    if settings is None:
        return 0
    repairs = run(dsn, settings, os.environ.get("XPENG_VIN", ""), dry_run=arguments.dry_run)
    for repair in repairs:
        print(json.dumps({"dry_run": arguments.dry_run, **repair}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
