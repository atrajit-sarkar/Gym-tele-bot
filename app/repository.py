from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from google.cloud.firestore_v1 import Client

from app.constants import POLL_OPTION_STATUS, WEEKDAY_LOOKUP, WEEKDAY_NAMES
from app.streaks import StreakSummary, compute_streak_summary


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_weekday(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in WEEKDAY_LOOKUP:
        raise ValueError("Weekday must be one of Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday.")
    return WEEKDAY_NAMES[WEEKDAY_LOOKUP[normalized]]


@dataclass(frozen=True)
class ScheduleSettings:
    reminder_time: str


@dataclass(frozen=True)
class CycleConfig:
    cycle_start_date: date
    gym_weekday_indices: list[int]
    running_weekday_indices: list[int]
    rest_weekday_indices: list[int]
    num_cycle_days: int
    sets_reps_info: str

    @property
    def all_active_indices(self) -> set[int]:
        return set(self.gym_weekday_indices) | set(self.running_weekday_indices)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CycleConfig":
        return cls(
            cycle_start_date=date.fromisoformat(data["cycle_start_date"]),
            gym_weekday_indices=data["gym_weekday_indices"],
            running_weekday_indices=data["running_weekday_indices"],
            rest_weekday_indices=data["rest_weekday_indices"],
            num_cycle_days=data["num_cycle_days"],
            sets_reps_info=data.get("sets_reps_info", ""),
        )

    def get_day_info(self, target_date: date) -> tuple[int | None, str]:
        """Return (cycle_day_index, day_type). day_type is 'gym', 'running', or 'rest'."""
        weekday = target_date.weekday()
        if weekday in self.rest_weekday_indices:
            return None, "rest"
        if weekday in self.running_weekday_indices:
            return None, "running"
        if weekday not in self.gym_weekday_indices:
            return None, "rest"

        days_elapsed = (target_date - self.cycle_start_date).days
        week_number = days_elapsed // 7
        gym_slots_sorted = sorted(self.gym_weekday_indices)
        position_in_week = gym_slots_sorted.index(weekday)
        gym_slot_index = week_number * len(gym_slots_sorted) + position_in_week
        cycle_day = gym_slot_index % self.num_cycle_days
        return cycle_day, "gym"


class FirestoreRepository:
    def __init__(self, db: Client, default_admin_id: int | None, default_reminder_time: str) -> None:
        self.db = db
        self.default_admin_id = default_admin_id
        self.default_reminder_time = default_reminder_time
        self.settings_ref = self.db.collection("settings").document("app")
        self.users_ref = self.db.collection("users")
        self.tasks_ref = self.db.collection("routine_tasks")
        self.polls_ref = self.db.collection("poll_dispatches")
        self.daily_motivations_ref = self.db.collection("daily_motivations")

    def initialize_defaults(self) -> None:
        snapshot = self.settings_ref.get()
        payload: dict[str, Any] = {
            "reminder_time": self.default_reminder_time,
            "updated_at": _utcnow(),
        }

        if self.default_admin_id is not None:
            payload["admin_telegram_id"] = self.default_admin_id

        if snapshot.exists:
            current = snapshot.to_dict() or {}
            merged = {**payload, **current}
            if self.default_admin_id is not None and not current.get("admin_telegram_id"):
                merged["admin_telegram_id"] = self.default_admin_id
            self.settings_ref.set(merged, merge=True)
            return

        self.settings_ref.set(payload, merge=True)

    def get_schedule_settings(self) -> ScheduleSettings:
        snapshot = self.settings_ref.get()
        data = snapshot.to_dict() if snapshot.exists else {}
        return ScheduleSettings(reminder_time=data.get("reminder_time", self.default_reminder_time))

    def set_reminder_time(self, reminder_time: str) -> None:
        self.settings_ref.set(
            {
                "reminder_time": reminder_time,
                "updated_at": _utcnow(),
            },
            merge=True,
        )

    def get_admin_telegram_id(self) -> int | None:
        snapshot = self.settings_ref.get()
        data = snapshot.to_dict() if snapshot.exists else {}
        value = data.get("admin_telegram_id")
        return int(value) if value else None

    def ensure_user(self, telegram_user, chat_id: int, joined_on: date) -> tuple[dict[str, Any], bool]:
        user_id = telegram_user.id
        admin_id = self.get_admin_telegram_id()
        promoted = False

        if admin_id is None:
            admin_id = user_id
            self.settings_ref.set(
                {
                    "admin_telegram_id": user_id,
                    "updated_at": _utcnow(),
                },
                merge=True,
            )
            promoted = True

        is_admin = admin_id == user_id
        ref = self.users_ref.document(str(user_id))
        snapshot = ref.get()
        existing = snapshot.to_dict() if snapshot.exists else {}

        payload = {
            "telegram_id": user_id,
            "chat_id": chat_id,
            "first_name": telegram_user.first_name or "",
            "username": telegram_user.username or "",
            "is_admin": is_admin,
            "active": True,
            "deactivated_reason": None,
            "deregistered_at": None,
            "joined_on": existing.get("joined_on", joined_on.isoformat()),
            "updated_at": _utcnow(),
        }

        if not snapshot.exists:
            payload["joined_at"] = _utcnow()
            payload["current_streak"] = 0
            payload["longest_streak"] = 0
            payload["total_completed_days"] = 0
            payload["total_missed_days"] = 0
            payload["completion_rate"] = 0.0

        ref.set(payload, merge=True)
        merged = {**existing, **payload}
        return merged, promoted

    def deactivate_user(self, telegram_id: int, reason: str) -> None:
        self.users_ref.document(str(telegram_id)).set(
            {
                "active": False,
                "deactivated_reason": reason,
                "deregistered_at": _utcnow() if reason == "self_deregistered" else None,
                "updated_at": _utcnow(),
            },
            merge=True,
        )

    def mark_user_inactive(self, telegram_id: int) -> None:
        self.deactivate_user(telegram_id=telegram_id, reason="delivery_blocked")

    def deregister_user(self, telegram_id: int) -> None:
        self.deactivate_user(telegram_id=telegram_id, reason="self_deregistered")

    def is_admin(self, telegram_id: int) -> bool:
        return self.get_admin_telegram_id() == telegram_id

    def is_active_user(self, telegram_id: int) -> bool:
        snapshot = self.users_ref.document(str(telegram_id)).get()
        data = snapshot.to_dict() if snapshot.exists else {}
        return bool(data.get("active"))

    def list_active_users(self) -> list[dict[str, Any]]:
        docs = self.users_ref.where("active", "==", True).stream()
        return [doc.to_dict() for doc in docs]

    def _build_task_payload(self, weekday: str, title: str, details: str, created_by: int) -> dict[str, Any]:
        normalized_weekday = normalize_weekday(weekday)
        task_id = secrets.token_hex(4)
        return {
            "task_id": task_id,
            "weekday": normalized_weekday,
            "weekday_index": WEEKDAY_LOOKUP[normalized_weekday.lower()],
            "title": title.strip(),
            "details": details.strip(),
            "created_by": created_by,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
        }

    def create_task(self, weekday: str, title: str, details: str, created_by: int) -> dict[str, Any]:
        payload = self._build_task_payload(weekday=weekday, title=title, details=details, created_by=created_by)
        self.tasks_ref.document(payload["task_id"]).set(payload)
        return payload

    def replace_weekday_tasks(self, weekday: str, items: list[tuple[str, str]], created_by: int) -> list[dict[str, Any]]:
        normalized_weekday = normalize_weekday(weekday)
        batch = self.db.batch()
        existing_docs = list(self.tasks_ref.where("weekday", "==", normalized_weekday).stream())
        for doc in existing_docs:
            batch.delete(doc.reference)

        created: list[dict[str, Any]] = []
        for title, details in items:
            payload = self._build_task_payload(
                weekday=normalized_weekday,
                title=title,
                details=details,
                created_by=created_by,
            )
            batch.set(self.tasks_ref.document(payload["task_id"]), payload)
            created.append(payload)

        batch.commit()
        return created

    def replace_full_routine(
        self,
        routine: dict[str, list[tuple[str, str]]],
        created_by: int,
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_routine = {
            normalize_weekday(weekday): items
            for weekday, items in routine.items()
        }

        batch = self.db.batch()
        for doc in self.tasks_ref.stream():
            batch.delete(doc.reference)

        created: dict[str, list[dict[str, Any]]] = {}
        for weekday, items in normalized_routine.items():
            created[weekday] = []
            for title, details in items:
                payload = self._build_task_payload(
                    weekday=weekday,
                    title=title,
                    details=details,
                    created_by=created_by,
                )
                batch.set(self.tasks_ref.document(payload["task_id"]), payload)
                created[weekday].append(payload)

        batch.commit()
        return created

    def delete_task(self, task_id: str) -> dict[str, Any] | None:
        ref = self.tasks_ref.document(task_id.strip())
        snapshot = ref.get()
        if not snapshot.exists:
            return None
        payload = snapshot.to_dict() or {}
        ref.delete()
        return payload

    def clear_weekday(self, weekday: str) -> int:
        normalized_weekday = normalize_weekday(weekday)
        docs = self.tasks_ref.where("weekday", "==", normalized_weekday).stream()
        deleted = 0
        for doc in docs:
            doc.reference.delete()
            deleted += 1
        return deleted

    def list_tasks(self) -> list[dict[str, Any]]:
        docs = [doc.to_dict() for doc in self.tasks_ref.stream()]
        return sorted(docs, key=lambda item: (item["weekday_index"], item["title"].lower()))

    def get_weekly_plan(self) -> dict[str, list[dict[str, Any]]]:
        plan = {day: [] for day in WEEKDAY_NAMES}
        for task in self.list_tasks():
            plan.setdefault(task["weekday"], []).append(task)
        return plan

    def get_tasks_for_day(self, day_name: str) -> list[dict[str, Any]]:
        normalized_weekday = normalize_weekday(day_name)
        docs = [doc.to_dict() for doc in self.tasks_ref.where("weekday", "==", normalized_weekday).stream()]
        return sorted(docs, key=lambda item: item["title"].lower())

    # ── Cycle-based routine support ──────────────────────────────────

    def get_cycle_config(self) -> CycleConfig | None:
        snapshot = self.db.collection("settings").document("routine_cycle").get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if not data.get("enabled"):
            return None
        return CycleConfig.from_dict(data)

    def set_cycle_config(self, config_data: dict[str, Any]) -> None:
        config_data["updated_at"] = _utcnow()
        self.db.collection("settings").document("routine_cycle").set(config_data)

    def get_cycle_day_tasks(self, cycle_day: int) -> list[dict[str, Any]]:
        docs = [doc.to_dict() for doc in self.tasks_ref.where("cycle_day", "==", cycle_day).stream()]
        return sorted(docs, key=lambda item: item.get("order", 0))

    def get_todays_routine(self, today: date) -> tuple[list[dict[str, Any]], str]:
        """Return (tasks, day_label) considering any active cycle config."""
        cycle_config = self.get_cycle_config()
        day_name = today.strftime("%A")

        if cycle_config is None:
            tasks = self.get_tasks_for_day(day_name)
            return tasks, day_name

        cycle_day_index, day_type = cycle_config.get_day_info(today)

        if day_type == "rest":
            return [], f"{day_name} — Rest Day"

        if day_type == "running":
            running_task = {
                "task_id": "running",
                "title": "Running / Cardio",
                "details": "",
            }
            return [running_task], f"{day_name} — Running Day"

        tasks = self.get_cycle_day_tasks(cycle_day_index)
        day_label = f"{day_name} — Day {cycle_day_index + 1}"
        return tasks, day_label

    def get_weekly_plan_with_cycle(self, reference_date: date) -> tuple[dict[str, tuple[list[dict[str, Any]], str]], str]:
        """Return (plan, sets_reps_info) for the week containing reference_date."""
        cycle_config = self.get_cycle_config()
        if cycle_config is None:
            plan = self.get_weekly_plan()
            return {day: (tasks, day) for day, tasks in plan.items()}, ""

        all_tasks = [doc.to_dict() for doc in self.tasks_ref.stream()]
        tasks_by_cycle_day: dict[int, list[dict]] = {}
        for task in all_tasks:
            cd = task.get("cycle_day")
            if cd is not None and cd >= 0:
                tasks_by_cycle_day.setdefault(cd, []).append(task)
        for cd in tasks_by_cycle_day:
            tasks_by_cycle_day[cd].sort(key=lambda t: t.get("order", 0))

        monday = reference_date - timedelta(days=reference_date.weekday())
        result: dict[str, tuple[list[dict[str, Any]], str]] = {}
        for i in range(7):
            day_date = monday + timedelta(days=i)
            day_name = WEEKDAY_NAMES[i]
            cycle_day_index, day_type = cycle_config.get_day_info(day_date)

            if day_type == "rest":
                result[day_name] = ([], f"{day_name} — Rest Day")
            elif day_type == "running":
                running_task = {"task_id": "running", "title": "Running / Cardio", "details": ""}
                result[day_name] = ([running_task], f"{day_name} — Running Day")
            else:
                tasks = tasks_by_cycle_day.get(cycle_day_index, [])
                result[day_name] = (tasks, f"{day_name} — Day {cycle_day_index + 1}")

        return result, cycle_config.sets_reps_info

    def get_checkin(self, user_id: int, scheduled_date: date) -> dict[str, Any] | None:
        ref = self.users_ref.document(str(user_id)).collection("checkins").document(scheduled_date.isoformat())
        snapshot = ref.get()
        return snapshot.to_dict() if snapshot.exists else None

    def upsert_poll_dispatch(
        self,
        user_id: int,
        chat_id: int,
        scheduled_date: date,
        task_items: list[dict[str, Any]],
        poll_id: str,
        message_id: int,
    ) -> None:
        date_key = scheduled_date.isoformat()
        task_ids = [task["task_id"] for task in task_items]
        task_titles = [task["title"] for task in task_items]
        payload = {
            "date": date_key,
            "weekday": scheduled_date.strftime("%A"),
            "task_ids": task_ids,
            "task_titles": task_titles,
            "poll_id": poll_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "poll_sent_at": _utcnow(),
            "updated_at": _utcnow(),
        }

        self.users_ref.document(str(user_id)).collection("checkins").document(date_key).set(payload, merge=True)
        self.polls_ref.document(poll_id).set(
            {
                "poll_id": poll_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "scheduled_date": date_key,
                "weekday": scheduled_date.strftime("%A"),
                "task_ids": task_ids,
                "task_titles": task_titles,
                "message_id": message_id,
                "created_at": _utcnow(),
                "updated_at": _utcnow(),
            }
        )

    def record_poll_answer(self, poll_id: str, option_ids: list[int], today: date) -> tuple[dict[str, Any], StreakSummary]:
        poll_snapshot = self.polls_ref.document(poll_id).get()
        if not poll_snapshot.exists:
            raise ValueError("Poll dispatch not found.")

        poll_data = poll_snapshot.to_dict() or {}
        user_id = int(poll_data["user_id"])
        scheduled_date = poll_data["scheduled_date"]
        selected_index = option_ids[0] if option_ids else 1
        status = POLL_OPTION_STATUS.get(selected_index, "skipped")

        checkin_ref = self.users_ref.document(str(user_id)).collection("checkins").document(scheduled_date)
        checkin_ref.set(
            {
                "status": status,
                "answered_at": _utcnow(),
                "updated_at": _utcnow(),
            },
            merge=True,
        )

        summary = self.recalculate_user_stats(user_id=user_id, today=today)
        return poll_data, summary

    def _get_joined_on(self, user_id: int) -> date:
        snapshot = self.users_ref.document(str(user_id)).get()
        data = snapshot.to_dict() if snapshot.exists else {}
        joined_on = data.get("joined_on")
        if joined_on:
            return date.fromisoformat(joined_on)
        return date.today()

    def _get_scheduled_weekdays(self) -> set[int]:
        cycle_config = self.get_cycle_config()
        if cycle_config is not None:
            return cycle_config.all_active_indices
        return {task["weekday_index"] for task in self.list_tasks()}

    def _get_user_checkins(self, user_id: int) -> dict[str, str]:
        docs = self.users_ref.document(str(user_id)).collection("checkins").stream()
        mapping: dict[str, str] = {}
        for doc in docs:
            data = doc.to_dict() or {}
            status = data.get("status")
            if status:
                mapping[data["date"]] = status
        return mapping

    def recalculate_user_stats(self, user_id: int, today: date) -> StreakSummary:
        joined_on = self._get_joined_on(user_id)
        scheduled_weekdays = self._get_scheduled_weekdays()
        checkins = self._get_user_checkins(user_id)

        summary = compute_streak_summary(
            joined_on=joined_on,
            scheduled_weekdays=scheduled_weekdays,
            checkins=checkins,
            today=today,
        )

        self.users_ref.document(str(user_id)).set(
            {
                "current_streak": summary.current_streak,
                "longest_streak": summary.longest_streak,
                "total_completed_days": summary.total_completed_days,
                "total_missed_days": summary.total_missed_days,
                "total_resolved_days": summary.total_resolved_days,
                "completion_rate": summary.completion_rate,
                "last_completed_date": summary.last_completed_date,
                "updated_at": _utcnow(),
            },
            merge=True,
        )
        return summary

    def get_user_stats(self, user_id: int, today: date) -> dict[str, Any]:
        summary = self.recalculate_user_stats(user_id=user_id, today=today)
        return asdict(summary)

    def get_daily_motivation(self, scheduled_date: date) -> dict[str, Any] | None:
        snapshot = self.daily_motivations_ref.document(scheduled_date.isoformat()).get()
        return snapshot.to_dict() if snapshot.exists else None

    def save_daily_motivation(
        self,
        scheduled_date: date,
        weekday: str,
        task_items: list[dict[str, Any]],
        text: str,
        source: str,
        model_used: str | None,
    ) -> None:
        self.daily_motivations_ref.document(scheduled_date.isoformat()).set(
            {
                "date": scheduled_date.isoformat(),
                "weekday": weekday,
                "task_titles": [task["title"] for task in task_items],
                "text": text,
                "source": source,
                "model_used": model_used,
                "updated_at": _utcnow(),
            },
            merge=True,
        )

    def delete_daily_motivation(self, scheduled_date: date) -> None:
        self.daily_motivations_ref.document(scheduled_date.isoformat()).delete()
