from __future__ import annotations

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

WEEKDAY_LOOKUP = {day.lower(): index for index, day in enumerate(WEEKDAY_NAMES)}

POLL_OPTION_STATUS = {
    0: "completed",
    1: "skipped",
}

MOTIVATION_QUOTES = [
    "Discipline beats mood. Show up for yourself today.",
    "Small reps become visible results. Keep the chain alive.",
    "You do not need perfect conditions. You need your next set.",
    "Consistency is the shortcut everybody wishes existed.",
    "Train with intention today so tomorrow feels earned.",
    "A strong routine turns motivation into momentum.",
    "Every session is a vote for the person you want to become.",
    "Progress is built quietly, one completed workout at a time.",
    "Rest with purpose, train with intensity, repeat with confidence.",
    "You are closer than you think. Keep your standard high today.",
]
