from __future__ import annotations

import unittest

from app.parsers import parse_edit_day_input, parse_full_routine_input


class EditDayParserTests(unittest.TestCase):
    def test_parse_single_day_item(self) -> None:
        weekday, items = parse_edit_day_input("Monday | Push Day | Bench Press, Incline Press")

        self.assertEqual(weekday, "Monday")
        self.assertEqual(items, [("Push Day", "Bench Press, Incline Press")])

    def test_parse_multiple_day_items(self) -> None:
        weekday, items = parse_edit_day_input(
            "Wednesday | Pull Day | Pull Ups, Rows ;; Core | Hanging Leg Raises, Planks"
        )

        self.assertEqual(weekday, "Wednesday")
        self.assertEqual(
            items,
            [
                ("Pull Day", "Pull Ups, Rows"),
                ("Core", "Hanging Leg Raises, Planks"),
            ],
        )

    def test_parse_rejects_missing_details(self) -> None:
        with self.assertRaises(ValueError):
            parse_edit_day_input("Friday | Legs Only")

    def test_parse_full_routine(self) -> None:
        routine = parse_full_routine_input(
            "Monday | Push Day | Bench Press, Incline Press\n"
            "Tuesday | Pull Day | Rows, Pull Ups ;; Core | Planks"
        )

        self.assertEqual(
            routine,
            {
                "Monday": [("Push Day", "Bench Press, Incline Press")],
                "Tuesday": [
                    ("Pull Day", "Rows, Pull Ups"),
                    ("Core", "Planks"),
                ],
            },
        )

    def test_parse_full_routine_rejects_duplicate_weekday(self) -> None:
        with self.assertRaises(ValueError):
            parse_full_routine_input(
                "Monday | Push Day | Bench Press\n"
                "Monday | Core | Planks"
            )


if __name__ == "__main__":
    unittest.main()
