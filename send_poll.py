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

from app.repository import CycleConfig

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

FALLBACK_QUOTES = [
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


def get_cycle_day_tasks(db: firestore.Client, cycle_day: int) -> list[dict]:
    docs = db.collection("routine_tasks").where("cycle_day", "==", cycle_day).stream()
    tasks = [doc.to_dict() for doc in docs]
    return sorted(tasks, key=lambda t: t.get("order", 0))


def get_todays_routine(db: firestore.Client, today: date) -> tuple[list[dict], str]:
    """Return (tasks, day_label) considering any active cycle config."""
    snapshot = db.collection("settings").document("routine_cycle").get()
    if snapshot.exists:
        data = snapshot.to_dict() or {}
        if data.get("enabled"):
            cycle_config = CycleConfig.from_dict(data)
            cycle_day_index, day_type = cycle_config.get_day_info(today)
            day_name = today.strftime("%A")

            if day_type == "rest":
                return [], f"{day_name} — Rest Day"
            if day_type == "running":
                return [{"task_id": "running", "title": "Running / Cardio", "details": ""}], f"{day_name} — Running Day"

            tasks = get_cycle_day_tasks(db, cycle_day_index)
            return tasks, f"{day_name} — Day {cycle_day_index + 1}"

    day_name = today.strftime("%A")
    tasks = get_tasks_for_day(db, day_name)
    return tasks, day_name


async def generate_motivation(day_name: str, tasks: list[dict], today: date) -> str:
    """Generate a motivation message using Ollama, falling back to built-in quotes."""
    api_url = os.getenv("OLLAMA_API_BASE_URL", "").strip()
    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    model = os.getenv("OLLAMA_MODEL", "gemini-3-flash-preview:cloud").strip()
    timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45"))

    if not api_key or not api_url:
        LOGGER.info("Ollama API not configured — using fallback motivation.")
        return _fallback_motivation(day_name, tasks, today)

    prompt = _build_motivation_prompt(day_name, tasks)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an elite but supportive gym coach. "
                    "Write a short daily motivation message based on the provided workout plan. "
                    "Requirements: 2 to 4 sentences, under 90 words, plain text only, no emojis, no hashtags, "
                    "no markdown, and make it feel specific to the session."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{api_url.rstrip('/')}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            if text:
                cleaned = " ".join(line.strip(" -\t") for line in text.splitlines() if line.strip())[:500]
                LOGGER.info("Generated motivation via Ollama (%s).", model)
                return cleaned
    except Exception:
        LOGGER.exception("Ollama motivation failed — using fallback.")

    return _fallback_motivation(day_name, tasks, today)


def _build_motivation_prompt(day_name: str, tasks: list[dict]) -> str:
    if not tasks:
        return (
            f"Day: {day_name}\n"
            "Plan: Recovery / Rest Day\n"
            "Write motivation that respects recovery, consistency, and discipline."
        )
    lines = [f"Day: {day_name}", "Plan:"]
    for task in tasks:
        details = task.get("details", "").strip()
        if details:
            lines.append(f"- {task['title']}: {details}")
        else:
            lines.append(f"- {task['title']}")
    lines.append("Write motivation that matches this training focus and pushes the athlete to complete the session.")
    return "\n".join(lines)


def _fallback_motivation(day_name: str, tasks: list[dict], today: date) -> str:
    quote = FALLBACK_QUOTES[today.toordinal() % len(FALLBACK_QUOTES)]
    if tasks:
        titles = ", ".join(task["title"] for task in tasks[:2])
        if len(tasks) > 2:
            titles = f"{titles}, and more"
        return f"{quote} Today's {day_name} focus is {titles}. Lock in and finish the work with clean form."
    return (
        "Recovery is part of the program. Use today to reset, refuel, and protect the streak by showing up strong "
        "for the next session."
    )


def build_workout_message(day_name: str, tasks: list[dict], motivation: str) -> str:
    lines = [f"<b>Daily Motivation</b>\n{escape(motivation)}", "", f"<b>{escape(day_name)} Routine</b>"]
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
) -> dict | None:
    """Send workout message and poll. Returns poll info dict or None."""
    base_url = f"https://api.telegram.org/bot{bot_token}"

    async with httpx.AsyncClient(timeout=30) as client:
        # Send the workout plan message to the topic
        motivation = await generate_motivation(day_name, tasks, datetime.now(ZoneInfo(os.getenv('BOT_TIMEZONE', 'Asia/Kolkata'))).date())
        message_text = build_workout_message(day_name, tasks, motivation)
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
            return None

        # Send the poll to the same topic
        poll_resp = await client.post(
            f"{base_url}/sendPoll",
            json={
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "question": f"Are you going for the {day_name} workout?",
                "options": json.dumps(["Yes, let's go!", "Skipping today"]),
                "is_anonymous": False,
                "allows_multiple_answers": False,
            },
        )
        poll_resp.raise_for_status()
        LOGGER.info("Sent poll to topic %s", topic_id)

        result = poll_resp.json().get("result", {})
        poll_id = (result.get("poll") or {}).get("id")
        message_id = result.get("message_id")
        if poll_id:
            return {"poll_id": poll_id, "message_id": message_id}
        return None


def store_poll_dispatch(
    db: firestore.Client,
    poll_id: str,
    message_id: int,
    chat_id: str,
    topic_id: int,
    day_name: str,
    scheduled_date: date,
    tasks: list[dict],
) -> None:
    """Store poll metadata in Firestore so the webhook worker can look it up."""
    from datetime import timezone as tz
    db.collection("poll_dispatches").document(poll_id).set({
        "poll_id": poll_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "scheduled_date": scheduled_date.isoformat(),
        "weekday": day_name,
        "task_ids": [t.get("task_id", "") for t in tasks],
        "task_titles": [t.get("title", "") for t in tasks],
        "message_id": message_id,
        "created_at": datetime.now(tz.utc),
    })
    LOGGER.info("Stored poll dispatch %s in Firestore.", poll_id)


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
    tasks, day_label = get_todays_routine(db, today)
    LOGGER.info("Found %d task(s) for %s (%s).", len(tasks), day_name, day_label)

    poll_info = asyncio.run(send_poll(
        bot_token=bot_token,
        chat_id=chat_id,
        topic_id=topic_id,
        day_name=day_label,
        tasks=tasks,
    ))

    if poll_info:
        store_poll_dispatch(
            db=db,
            poll_id=poll_info["poll_id"],
            message_id=poll_info["message_id"],
            chat_id=chat_id,
            topic_id=topic_id,
            day_name=day_name,
            scheduled_date=today,
            tasks=tasks,
        )

    LOGGER.info("Done.")


if __name__ == "__main__":
    main()
