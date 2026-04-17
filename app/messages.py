from __future__ import annotations

from datetime import date
from html import escape

from app.constants import MOTIVATION_QUOTES, WEEKDAY_NAMES


def build_help_text(is_admin: bool) -> str:
    lines = [
        "<b>Commands</b>",
        "/start - Register and see the active weekly routine",
        "/plan - View the full weekly gym plan",
        "/today - See today's workout and receive today's poll if needed",
        "/stats - View your streak and completion stats",
        "/deregister - Leave the gym routine and stop notifications",
        "/help - Show this help message",
    ]

    if is_admin:
        lines.extend(
            [
                "",
                "<b>Admin Commands</b>",
                "/tasks - List routine tasks with task IDs",
                "/addtask Monday | Push Day | Bench Press, Incline Press, Dips",
                "/editday Monday | Push Day | Bench Press, Incline Press ;; Core | Planks, Leg Raises",
                "/setroutine with one line per day to replace the full weekly plan at once",
                "/deletetask TASK_ID",
                "/clearweekday Monday",
                "/settime 07:00",
                "/broadcastplan",
            ]
        )

    return "\n".join(lines)


def format_motivation_message(text: str) -> str:
    return f"<b>Daily Motivation</b>\n{escape(text)}"


def build_motivation_message(today: date) -> str:
    quote = MOTIVATION_QUOTES[today.toordinal() % len(MOTIVATION_QUOTES)]
    return format_motivation_message(quote)


def build_weekly_plan(plan: dict[str, list[dict]], show_ids: bool = False) -> str:
    lines = ["<b>Weekly Gym Routine</b>"]
    has_tasks = False

    for day in WEEKDAY_NAMES:
        tasks = plan.get(day, [])
        if not tasks:
            lines.append(f"\n<b>{day}</b>\nRest / Recovery")
            continue

        has_tasks = True
        lines.append(f"\n<b>{day}</b>")
        for task in tasks:
            task_line = escape(task["title"])
            if show_ids:
                task_line = f"{task_line} <code>({escape(task['task_id'])})</code>"
            lines.append(task_line)
            if task.get("details"):
                lines.append(f"  {escape(task['details'])}")

    if not has_tasks:
        lines.append("\nThe admin has not published a routine yet.")

    return "\n".join(lines)


def build_cycle_weekly_plan(
    plan: dict[str, tuple[list[dict], str]],
    sets_reps_info: str,
    show_ids: bool = False,
) -> str:
    lines = ["<b>Weekly Gym Routine (Rotating Cycle)</b>"]
    if sets_reps_info:
        lines.append(f"<i>{escape(sets_reps_info)}</i>")

    for day in WEEKDAY_NAMES:
        tasks, label = plan.get(day, ([], day))
        lines.append(f"\n<b>{escape(label)}</b>")
        if not tasks:
            lines.append("Rest / Recovery")
            continue
        for task in tasks:
            task_line = escape(task["title"])
            if show_ids and task.get("task_id") and task["task_id"] != "running":
                task_line = f"{task_line} <code>({escape(task['task_id'])})</code>"
            lines.append(f"• {task_line}")
            if task.get("details"):
                lines.append(f"  {escape(task['details'])}")

    return "\n".join(lines)


def build_today_message(day_name: str, tasks: list[dict], motivation_text: str, sets_reps_info: str = "") -> str:
    lines = [motivation_text, "", f"<b>{escape(day_name)} Routine</b>"]
    if not tasks:
        lines.append("Today is a recovery day. Stay hydrated, move a little, and come back strong tomorrow.")
        return "\n".join(lines)

    if sets_reps_info:
        lines.append(f"<i>{escape(sets_reps_info)}</i>")

    for task in tasks:
        lines.append(f"• {escape(task['title'])}")
        if task.get("details"):
            lines.append(f"  {escape(task['details'])}")

    lines.append("")
    lines.append("You will get a check-in poll right after this message.")
    return "\n".join(lines)


def build_stats_message(stats: dict) -> str:
    last_completed = stats.get("last_completed_date") or "No completed workout yet"
    return (
        "<b>Your Progress</b>\n"
        f"Current streak: <b>{stats.get('current_streak', 0)}</b>\n"
        f"Longest streak: <b>{stats.get('longest_streak', 0)}</b>\n"
        f"Completed workout days: <b>{stats.get('total_completed_days', 0)}</b>\n"
        f"Missed workout days: <b>{stats.get('total_missed_days', 0)}</b>\n"
        f"Completion rate: <b>{stats.get('completion_rate', 0.0)}%</b>\n"
        f"Last completed day: <b>{escape(str(last_completed))}</b>"
    )
