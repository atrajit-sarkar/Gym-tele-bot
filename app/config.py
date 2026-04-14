from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value.strip())


def _parse_reminder_time(value: str) -> time:
    raw = value.strip()
    try:
        hour_text, minute_text = raw.split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError("REMINDER_TIME must use HH:MM format.") from exc

    if hour not in range(24) or minute not in range(60):
        raise ValueError("REMINDER_TIME must use a valid 24-hour clock value.")

    return time(hour=hour, minute=minute)


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    firebase_service_account_base64: str
    firestore_database_id: str | None
    ollama_api_base_url: str
    ollama_api_key: str | None
    ollama_model: str
    ollama_timeout_seconds: float
    bot_timezone_name: str
    reminder_time_text: str
    bot_name: str
    admin_telegram_id: int | None
    log_level: str

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.bot_timezone_name)

    @property
    def reminder_time(self) -> time:
        return _parse_reminder_time(self.reminder_time_text)

    def firebase_credentials_info(self) -> dict[str, Any]:
        decoded = base64.b64decode(self.firebase_service_account_base64.encode("utf-8"))
        return json.loads(decoded.decode("utf-8"))


def load_config() -> AppConfig:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    firebase_base64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64", "").strip()
    firestore_database_id = os.getenv("FIRESTORE_DATABASE_ID", "").strip() or None
    ollama_api_base_url = (
        os.getenv("OLLAMA_API_BASE_URL")
        or os.getenv("OLLAMA_API_URL")
        or "https://ollama.com/api"
    ).strip() or "https://ollama.com/api"
    ollama_api_key = os.getenv("OLLAMA_API_KEY", "").strip() or None
    ollama_model = os.getenv("OLLAMA_MODEL", "gemini-3-flash-preview:cloud").strip() or "gemini-3-flash-preview:cloud"
    ollama_timeout_seconds = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45").strip() or "45")

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")
    if not firebase_base64:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_BASE64 is required.")

    timezone_name = os.getenv("BOT_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
    reminder_time_text = os.getenv("REMINDER_TIME", "07:00").strip() or "07:00"
    bot_name = os.getenv("BOT_NAME", "Gym Buddy").strip() or "Gym Buddy"
    admin_telegram_id = _parse_optional_int(os.getenv("ADMIN_TELEGRAM_ID"))
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return AppConfig(
        telegram_bot_token=token,
        firebase_service_account_base64=firebase_base64,
        firestore_database_id=firestore_database_id,
        ollama_api_base_url=ollama_api_base_url.rstrip("/"),
        ollama_api_key=ollama_api_key,
        ollama_model=ollama_model,
        ollama_timeout_seconds=ollama_timeout_seconds,
        bot_timezone_name=timezone_name,
        reminder_time_text=reminder_time_text,
        bot_name=bot_name,
        admin_telegram_id=admin_telegram_id,
        log_level=log_level,
    )
