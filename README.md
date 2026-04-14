# Gym Buddy Telegram Bot

Professional Telegram bot for daily gym motivation, weekly routine delivery, user check-ins, and streak tracking backed by Firebase Firestore.

## Features

- Daily motivation message for every registered user
- Plan-aware daily motivation generated through Ollama Cloud with fallback text if the API is unavailable
- Weekly recurring routine managed only by the admin
- Daily non-anonymous Telegram poll for workout completion tracking
- Firestore-backed user registry, weekly routine storage, poll dispatch mapping, and streak history
- Streak logic that respects workout days and does not break on rest days
- Railway-friendly Docker deployment
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

## Railway Deployment

1. Create a new Railway project from this repository.
2. Railway will detect the `Dockerfile` and run the bot as a worker service.
3. Open the Railway Raw Editor for variables.
4. Paste the contents of your local `.env`.
5. Deploy.

You do not need to upload the Firebase JSON file to Railway because the bot reads it from `FIREBASE_SERVICE_ACCOUNT_BASE64`.
If you are using a named Firestore database instead of `(default)`, set `FIRESTORE_DATABASE_ID` to that database ID, for example `gymbuddy`.
For Ollama Cloud, set `OLLAMA_API_BASE_URL=https://ollama.com/api`, add `OLLAMA_API_KEY`, and keep `OLLAMA_MODEL=gemini-3-flash-preview:cloud`.
