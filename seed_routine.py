"""Seed the rotating 3-day gym cycle into Firestore.

Cycle layout (repeating):
  Day 1 (Chest / Shoulders / Triceps)
  Day 2 (Back / Biceps)
  Day 3 (Legs)

Weekly schedule:
  Monday    → gym slot (Day 1, Day 2, Day 3 rotate through)
  Tuesday   → gym slot
  Wednesday → Running
  Thursday  → gym slot
  Friday    → gym slot (extra — 4 gym slots per week, cycling through 3 days)
  Saturday  → Running
  Sunday    → Rest

The cycle rotates continuously. With 4 gym slots per week and 3 gym days,
each 3-week period covers the full rotation evenly.

All exercises: 3 sets, reps 15 / 12 / 10
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import firebase_admin
from firebase_admin import credentials, firestore


def get_firestore_client() -> firestore.Client:
    raw = os.environ["FIREBASE_SERVICE_ACCOUNT_BASE64"]
    cred_info = json.loads(base64.b64decode(raw.encode("utf-8")).decode("utf-8"))
    cred = credentials.Certificate(cred_info)
    app = firebase_admin.initialize_app(cred)
    database_id = os.getenv("FIRESTORE_DATABASE_ID") or None
    return firestore.client(app, database_id=database_id)


# ── Routine definition ───────────────────────────────────────────────

CYCLE_DAYS = [
    {
        "label": "Day 1 — Chest / Shoulders / Triceps",
        "exercises": [
            "Smith Machine Bench Press",
            "Incline Bench Press",
            "Machine Fly",
            "D/B Shoulder Press",
            "D/B Front Raise",
            "D/B Side Lateral Raise",
            "Cable Press Downs",
            "D/B Kick Ups",
        ],
    },
    {
        "label": "Day 2 — Back / Biceps",
        "exercises": [
            "Chin Ups",
            "Lat Pull Downs",
            "Seated Machine Row",
            "B/B Deadlift",
            "Reverse Machine Fly",
            "B/B Curl",
            "D/B Hammer Curl",
        ],
    },
    {
        "label": "Day 3 — Legs",
        "exercises": [
            "B/B Squat",
            "Leg Extension",
            "Leg Curl",
            "Leg Press",
            "D/B Lunges",
        ],
    },
]

SETS_REPS = "All exercises: 3 sets — Reps: 15, 12, 10"

# Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
GYM_WEEKDAY_INDICES = [0, 1, 3, 4]        # Mon, Tue, Thu, Fri
RUNNING_WEEKDAY_INDICES = [2, 5]           # Wed, Sat
REST_WEEKDAY_INDICES = [6]                 # Sun


def main() -> None:
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    batch = db.batch()

    # 1. Clear existing routine_tasks
    for doc in db.collection("routine_tasks").stream():
        batch.delete(doc.reference)
    print("Clearing existing routine tasks...")

    # 2. Create tasks for each cycle day
    for cycle_day, day_info in enumerate(CYCLE_DAYS):
        for order, exercise_name in enumerate(day_info["exercises"]):
            task_id = secrets.token_hex(4)
            payload = {
                "task_id": task_id,
                "cycle_day": cycle_day,
                "order": order,
                "title": exercise_name,
                "details": SETS_REPS,
                "label": day_info["label"],
                "created_at": now,
                "updated_at": now,
            }
            batch.set(db.collection("routine_tasks").document(task_id), payload)
        print(f"  Added {len(day_info['exercises'])} exercises for {day_info['label']}")

    # 3. Store the cycle configuration
    # cycle_start_date = the most recent Monday (to anchor the cycle)
    today = date.today()
    days_since_monday = today.weekday()
    cycle_start = today if days_since_monday == 0 else today.__class__.fromordinal(today.toordinal() - days_since_monday)

    cycle_config = {
        "enabled": True,
        "cycle_start_date": cycle_start.isoformat(),
        "gym_weekday_indices": GYM_WEEKDAY_INDICES,
        "running_weekday_indices": RUNNING_WEEKDAY_INDICES,
        "rest_weekday_indices": REST_WEEKDAY_INDICES,
        "num_cycle_days": len(CYCLE_DAYS),
        "sets_reps_info": SETS_REPS,
        "updated_at": now,
    }
    batch.set(db.collection("settings").document("routine_cycle"), cycle_config)
    print(f"  Cycle config: start={cycle_start}, gym={GYM_WEEKDAY_INDICES}, running={RUNNING_WEEKDAY_INDICES}")

    batch.commit()
    print("\nDone! Routine seeded successfully.")
    print(f"Cycle starts on {cycle_start} (Monday)")
    print(f"3 gym days rotate across 4 gym slots/week (Mon, Tue, Thu, Fri)")
    print(f"Running on Wed & Sat, Rest on Sunday")


if __name__ == "__main__":
    main()
