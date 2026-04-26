from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class StreakSummary:
    current_streak: int
    longest_streak: int
    total_completed_days: int
    total_missed_days: int
    total_resolved_days: int
    completion_rate: float
    last_completed_date: str | None


def compute_streak_summary(
    joined_on: date,
    scheduled_weekdays: set[int],
    checkins: dict[str, str],
    today: date,
) -> StreakSummary:
    if not scheduled_weekdays:
        return StreakSummary(
            current_streak=0,
            longest_streak=0,
            total_completed_days=0,
            total_missed_days=0,
            total_resolved_days=0,
            completion_rate=0.0,
            last_completed_date=None,
        )

    resolved_statuses: list[tuple[str, str]] = []
    current_day = joined_on

    while current_day <= today:
        key = current_day.isoformat()
        status = checkins.get(key)

        if current_day.weekday() not in scheduled_weekdays:
            # Non-scheduled day (e.g. Sunday) — always counts as rest for streak
            # continuity, whether or not a check-in was recorded (legacy users).
            resolved_statuses.append((key, "rest"))
            current_day += timedelta(days=1)
            continue

        if current_day == today and status is None:
            current_day += timedelta(days=1)
            continue

        if current_day < today and status is None:
            status = "missed"

        if status is not None:
            resolved_statuses.append((key, status))

        current_day += timedelta(days=1)

    total_completed = sum(1 for _, status in resolved_statuses if status == "completed")
    total_missed = sum(1 for _, status in resolved_statuses if status not in ("completed", "rest"))

    current_streak = 0
    for _, status in reversed(resolved_statuses):
        if status in ("completed", "rest"):
            current_streak += 1
            continue
        break

    longest_streak = 0
    running = 0
    for _, status in resolved_statuses:
        if status in ("completed", "rest"):
            running += 1
            longest_streak = max(longest_streak, running)
            continue
        running = 0

    gym_resolved = [s for _, s in resolved_statuses if s != "rest"]
    completion_rate = 0.0
    if gym_resolved:
        completion_rate = round((total_completed / len(gym_resolved)) * 100, 2)

    completed_dates = [key for key, status in resolved_statuses if status == "completed"]

    return StreakSummary(
        current_streak=current_streak,
        longest_streak=longest_streak,
        total_completed_days=total_completed,
        total_missed_days=total_missed,
        total_resolved_days=len(resolved_statuses),
        completion_rate=completion_rate,
        last_completed_date=completed_dates[-1] if completed_dates else None,
    )
