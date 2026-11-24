"""Fill battery state onto imported positions from an XPENG data export.

EVConduit's GPS logger records where the car was, not how full the battery was.
XPENG's own GDPR export is the mirror image: it has 1 Hz SoC, odometer and range
for every second of a 30-day window, but no coordinates at all. Joining the two
by timestamp gives TeslaMate positions that carry both - which is what the
Battery Health, Charge Level and efficiency dashboards read.

The join is exact-second, with a 5 s tolerance for clock skew, and nothing is
interpolated across a gap: a position with no export sample within tolerance is
left with its battery fields NULL, exactly as it was imported.

The same samples carry pack voltage and current. A drive position takes their
product as its power, and each drive the export covers gets the energy the pack
delivered and the energy regeneration returned, summed second by second.
"""

from __future__ import annotations

import csv
import heapq
import io
import logging
import math
import zipfile
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from export_parser import CURRENT_SNA, MAX_STEP_SECONDS, VOLT_SNA

_LOGGER = logging.getLogger("xpengmate.export")

#: Sentinel values documented in the export field reference. They arrive on the
#: whole row at once, so every consumer must filter them, not clamp them.
SOC_SENTINEL = 255
RANGE_SENTINEL = 1600.0
ODOMETER_MAX = 2_000_000.0

#: How far a position may be from the export sample it borrows.
TOLERANCE_SECONDS = 5

#: The share of a drive's duration the export must have samples for before its energy is stored.
COVERAGE_FLOOR = 0.9

DRIVE_ENERGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS xpengmate_drive_energy (
    drive_id             integer          PRIMARY KEY REFERENCES drives (id) ON DELETE CASCADE,
    energy_used_kwh      double precision NOT NULL,
    energy_recovered_kwh double precision NOT NULL
)
"""


def _float(value: str) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _members(archive: zipfile.ZipFile, suffix: str) -> list[str]:
    """The base/_part1/_part2 members for one dataset, in time order.

    The export splits each dataset into a base file and numbered parts that are
    contiguous and ascending, so the part number is the sort key. (Ordering by
    the file's *last* characters does not work: the base file ends in `_di`,
    which sorts after `part1` and `part2` as text.)
    """
    names = [n for n in archive.namelist() if suffix in n and n.endswith(".csv")]

    def part(name: str) -> int:
        stem = Path(name).stem
        if "_part" not in stem:
            return 0
        try:
            return int(stem.rsplit("_part", 1)[1])
        except (ValueError, IndexError):
            return 99

    return sorted(names, key=part)


def _rows(archive: zipfile.ZipFile, name: str, fields: dict[str, str]) -> Iterator[tuple[int, dict]]:
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            try:
                timer = int(float(row.get("timer") or 0))
            except (TypeError, ValueError):
                continue
            values = {}
            for key, source in fields.items():
                values[key] = _float(row.get(source))
            yield timer, values


def _stream(archive: zipfile.ZipFile, members: list[str], fields: dict[str, str]) -> Iterator[tuple[int, dict]]:
    """The dataset's rows in timer order.

    A dataset can hold one day's block out of place (seen in ``driving_operation_di``),
    and ``_merge`` drops every row it passes out of order. The values wait in
    compact arrays, 8 bytes per value, until all rows are read.
    """
    keys = list(fields)
    timers = array("q")
    columns = [array("d") for _key in keys]
    in_order = True
    for name in members:
        for timer, values in _rows(archive, name, fields):
            if timers and timer < timers[-1]:
                in_order = False
            timers.append(timer)
            for column, key in zip(columns, keys):
                value = values[key]
                column.append(math.nan if value is None else value)
    indexes = range(len(timers)) if in_order else sorted(range(len(timers)), key=timers.__getitem__)
    for index in indexes:
        yield timers[index], {
            key: None if math.isnan(column[index]) else column[index] for column, key in zip(columns, keys)
        }


def _merge(
    left: Iterator[tuple[int, dict]], right: Iterator[tuple[int, dict]]
) -> Iterator[tuple[int, dict]]:
    """Inner-join two timer-sorted streams on exactly equal timestamps."""
    a = next(left, None)
    b = next(right, None)
    while a is not None and b is not None:
        if a[0] < b[0]:
            a = next(left, None)
        elif b[0] < a[0]:
            b = next(right, None)
        else:
            yield a[0], {**a[1], **b[1]}
            a = next(left, None)
            b = next(right, None)


def iter_samples(zip_path: str) -> Iterator[tuple[int, dict]]:
    """Merged 1 Hz samples: (unix seconds, {soc, odometer_km, range_km, volt, amp})."""
    with zipfile.ZipFile(zip_path) as archive:
        operation = _stream(
            archive,
            _members(archive, "driving_operation_di"),
            {"odometer_km": "cdcu_totalodometer"},
        )
        energy = _stream(
            archive,
            _members(archive, "driving_power_energy_di"),
            {
                "soc": "ldcu_bms_soc_disp",
                "range_km": "ldcu_dstbatdisp_dynamic",
                "volt": "bms_battvolt",
                "amp": "bms_battcurr",
            },
        )
        yield from _merge(operation, energy)


def clean_sample(sample: dict) -> tuple[int | None, float | None, float | None]:
    soc = sample.get("soc")
    if soc is not None and (soc < 0 or soc >= SOC_SENTINEL):
        soc = None
    odometer = sample.get("odometer_km")
    if odometer is not None and (odometer < 0 or odometer > ODOMETER_MAX):
        odometer = None
    range_km = sample.get("range_km")
    if range_km is not None and (range_km <= 0 or range_km > RANGE_SENTINEL):
        range_km = None
    return (
        int(round(soc)) if soc is not None else None,
        round(odometer, 1) if odometer is not None else None,
        round(range_km, 2) if range_km is not None else None,
    )


def power_kw(sample: dict) -> float | None:
    """Pack voltage x current: positive while the battery discharges, negative while it charges."""
    volt, amp = sample.get("volt"), sample.get("amp")
    if volt is None or amp is None or volt <= 0 or volt >= VOLT_SNA or abs(amp) >= CURRENT_SNA:
        return None
    return volt * amp / 1000.0


def nearest_samples(
    positions: Iterable[tuple[Any, int]], samples: Iterator[tuple[int, dict]]
) -> Iterator[tuple[Any, tuple[int, dict]]]:
    """Pair each (id, unix seconds) position, in time order, with the closest sample within tolerance."""
    before: tuple[int, dict] | None = None
    after = next(samples, None)
    for position_id, epoch_seconds in positions:
        while after is not None and after[0] <= epoch_seconds:
            before, after = after, next(samples, None)
        close = [s for s in (before, after) if s is not None and abs(s[0] - epoch_seconds) <= TOLERANCE_SECONDS]
        if close:
            yield position_id, min(close, key=lambda sample: abs(sample[0] - epoch_seconds))


def samples_at(
    zip_paths: list[str], moments: list[datetime]
) -> dict[datetime, tuple[int | None, float | None, float | None]]:
    """The cleaned sample nearest to each naive-UTC moment, from the first zip that has one."""
    found: dict[datetime, tuple[int | None, float | None, float | None]] = {}
    for zip_path in zip_paths:
        wanted = sorted(moment for moment in set(moments) if moment not in found)
        if not wanted:
            break
        positions = [(moment, int(moment.replace(tzinfo=timezone.utc).timestamp())) for moment in wanted]
        for moment, chosen in nearest_samples(positions, iter_samples(zip_path)):
            found[moment] = clean_sample(chosen[1])
    return found


def position_updates(
    positions: Iterable[tuple[int, int, bool]], samples: Iterator[tuple[int, dict]]
) -> list[tuple[int, int | None, float | None, float | None, int | None]]:
    """(id, soc, odometer, range, power) for each (id, unix seconds, in a drive) position with a sample.

    Only drive positions take the pack power; a charge position keeps the charger power it has.
    """
    in_drive = {position_id for position_id, _epoch, driving in positions if driving}
    updates = []
    pairs = nearest_samples(((position_id, epoch) for position_id, epoch, _driving in positions), samples)
    for position_id, chosen in pairs:
        soc, odometer, range_km = clean_sample(chosen[1])
        power = power_kw(chosen[1]) if position_id in in_drive else None
        if soc is None and odometer is None and range_km is None and power is None:
            continue
        updates.append((position_id, soc, odometer, range_km, None if power is None else round(power)))
    return updates


@dataclass
class DriveEnergy:
    used_kwh: float = 0.0
    recovered_kwh: float = 0.0
    power_max: float | None = None
    power_min: float | None = None
    seconds: float = 0.0


def drive_energy(
    windows: list[tuple[int, float, float]], samples: Iterator[tuple[int, dict]]
) -> dict[int, DriveEnergy]:
    """Integrate pack power over each (drive id, start, end) window in unix seconds.

    The windows must not overlap. A step longer than ``MAX_STEP_SECONDS`` is a gap in
    the export and adds no energy; a step that starts in another drive adds none either.
    ``seconds`` is the time of the steps that were integrated.
    """
    ordered = sorted(windows, key=lambda window: window[1])
    found: dict[int, DriveEnergy] = {}
    index = 0
    previous: tuple[int, int] | None = None
    for timer, sample in samples:
        while index < len(ordered) and ordered[index][2] < timer:
            index += 1
        if index == len(ordered):
            break
        drive_id, start, _end = ordered[index]
        power = power_kw(sample)
        if timer < start or power is None:
            continue
        energy = found.setdefault(drive_id, DriveEnergy())
        energy.power_max = power if energy.power_max is None else max(energy.power_max, power)
        energy.power_min = power if energy.power_min is None else min(energy.power_min, power)
        if previous is not None and previous[0] == drive_id and 0 < timer - previous[1] <= MAX_STEP_SECONDS:
            energy.seconds += timer - previous[1]
            kwh = power * (timer - previous[1]) / 3600.0
            if kwh > 0:
                energy.used_kwh += kwh
            else:
                energy.recovered_kwh -= kwh
        previous = (drive_id, timer)
    return found


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def store_drive_energy(store: Any, zip_path: str, timer_start: int, timer_end: int) -> int:
    """Energy and power extremes of each drive the export covers; returns how many drives.

    A drive the export covers for less than ``COVERAGE_FLOOR`` of its duration gets nothing:
    a partial sum and partial power extremes would pass for the whole drive.
    """
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT id, start_date, end_date FROM drives"
            " WHERE car_id = %s AND start_date >= %s AND end_date <= %s",
            (store.car_id, _utc(timer_start), _utc(timer_end)),
        )
        windows = [
            (int(drive_id), start.replace(tzinfo=timezone.utc).timestamp(), end.replace(tzinfo=timezone.utc).timestamp())
            for drive_id, start, end in cur.fetchall()
        ]
    found = drive_energy(windows, iter_samples(zip_path))
    energies = {
        drive_id: found[drive_id]
        for drive_id, start, end in windows
        if drive_id in found and found[drive_id].seconds >= COVERAGE_FLOOR * (end - start)
    }
    skipped = sorted({drive_id for drive_id, _start, _end in windows} - set(energies))
    if skipped:
        _LOGGER.warning(
            "drive energy from %s: no energy for drives %s, the export covers too little of them",
            Path(zip_path).name,
            skipped,
        )
    with store.conn.cursor() as cur:
        for drive_id, energy in energies.items():
            cur.execute(
                "INSERT INTO xpengmate_drive_energy (drive_id, energy_used_kwh, energy_recovered_kwh)"
                " VALUES (%s, %s, %s) ON CONFLICT (drive_id) DO UPDATE SET"
                " energy_used_kwh = EXCLUDED.energy_used_kwh,"
                " energy_recovered_kwh = EXCLUDED.energy_recovered_kwh",
                (drive_id, round(energy.used_kwh, 3), round(energy.recovered_kwh, 3)),
            )
            cur.execute(
                "UPDATE drives SET power_max = %s, power_min = %s WHERE id = %s",
                (round(energy.power_max), round(energy.power_min), drive_id),
            )
    store.conn.commit()
    _LOGGER.info("drive energy from %s: %d drives", Path(zip_path).name, len(energies))
    return len(energies)


def resolve_zip(path: str) -> str:
    """Accept a zip file or a directory holding export zips (newest wins).

    The export filenames change with every download, so pointing an env var at
    the directory is what keeps a scheduled sync from needing an edit each time.
    """
    candidate = Path(path)
    if candidate.is_file():
        return str(candidate)
    if candidate.is_dir():
        zips = sorted(
            candidate.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        if zips:
            return str(zips[0])
    raise FileNotFoundError(f"no export zip at {path}")


def enrich(store: Any, zip_path: str) -> int:
    """Join the export onto this car's positions, then fill missing drive ranges."""
    zip_path = resolve_zip(zip_path)
    with store.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, extract(epoch FROM date)::bigint, drive_id IS NOT NULL
            FROM positions
            WHERE car_id = %s
            ORDER BY date
            """,
            (store.car_id,),
        )
        positions = [(int(row[0]), int(row[1]), bool(row[2])) for row in cur.fetchall()]
    if not positions:
        _LOGGER.warning("no positions to enrich")
        return 0

    updates = position_updates(positions, iter_samples(zip_path))
    matched = len(updates)

    _LOGGER.info(
        "export covers %d of %d positions (%.1f%%)",
        matched,
        len(positions),
        100.0 * matched / len(positions),
    )
    with store.conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE positions
            SET battery_level = COALESCE(%s, battery_level),
                usable_battery_level = COALESCE(%s, usable_battery_level),
                odometer = COALESCE(%s, odometer),
                est_battery_range_km = COALESCE(%s, est_battery_range_km),
                ideal_battery_range_km = COALESCE(%s, ideal_battery_range_km),
                power = COALESCE(%s, power)
            WHERE id = %s
            """,
            [
                (soc, soc, odometer, range_km, range_km, power, position_id)
                for position_id, soc, odometer, range_km, power in updates
            ],
        )
        # Drive-level range figures, from the enriched positions. TeslaMate's
        # efficiency dashboards use (start_range - end_range). Only empty fields
        # are filled: a live drive's first position can come minutes after the
        # car started moving, so its odometer and range are not the drive start.
        cur.execute(
            """
            UPDATE drives d SET
                start_ideal_range_km = COALESCE(d.start_ideal_range_km, first.ideal_battery_range_km),
                end_ideal_range_km   = COALESCE(d.end_ideal_range_km, last.ideal_battery_range_km),
                start_rated_range_km = COALESCE(d.start_rated_range_km, first.ideal_battery_range_km),
                end_rated_range_km   = COALESCE(d.end_rated_range_km, last.ideal_battery_range_km),
                start_km = COALESCE(d.start_km, first.odometer),
                end_km   = COALESCE(d.end_km, last.odometer)
            FROM (
                SELECT DISTINCT ON (drive_id)
                    drive_id, ideal_battery_range_km, odometer
                FROM positions
                WHERE car_id = %s AND drive_id IS NOT NULL
                ORDER BY drive_id, date ASC
            ) first,
            (
                SELECT DISTINCT ON (drive_id)
                    drive_id, ideal_battery_range_km, odometer
                FROM positions
                WHERE car_id = %s AND drive_id IS NOT NULL
                ORDER BY drive_id, date DESC
            ) last
            WHERE d.id = first.drive_id
              AND d.id = last.drive_id
              AND d.car_id = %s
            """,
            (store.car_id, store.car_id, store.car_id),
        )
        drives = cur.rowcount
    store.conn.commit()
    _LOGGER.info("enriched drives: %d", drives)
    return matched
