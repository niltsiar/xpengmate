"""Replace the drive edges that the gap repair guessed with the trip times of an XPENG export.

``repair_drive_gaps`` dates the drives it inserts or extends from Home Assistant
odometer samples that arrive minutes apart, so those edges are estimates. The
export has 1 Hz odometer and speed, and its trips start and end at the measured
moments. A guessed edge takes the time of the one export trip that matches it:

* inserted: both trip ends within ``PRECISE_LOWER_KM`` of the drive's km, and
  the two km ranges overlap by at least half;
* extended_start (extended_end): the trip's start (end) km within
  ``PRECISE_LOWER_KM`` of the drive's, and the trip overlaps the drive in time.

An edge moves only while the drive still has its guessed time there. An inserted
drive with one such edge left follows the rule of that edge.

The new edge still covers the drive's live positions. A guess keeps its times
when no trip or several trips match, when one trip matches several drives, or
when the new times would overlap another drive or a charging process. The
drive's dates, duration and edge ranges change, and so does the estimated
position the repair added at that edge, which takes the export's battery
level, odometer and range at the new moment. The positions of the car that
have no drive and lie inside the new window are linked to the drive. The km
stay. The repair row records the confirmed times, so a second run changes
nothing, and the repair job's watermark does not move.

The queries use only plain columns, so they run on PostgreSQL and on SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from close_orphan_charges import duration_min
from repair_drive_gaps import GAP_KINDS, PRECISE_LOWER_KM

MIN_KM_OVERLAP = 0.5

Sample = tuple[int | None, float | None, float | None]
#: The export's (soc, odometer km, range km) at each moment it has a sample for.
SamplesFor = Callable[[list[datetime]], dict[datetime, Sample]]

_SIDES = {"inserted": ("start", "end"), "extended_start": ("start",), "extended_end": ("end",)}


@dataclass(frozen=True)
class Trip:
    start: datetime
    end: datetime
    km_from: float
    km_to: float


@dataclass
class Guess:
    """A repair row with the current state of its drive."""

    repair_id: int
    kind: str
    drive_id: int
    guessed_start: datetime
    guessed_end: datetime
    start: datetime
    end: datetime
    start_km: float
    end_km: float
    live: tuple[datetime, datetime] | None = None

    def edges(self) -> dict[str, datetime]:
        """The guessed time of each edge this repair moved that the drive still has."""
        guessed = {"start": self.guessed_start, "end": self.guessed_end}
        current = {"start": self.start, "end": self.end}
        return {side: guessed[side] for side in _SIDES[self.kind] if current[side] == guessed[side]}

    def fits(self, trip: Trip) -> bool:
        sides = self.edges()
        near = {
            "start": abs(trip.km_from - self.start_km) <= PRECISE_LOWER_KM,
            "end": abs(trip.km_to - self.end_km) <= PRECISE_LOWER_KM,
        }
        if len(sides) == 2:
            overlap = min(trip.km_to, self.end_km) - max(trip.km_from, self.start_km)
            longest = max(trip.km_to - trip.km_from, self.end_km - self.start_km)
            return near["start"] and near["end"] and overlap >= MIN_KM_OVERLAP * longest
        in_time = trip.start <= self.end and trip.end >= self.start
        return in_time and all(near[side] for side in sides)


@dataclass
class EdgeChange:
    repair_id: int
    drive_id: int
    side: str
    old: datetime
    new: datetime


def _overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def match_guessed_edges(
    guesses: list[Guess],
    trips: list[Trip],
    drives: dict[int, tuple[datetime, datetime]],
    charges: list[tuple[datetime, datetime]],
) -> tuple[list[EdgeChange], list[dict[str, Any]]]:
    """The edge changes that pass every rule, and the guesses that keep their times with the reason."""
    skipped: list[dict[str, Any]] = []

    def skip(guess: Guess, reason: str) -> None:
        skipped.append({"drive_id": guess.drive_id, "kind": guess.kind, "reason": reason})

    matched: list[tuple[Guess, Trip]] = []
    for guess in guesses:
        if not guess.edges():
            skip(guess, "drive edge changed after the repair")
            continue
        candidates = [trip for trip in trips if guess.fits(trip)]
        if len(candidates) != 1:
            skip(guess, f"{len(candidates)} matching trips")
            continue
        matched.append((guess, candidates[0]))

    claimants: dict[Trip, set[int]] = {}
    for guess, trip in matched:
        claimants.setdefault(trip, set()).add(guess.drive_id)
    proposed: list[tuple[Guess, list[EdgeChange]]] = []
    for guess, trip in matched:
        if len(claimants[trip]) > 1:
            skip(guess, "trip matches another drive")
            continue
        changes = []
        for side, old in guess.edges().items():
            if side == "start":
                new = min(trip.start, guess.live[0]) if guess.live else trip.start
            else:
                new = max(trip.end, guess.live[1]) if guess.live else trip.end
            changes.append(EdgeChange(guess.repair_id, guess.drive_id, side, old, new))
        proposed.append((guess, changes))

    # Dropping a drive restores its old window, which can clash with a drive kept so far.
    while True:
        windows = dict(drives)
        for _guess, changes in proposed:
            for change in changes:
                start, end = windows[change.drive_id]
                windows[change.drive_id] = (change.new, end) if change.side == "start" else (start, change.new)
        clashing = {
            drive_id
            for drive_id in {guess.drive_id for guess, _changes in proposed}
            if any(_overlaps(windows[drive_id], window) for other, window in windows.items() if other != drive_id)
            or any(_overlaps(windows[drive_id], charge) for charge in charges)
        }
        if not clashing:
            break
        for guess, _changes in proposed:
            if guess.drive_id in clashing:
                skip(guess, "new times overlap another drive or a charge")
        proposed = [item for item in proposed if item[0].drive_id not in clashing]
    return [change for _guess, changes in proposed for change in changes], skipped


# -- database -----------------------------------------------


def load_guesses(cursor: Any, car_id: int) -> list[Guess]:
    """Unconfirmed gap repairs of the car, with their drives' live position span."""
    cursor.execute(
        "SELECT r.id, r.kind, r.drive_id, r.start_at, r.end_at, d.start_date, d.end_date,"
        " d.start_km, d.end_km FROM xpengmate_drive_repairs r JOIN drives d ON d.id = r.drive_id"
        f" WHERE r.car_id = %s AND r.kind IN ({', '.join(['%s'] * len(GAP_KINDS))})"
        " AND r.confirmed_start_at IS NULL AND r.confirmed_end_at IS NULL"
        " AND d.end_date IS NOT NULL AND d.start_km IS NOT NULL AND d.end_km IS NOT NULL"
        " ORDER BY d.start_date",
        (car_id, *GAP_KINDS),
    )
    guesses = [Guess(*row) for row in cursor.fetchall()]
    guessed: dict[int, set[datetime]] = {}
    for guess in guesses:
        guessed.setdefault(guess.drive_id, set()).update(guess.edges().values())
    for guess in guesses:
        cursor.execute("SELECT date, speed FROM positions WHERE drive_id = %s ORDER BY date", (guess.drive_id,))
        live = [
            moment for moment, speed in cursor.fetchall()
            if not (speed is None and moment in guessed[guess.drive_id])
        ]
        guess.live = (live[0], live[-1]) if live else None
    return guesses


def _windows(cursor: Any, table: str, car_id: int) -> list[tuple[int, datetime, datetime]]:
    """Every row's (id, start, end); an open row runs to the end of time."""
    cursor.execute(f"SELECT id, start_date, end_date FROM {table} WHERE car_id = %s", (car_id,))
    return [(row_id, start, end or datetime.max) for row_id, start, end in cursor.fetchall()]


def apply_changes(
    cursor: Any, car_id: int, changes: list[EdgeChange], samples: dict[datetime, Sample]
) -> tuple[int, int]:
    """Write the changes; returns how many estimated positions moved and how many were linked."""
    moved = 0
    linked = 0
    for change in changes:
        side = change.side
        soc, odometer, range_km = samples.get(change.new, (None, None, None))
        cursor.execute(
            "UPDATE positions SET date = %s, battery_level = COALESCE(%s, battery_level),"
            " usable_battery_level = COALESCE(%s, usable_battery_level),"
            " odometer = COALESCE(%s, odometer),"
            " rated_battery_range_km = COALESCE(%s, rated_battery_range_km),"
            " ideal_battery_range_km = COALESCE(%s, ideal_battery_range_km),"
            " est_battery_range_km = COALESCE(%s, est_battery_range_km)"
            " WHERE drive_id = %s AND date = %s AND speed IS NULL",
            (change.new, soc, soc, odometer, range_km, range_km, range_km, change.drive_id, change.old),
        )
        moved += cursor.rowcount
        cursor.execute(
            f"UPDATE drives SET {side}_date = %s,"
            f" {side}_rated_range_km = COALESCE(%s, {side}_rated_range_km),"
            f" {side}_ideal_range_km = COALESCE(%s, {side}_ideal_range_km) WHERE id = %s",
            (change.new, range_km, range_km, change.drive_id),
        )
        cursor.execute(
            f"UPDATE xpengmate_drive_repairs SET confirmed_{side}_at = %s WHERE id = %s",
            (change.new, change.repair_id),
        )
    for drive_id in {change.drive_id for change in changes}:
        cursor.execute("SELECT start_date, end_date FROM drives WHERE id = %s", (drive_id,))
        start, end = cursor.fetchone()
        cursor.execute("UPDATE drives SET duration_min = %s WHERE id = %s", (duration_min(start, end), drive_id))
        cursor.execute(
            "UPDATE positions SET drive_id = %s"
            " WHERE car_id = %s AND drive_id IS NULL AND date >= %s AND date <= %s",
            (drive_id, car_id, start, end),
        )
        linked += cursor.rowcount
    return moved, linked


def reconcile(cursor: Any, car_id: int, trips: list[Trip], samples_for: SamplesFor) -> dict[str, Any]:
    """Match and write in the caller's transaction; returns the report."""
    guesses = load_guesses(cursor, car_id)
    drives = {row_id: (start, end) for row_id, start, end in _windows(cursor, "drives", car_id)}
    charges = [(start, end) for _id, start, end in _windows(cursor, "charging_processes", car_id)]
    changes, skipped = match_guessed_edges(guesses, trips, drives, charges)
    moved, linked = apply_changes(
        cursor, car_id, changes, samples_for([change.new for change in changes]) if changes else {}
    )
    return {
        "changed": [
            {"drive_id": c.drive_id, "side": c.side, "from": c.old.isoformat(), "to": c.new.isoformat()}
            for c in changes
        ],
        "skipped": skipped,
        "positions_moved": moved,
        "positions_linked": linked,
    }
