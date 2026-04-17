"""Standalone script to send the daily workout poll to a Telegram group topic.

Designed to run from GitHub Actions on a cron schedule.
Reads the workout plan from Firestore and sends a poll to the
day-specific topic in a Telegram group.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

import firebase_admin
import httpx
from firebase_admin import credentials, firestore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger(__name__)

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

# Map each weekday to its env var for the topic/thread ID
TOPIC_ENV_VARS = {
    "Monday": "MONDAY_TOPIC_ID",
    "Tuesday": "TUESDAY_TOPIC_ID",
    "Wednesday": "WEDNESDAY_TOPIC_ID",
    "Thursday": "THURSDAY_TOPIC_ID",
    "Friday": "FRIDAY_TOPIC_ID",
    "Saturday": "SATURDAY_TOPIC_ID",
    "Sunday": "SUNDAY_TOPIC_ID",
}


def get_firestore_client() -> firestore.Client:
    firebase_base64 = os.environ["FIREBASE_SERVICE_ACCOUNT_BASE64"]
    decoded = base64.b64decode(firebase_base64.encode("utf-8"))
    cred_info = json.loads(decoded.decode("utf-8"))
    cred = credentials.Certificate(cred_info)
    app = firebase_admin.initialize_app(cred)
    database_id = os.getenv("FIRESTORE_DATABASE_ID") or None
    return firestore.client(app, database_id=database_id)


def get_tasks_for_day(db: firestore.Client, day_name: str) -> list[dict]:
    docs = db.collection("routine_tasks").where("weekday", "==", day_name).stream()
    tasks = [doc.to_dict() for doc in docs]
    return sorted(tasks, key=lambda t: t.get("title", "").lower())


def build_workout_message(day_name: str, tasks: list[dict]) -> str:
    lines = [f"<b>{escape(day_name)} Routine</b>"]
    if not tasks:
        lines.append("Today is a recovery day. Stay hydrated, move a little, and come back strong tomorrow.")
        return "\n".join(lines)

    for task in tasks:
        lines.append(f"- {escape(task.get('title', ''))}")
        details = task.get("details", "")
        if details:
            lines.append(f"  {escape(details)}")

    return "\n".join(lines)


async def send_poll(
    bot_token: str,
    chat_id: str,
    topic_id: int,
    day_name: str,
    tasks: list[dict],
) -> None:
    base_url = f"https://api.telegram.org/bot{bot_token}"

    async with httpx.AsyncClient(timeout=30) as client:
        # Send the workout plan message to the topic
        message_text = build_workout_message(day_name, tasks)
        msg_resp = await client.post(
            f"{base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "text": message_text,
                "parse_mode": "HTML",
            },
        )
        msg_resp.raise_for_status()
        LOGGER.info("Sent workout message to topic %s", topic_id)

        if not tasks:
            LOGGER.info("No tasks for %s — skipping poll.", day_name)
            return

        # Send the poll to the same topic
        poll_resp = await client.post(
            f"{base_url}/sendPoll",
            json={
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "question": f"Did you complete your {day_name} workout?",
                "options": json.dumps(["Completed", "Skipped"]),
                "is_anonymous": False,
                "allows_multiple_answers": False,
            },
        )
        poll_resp.raise_for_status()
        LOGGER.info("Sent poll to topic %s", topic_id)


def main() -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_GROUP_CHAT_ID"]
    timezone_name = os.getenv("BOT_TIMEZONE", "Asia/Kolkata")

    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    day_name = today.strftime("%A")

    topic_env = TOPIC_ENV_VARS.get(day_name)
    if not topic_env:
        LOGGER.error("No topic env var mapping for %s", day_name)
        sys.exit(1)

    topic_id_str = os.getenv(topic_env, "").strip()
    if not topic_id_str:
        LOGGER.error("Environment variable %s is not set. Skipping poll for %s.", topic_env, day_name)
        sys.exit(0)

    topic_id = int(topic_id_str)

    LOGGER.info("Today is %s (%s). Sending poll to topic %s.", today.isoformat(), day_name, topic_id)

    db = get_firestore_client()
    tasks = get_tasks_for_day(db, day_name)
    LOGGER.info("Found %d task(s) for %s.", len(tasks), day_name)

    asyncio.run(send_poll(
        bot_token=bot_token,
        chat_id=chat_id,
        topic_id=topic_id,
        day_name=day_name,
        tasks=tasks,
    ))

    LOGGER.info("Done.")


if __name__ == "__main__":
    main()
