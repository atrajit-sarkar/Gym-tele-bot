# Gym Buddy Telegram Bot

Professional Telegram bot for daily gym motivation, weekly routine delivery, user check-ins, and streak tracking backed by Firebase Firestore.

## Features

- Daily motivation message for every registered user
- Plan-aware daily motivation generated through Ollama Cloud with fallback text if the API is unavailable
- Weekly recurring routine managed only by the admin
- Daily non-anonymous Telegram poll for workout completion tracking
- Firestore-backed user registry, weekly routine storage, poll dispatch mapping, and streak history
- Streak logic that respects workout days and does not break on rest days
- GitHub Actions cron workflow sends daily polls to day-specific topics in a Telegram group
- Firebase service account loading from a single base64 environment variable
- Optional named Firestore database selection with `FIRESTORE_DATABASE_ID`

## Bot Flow

1. The first person to send `/start` becomes the admin if `ADMIN_TELEGRAM_ID` is empty.
2. The admin creates recurring weekly tasks with `/addtask` or replaces a full day with `/editday`.
3. Every user who sends `/start` is registered and immediately receives the active weekly routine.
4. Every day at `REMINDER_TIME`, the bot sends motivation to all active users.
5. If that day has workout tasks, the bot also sends a non-anonymous poll.
6. Poll answers are stored in Firestore and streaks are recalculated automatically.

## Commands

### User

- `/start`
- `/plan`
- `/today`
- `/stats`
- `/deregister`
- `/help`

### Admin

- `/tasks`
- `/addtask Monday | Push Day | Bench Press, Incline Press, Dips`
- `/editday Monday | Push Day | Bench Press, Incline Press ;; Core | Planks, Leg Raises`
- `/setroutine` with one line per day to replace the full weekly routine at once
- `/deletetask TASK_ID`
- `/clearweekday Monday`
- `/settime 07:00`
- `/broadcastplan`

### Full Routine Paste Format

Use `/setroutine` followed by one line per training day:

```text
/setroutine Monday | Push Day | Bench Press, Incline Press ;; Core | Planks
Tuesday | Pull Day | Rows, Pull Ups
Wednesday | Legs | Squats, Lunges
Friday | Upper Power | Weighted Dips, Barbell Rows
```

Each line starts with the weekday.
Use `;;` to add more than one plan item to the same day.
Days you leave out will stay as rest / recovery days.

## Firestore Collections

- `settings/app`
- `users/{telegram_id}`
- `users/{telegram_id}/checkins/{yyyy-mm-dd}`
- `routine_tasks/{task_id}`
- `poll_dispatches/{poll_id}`
- `daily_motivations/{yyyy-mm-dd}`

## Local Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## GitHub Actions – Daily Poll to Group Topics

The daily workout poll is sent automatically via a GitHub Actions cron workflow.
Each day's poll is delivered to a specific topic (thread) in your Telegram group.

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_GROUP_CHAT_ID` | Chat ID of your Telegram group (usually negative, e.g. `-100xxxxxxxxxx`) |
| `FIREBASE_SERVICE_ACCOUNT_BASE64` | Base64-encoded Firebase service account JSON |
| `FIRESTORE_DATABASE_ID` | Named Firestore database ID (leave empty for `(default)`) |
| `BOT_TIMEZONE` | Timezone for date calculation (default: `Asia/Kolkata`) |
| `MONDAY_TOPIC_ID` | Thread ID of the Monday topic in your group |
| `TUESDAY_TOPIC_ID` | Thread ID of the Tuesday topic |
| `WEDNESDAY_TOPIC_ID` | Thread ID of the Wednesday topic |
| `THURSDAY_TOPIC_ID` | Thread ID of the Thursday topic |
| `FRIDAY_TOPIC_ID` | Thread ID of the Friday topic |
| `SATURDAY_TOPIC_ID` | Thread ID of the Saturday topic |
| `SUNDAY_TOPIC_ID` | Thread ID of the Sunday topic |

### How to find a topic's thread ID

Right-click a topic in your Telegram group → **Copy Link**.
The URL looks like `https://t.me/c/XXXXXXXXXX/123` — the last number (`123`) is the thread ID.

### Cron schedule

The workflow runs daily at **01:30 UTC** (07:00 IST). Edit the cron expression in
`.github/workflows/daily-poll.yml` to change the time.

You can also trigger the workflow manually from the **Actions** tab → **Daily Workout Poll** → **Run workflow**.
