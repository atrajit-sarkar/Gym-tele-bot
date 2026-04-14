from __future__ import annotations

import unittest
from datetime import date

from app.streaks import compute_streak_summary


class StreakSummaryTests(unittest.TestCase):
    def test_rest_days_do_not_break_streak(self) -> None:
        summary = compute_streak_summary(
            joined_on=date(2026, 4, 6),
            scheduled_weekdays={0, 2, 4},
            checkins={
                "2026-04-06": "completed",
                "2026-04-08": "completed",
            },
            today=date(2026, 4, 9),
        )

        self.assertEqual(summary.current_streak, 2)
        self.assertEqual(summary.longest_streak, 2)
        self.assertEqual(summary.total_completed_days, 2)
        self.assertEqual(summary.total_missed_days, 0)

    def test_missed_scheduled_day_resets_current_streak(self) -> None:
        summary = compute_streak_summary(
            joined_on=date(2026, 4, 6),
            scheduled_weekdays={0, 2, 4},
            checkins={
                "2026-04-06": "completed",
                "2026-04-10": "completed",
            },
            today=date(2026, 4, 10),
        )

        self.assertEqual(summary.current_streak, 1)
        self.assertEqual(summary.longest_streak, 1)
        self.assertEqual(summary.total_completed_days, 2)
        self.assertEqual(summary.total_missed_days, 1)

    def test_unanswered_today_does_not_break_previous_streak(self) -> None:
        summary = compute_streak_summary(
            joined_on=date(2026, 4, 6),
            scheduled_weekdays={0, 2, 4},
            checkins={
                "2026-04-06": "completed",
                "2026-04-08": "completed",
            },
            today=date(2026, 4, 10),
        )

        self.assertEqual(summary.current_streak, 2)
        self.assertEqual(summary.longest_streak, 2)
        self.assertEqual(summary.total_completed_days, 2)
        self.assertEqual(summary.total_missed_days, 0)


if __name__ == "__main__":
    unittest.main()
