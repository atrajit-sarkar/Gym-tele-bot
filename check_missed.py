"""Midnight cron job: mark non-responders as missed and DM them a progress report.

Runs via GitHub Actions at 18:30 UTC (00:00 IST next day).
Checks today's poll dispatch. Any registered user who did NOT check in
for today is recorded as "missed" and receives an HTML progress report DM.

Sunday (rest day) is skipped entirely — no auto-miss, no auto-DM.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timezone
from html import escape as h
from zoneinfo import ZoneInfo

import firebase_admin
import httpx
from firebase_admin import credentials, firestore

from app.repository import CycleConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOG = logging.getLogger(__name__)

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
REST_DAY_INDEX = 6  # Sunday (Mon=0 ... Sun=6)


# ── Firebase setup ────────────────────────────────────────────────────

def get_firestore_client() -> firestore.Client:
    raw = os.environ["FIREBASE_SERVICE_ACCOUNT_BASE64"]
    cred_info = json.loads(base64.b64decode(raw.encode("utf-8")).decode("utf-8"))
    cred = credentials.Certificate(cred_info)
    app = firebase_admin.initialize_app(cred)
    database_id = os.getenv("FIRESTORE_DATABASE_ID") or None
    return firestore.client(app, database_id=database_id)


# ── Streak / history (mirrors worker logic) ──────────────────────────

def calculate_streaks(
    joined_on: date,
    scheduled_weekdays: set[int],
    checkins: dict[str, str],
    today: date,
) -> dict:
    if not scheduled_weekdays:
        return {"current_streak": 0, "longest_streak": 0, "total_completed": 0, "total_missed": 0, "completion_rate": 0}

    resolved: list[dict] = []
    d = joined_on
    from datetime import timedelta
    while d <= today:
        dow = d.weekday()  # Mon=0 ... Sun=6
        if dow not in scheduled_weekdays:
            d += timedelta(days=1)
            continue
        key = d.isoformat()
        status = checkins.get(key)
        if key == today.isoformat() and not status:
            d += timedelta(days=1)
            continue
        if key < today.isoformat() and not status:
            status = "missed"
        if status:
            resolved.append({"date": key, "status": status})
        d += timedelta(days=1)

    total_completed = sum(1 for r in resolved if r["status"] == "completed")
    total_missed = len(resolved) - total_completed

    current_streak = 0
    for r in reversed(resolved):
        if r["status"] == "completed":
            current_streak += 1
        else:
            break

    longest_streak = running = 0
    for r in resolved:
        if r["status"] == "completed":
            running += 1
            longest_streak = max(longest_streak, running)
        else:
            running = 0

    rate = round(total_completed / len(resolved) * 100, 2) if resolved else 0
    return {
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "total_completed": total_completed,
        "total_missed": total_missed,
        "completion_rate": rate,
    }


def build_history(
    joined_on: date,
    scheduled_weekdays: set[int],
    checkins: dict[str, str],
    tasks_by_weekday: dict[str, list[str]],
    today: date,
) -> list[dict]:
    from datetime import timedelta
    history: list[dict] = []
    d = joined_on
    while d <= today:
        dow = d.weekday()
        key = d.isoformat()
        weekday_name = WEEKDAY_NAMES[dow]

        if dow not in scheduled_weekdays:
            if key <= today.isoformat():
                history.append({"date": key, "weekday": weekday_name, "status": "rest", "tasks": ["Rest Day"]})
            d += timedelta(days=1)
            continue

        status = checkins.get(key)
        if key == today.isoformat() and not status:
            d += timedelta(days=1)
            continue
        if key < today.isoformat() and not status:
            status = "missed"
        if status:
            day_tasks = tasks_by_weekday.get(weekday_name, [])
            history.append({"date": key, "weekday": weekday_name, "status": status, "tasks": day_tasks or ["\u2014"]})
        d += timedelta(days=1)

    history.reverse()
    return history


# ── HTML report (mirrors worker template) ────────────────────────────

def build_progress_html(
    first_name: str,
    stats: dict,
    today_weekday: str,
    today_status: str,
    today_tasks: list[dict],
    history: list[dict],
) -> str:
    pct = stats["completion_rate"]
    circ = 2 * math.pi * 54
    offset = circ - (pct / 100) * circ

    if today_tasks:
        today_html = "".join(
            f'<div class="today-task"><span class="task-name">{h(t["title"])}</span>'
            + (f'<span class="task-detail">{h(t.get("details",""))}</span>' if t.get("details") else "")
            + "</div>"
            for t in today_tasks
        )
    else:
        today_html = '<div class="today-task"><span class="task-name">Rest Day</span></div>'

    badge = (
        '<span class="badge badge-done">Going for it</span>'
        if today_status == "completed"
        else '<span class="badge badge-miss">Missed</span>'
        if today_status == "missed"
        else '<span class="badge badge-skip">Skipping</span>'
    )

    rows = []
    for entry in history:
        d = date.fromisoformat(entry["date"])
        date_str = d.strftime("%d %b %Y")
        icon_map = {"completed": "check", "skipped": "skip", "rest": "rest", "missed": "miss"}
        icon = icon_map.get(entry["status"], "miss")
        task_list = ", ".join(entry["tasks"]) if entry["tasks"] else "\u2014"
        rows.append(
            f'<tr class="row-{icon}">'
            f'<td class="cell-date"><span class="date-day">{date_str}</span>'
            f'<span class="date-weekday">{h(entry["weekday"])}</span></td>'
            f'<td class="cell-workout">{h(task_list)}</td>'
            f'<td class="cell-status"><span class="status-icon status-{icon}"></span></td></tr>'
        )
    history_html = "\n".join(rows)

    return _HTML_TEMPLATE.format(
        name=h(first_name),
        circ=f"{circ:.2f}",
        offset=f"{offset:.2f}",
        pct=round(pct),
        current_streak=stats["current_streak"],
        longest_streak=stats["longest_streak"],
        today_weekday=h(today_weekday),
        badge=badge,
        today_html=today_html,
        history_html=history_html,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Progress Report \u2013 {name}</title>
<style>
  :root {{
    --bg: #0a0a0f; --surface: #13131a; --surface-2: #1a1a24;
    --border: #25253a; --text: #e4e4ed; --text-dim: #8888a0;
    --accent: #6c5ce7; --green: #00d26a; --green-dim: rgba(0,210,106,.12);
    --red: #ff4757; --red-dim: rgba(255,71,87,.12);
    --yellow: #ffa502; --yellow-dim: rgba(255,165,2,.12);
    --radius: 16px; --radius-sm: 10px;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
    background:var(--bg); color:var(--text); min-height:100vh; -webkit-font-smoothing:antialiased; }}
  .container {{ max-width:480px; margin:0 auto; padding:24px 16px 40px; }}
  .header {{ text-align:center; padding:32px 0 8px; }}
  .header h1 {{ font-size:22px; font-weight:700; letter-spacing:-.3px; }}
  .header .subtitle {{ font-size:13px; color:var(--text-dim); margin-top:4px; }}
  .ring-section {{ display:flex; justify-content:center; padding:28px 0 20px; }}
  .ring-wrap {{ position:relative; width:140px; height:140px; }}
  .ring-wrap svg {{ transform:rotate(-90deg); width:140px; height:140px; }}
  .ring-bg {{ stroke:var(--surface-2); }}
  .ring-fill {{ stroke:var(--accent); stroke-linecap:round; }}
  .ring-label {{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; }}
  .ring-pct {{ font-size:32px; font-weight:800; letter-spacing:-1px; line-height:1; }}
  .ring-sub {{ font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px; margin-top:4px; }}
  .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:20px; }}
  .stat-card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:16px; text-align:center; }}
  .stat-value {{ font-size:28px; font-weight:800; letter-spacing:-.5px; line-height:1; }}
  .stat-value.accent {{ color:var(--accent); }}
  .stat-value.green {{ color:var(--green); }}
  .stat-label {{ font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.8px; margin-top:6px; }}
  .section {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:16px; }}
  .section-title {{ font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:1px; color:var(--text-dim); margin-bottom:14px; display:flex; align-items:center; justify-content:space-between; }}
  .today-task {{ padding:10px 0; border-bottom:1px solid var(--border); display:flex; flex-direction:column; gap:2px; }}
  .today-task:last-child {{ border-bottom:none; }}
  .task-name {{ font-size:15px; font-weight:600; }}
  .task-detail {{ font-size:13px; color:var(--text-dim); }}
  .badge {{ font-size:11px; font-weight:600; padding:4px 10px; border-radius:20px; text-transform:uppercase; letter-spacing:.5px; }}
  .badge-done {{ background:var(--green-dim); color:var(--green); }}
  .badge-skip {{ background:var(--yellow-dim); color:var(--yellow); }}
  .badge-miss {{ background:var(--red-dim); color:var(--red); }}
  .history-table {{ width:100%; border-collapse:collapse; }}
  .history-table tr {{ border-bottom:1px solid var(--border); }}
  .history-table tr:last-child {{ border-bottom:none; }}
  .history-table td {{ padding:12px 0; vertical-align:middle; }}
  .cell-date {{ display:flex; flex-direction:column; gap:1px; width:110px; }}
  .date-day {{ font-size:14px; font-weight:600; }}
  .date-weekday {{ font-size:11px; color:var(--text-dim); }}
  .cell-workout {{ font-size:13px; color:var(--text-dim); padding:0 8px; }}
  .cell-status {{ text-align:right; width:36px; }}
  .status-icon {{ display:inline-block; width:10px; height:10px; border-radius:50%; }}
  .status-check {{ background:var(--green); box-shadow:0 0 6px var(--green); }}
  .status-skip {{ background:var(--yellow); box-shadow:0 0 6px var(--yellow); }}
  .status-miss {{ background:var(--red); box-shadow:0 0 6px var(--red); }}
  .status-rest {{ background:#8888a0; box-shadow:0 0 6px #8888a0; }}
  .legend {{ display:flex; gap:16px; justify-content:center; padding:12px 0 0; }}
  .legend-item {{ display:flex; align-items:center; gap:6px; font-size:11px; color:var(--text-dim); }}
  .legend-dot {{ width:8px; height:8px; border-radius:50%; }}
  .footer {{ text-align:center; padding:28px 0 8px; font-size:12px; color:var(--text-dim); }}
  .footer span {{ color:var(--accent); font-weight:600; }}
</style>
</head>
<body>
<div class="container">
  <div class="header"><h1>{name}'s Progress</h1><div class="subtitle">Gym Buddy Report</div></div>
  <div class="ring-section"><div class="ring-wrap">
    <svg viewBox="0 0 120 120">
      <circle class="ring-bg" cx="60" cy="60" r="54" fill="none" stroke-width="8"/>
      <circle class="ring-fill" cx="60" cy="60" r="54" fill="none" stroke-width="8"
        stroke-dasharray="{circ}" stroke-dashoffset="{offset}"/>
    </svg>
    <div class="ring-label"><span class="ring-pct">{pct}%</span><span class="ring-sub">Complete</span></div>
  </div></div>
  <div class="stats">
    <div class="stat-card"><div class="stat-value accent">{current_streak}</div><div class="stat-label">Current Streak</div></div>
    <div class="stat-card"><div class="stat-value green">{longest_streak}</div><div class="stat-label">Longest Streak</div></div>
  </div>
  <div class="section">
    <div class="section-title"><span>Today \u2014 {today_weekday}</span>{badge}</div>
    {today_html}
  </div>
  <div class="section">
    <div class="section-title"><span>Workout History</span></div>
    <table class="history-table">{history_html}</table>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:var(--green)"></div> Completed</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--yellow)"></div> Skipped</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--red)"></div> Missed</div>
      <div class="legend-item"><div class="legend-dot" style="background:#8888a0"></div> Rest</div>
    </div>
  </div>
  <div class="footer">Powered by <span>Gym Buddy</span></div>
</div>
</body>
</html>"""


# ── Telegram helpers ─────────────────────────────────────────────────

async def send_document(client: httpx.AsyncClient, bot_token: str, user_id: int, html: str, filename: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    files = {"document": (filename, html.encode("utf-8"), "text/html")}
    data = {"chat_id": str(user_id), "caption": "You missed today\u2019s workout poll. Here\u2019s your progress report."}
    resp = await client.post(url, data=data, files=files)
    if resp.status_code != 200:
        LOG.error("sendDocument to %s failed: %s", user_id, resp.text)
    else:
        LOG.info("Sent progress report to user %s", user_id)


# ── Main logic ───────────────────────────────────────────────────────

async def run() -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tz = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Kolkata"))
    today = datetime.now(tz).date()
    day_name = today.strftime("%A")
    today_str = today.isoformat()

    db = get_firestore_client()

    # Check if today is a rest day (cycle or legacy)
    cycle_snapshot = db.collection("settings").document("routine_cycle").get()
    is_rest_day = False
    cycle_active = False
    if cycle_snapshot.exists:
        cycle_data = cycle_snapshot.to_dict() or {}
        if cycle_data.get("enabled"):
            cycle_active = True
            cycle_config = CycleConfig.from_dict(cycle_data)
            _, day_type = cycle_config.get_day_info(today)
            if day_type == "rest":
                is_rest_day = True

    if not cycle_active and not is_rest_day and today.weekday() == REST_DAY_INDEX:
        is_rest_day = True

    if is_rest_day:
        LOG.info("Today is a rest day. Nothing to do.")
        return

    LOG.info("Checking missed responses for %s (%s)", today_str, day_name)

    # Find today's poll dispatch
    dispatches = list(
        db.collection("poll_dispatches")
        .where("scheduled_date", "==", today_str)
        .stream()
    )
    if not dispatches:
        LOG.info("No poll dispatch found for %s. Skipping.", today_str)
        return

    # Get all registered users
    user_docs = list(db.collection("users").stream())
    if not user_docs:
        LOG.info("No registered users.")
        return

    # Gather scheduled weekdays and tasks-by-weekday from routine_tasks
    task_docs = list(db.collection("routine_tasks").stream())
    scheduled_weekdays: set[int] = set()
    tasks_by_weekday: dict[str, list] = {}
    tasks_by_weekday_full: dict[str, list[dict]] = {}
    tasks_by_cycle_day: dict[int, list[dict]] = {}

    # Check for cycle config to determine scheduled weekdays
    cycle_enabled = False
    cycle_cfg = None
    if cycle_snapshot.exists:
        cycle_data_check = cycle_snapshot.to_dict() or {}
        if cycle_data_check.get("enabled"):
            cycle_cfg = CycleConfig.from_dict(cycle_data_check)
            scheduled_weekdays = cycle_cfg.all_active_indices
            cycle_enabled = True

    for td in task_docs:
        d = td.to_dict()
        idx = d.get("weekday_index")
        wkday = d.get("weekday", "")
        title = d.get("title", "")
        details = d.get("details", "")
        cycle_day = d.get("cycle_day")
        if idx is not None and not cycle_enabled:
            scheduled_weekdays.add(int(idx))
        if wkday:
            tasks_by_weekday.setdefault(wkday, []).append(title or "\u2014")
            tasks_by_weekday_full.setdefault(wkday, []).append({"title": title, "details": details})
        if cycle_day is not None:
            tasks_by_cycle_day.setdefault(int(cycle_day), []).append({"title": title, "details": details, "order": d.get("order", 0)})

    # Sort cycle day tasks by order
    for cd in tasks_by_cycle_day:
        tasks_by_cycle_day[cd].sort(key=lambda t: t["order"])

    # Determine today's tasks
    if cycle_enabled and cycle_cfg is not None:
        cycle_day_index, day_type = cycle_cfg.get_day_info(today)
        if day_type == "running":
            today_tasks = [{"title": "Running / Cardio", "details": ""}]
        elif day_type == "gym" and cycle_day_index is not None:
            today_tasks = tasks_by_cycle_day.get(cycle_day_index, [])
        else:
            today_tasks = []
    else:
        today_tasks = tasks_by_weekday_full.get(day_name, [])

    missed_users: list[dict] = []
    for user_doc in user_docs:
        uid = user_doc.id
        udata = user_doc.to_dict()

        # Check if they already checked in today (status field present = answered)
        checkin_ref = db.collection("users").document(uid).collection("checkins").document(today_str)
        checkin_snap = checkin_ref.get()
        if checkin_snap.exists and (checkin_snap.to_dict() or {}).get("status"):
            continue  # already responded

        first_name = udata.get("first_name", "Someone")
        missed_users.append({"uid": uid, "first_name": first_name, "data": udata})

    if not missed_users:
        LOG.info("All users responded. No missed entries.")
        return

    LOG.info("Found %d user(s) who missed today's poll.", len(missed_users))

    async with httpx.AsyncClient(timeout=30) as client:
        for mu in missed_users:
            uid = mu["uid"]
            first_name = mu["first_name"]
            joined_on_str = mu["data"].get("joined_on", today_str)
            joined_on = date.fromisoformat(joined_on_str) if isinstance(joined_on_str, str) else today

            # Record missed checkin
            now = datetime.now(timezone.utc).isoformat()
            db.collection("users").document(uid).collection("checkins").document(today_str).set({
                "date": today_str,
                "weekday": day_name,
                "status": "missed",
                "user_id": int(uid),
                "first_name": first_name,
                "answered_at": None,
                "updated_at": now,
                "auto_missed": True,
            })
            LOG.info("Recorded missed for user %s on %s", uid, today_str)

            # Compute progress
            checkin_docs = list(db.collection("users").document(uid).collection("checkins").stream())
            checkin_map: dict[str, str] = {}
            for cd in checkin_docs:
                cdata = cd.to_dict()
                cdate = cdata.get("date", "")
                cstatus = cdata.get("status", "")
                if cdate and cstatus:
                    checkin_map[cdate] = cstatus

            streaks = calculate_streaks(joined_on, scheduled_weekdays, checkin_map, today)
            history = build_history(joined_on, scheduled_weekdays, checkin_map, tasks_by_weekday, today)

            html = build_progress_html(
                first_name=first_name,
                stats=streaks,
                today_weekday=day_name,
                today_status="missed",
                today_tasks=today_tasks,
                history=history,
            )

            await send_document(client, bot_token, int(uid), html, f"progress-{today_str}.html")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
