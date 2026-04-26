/**
 * Cloudflare Worker – Telegram webhook handler for gym poll answers.
 *
 * Receives poll_answer updates from Telegram, looks up the poll in
 * Firestore, records the user's check-in, and sends a brief
 * confirmation to the group topic.
 */

// ── Entry point ──────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    // Verify Telegram webhook secret
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secret !== env.WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const update = await request.json();

    // Only process poll_answer updates
    if (!update.poll_answer) {
      return new Response("OK", { status: 200 });
    }

    try {
      await handlePollAnswer(update.poll_answer, env);
    } catch (err) {
      console.error("Error handling poll answer:", err);
    }

    // Always return 200 so Telegram doesn't retry
    return new Response("OK", { status: 200 });
  },
};

// ── Poll answer handler ──────────────────────────────────────────────

const OPTION_STATUS = { 0: "completed", 1: "skipped" };
const REST_DAY_INDEX = 6; // Sunday (Mon=0 ... Sun=6)

async function handlePollAnswer(pollAnswer, env) {
  const { poll_id, user, option_ids } = pollAnswer;
  if (!option_ids || option_ids.length === 0) {
    // User retracted their vote — ignore
    return;
  }

  const userId = user.id;
  const firstName = user.first_name || "Someone";
  const status = OPTION_STATUS[option_ids[0]] ?? "skipped";

  const sa = parseServiceAccount(env.FIREBASE_SERVICE_ACCOUNT_BASE64);
  const accessToken = await getAccessToken(sa);
  const dbPath = firestoreBasePath(sa.project_id, env.FIRESTORE_DATABASE_ID);

  // Look up which day / topic this poll belongs to
  const pollDoc = await firestoreGet(dbPath, accessToken, `poll_dispatches/${poll_id}`);
  if (!pollDoc) {
    console.log(`Poll dispatch not found: ${poll_id}`);
    return;
  }

  const scheduledDate = pollDoc.fields.scheduled_date.stringValue;
  const weekday = pollDoc.fields.weekday.stringValue;
  const chatId = pollDoc.fields.chat_id.stringValue;
  const topicId = pollDoc.fields.topic_id.integerValue;

  // Record check-in
  const now = new Date().toISOString();
  await firestorePatch(dbPath, accessToken, `users/${userId}/checkins/${scheduledDate}`, {
    fields: {
      date: { stringValue: scheduledDate },
      weekday: { stringValue: weekday },
      status: { stringValue: status },
      poll_id: { stringValue: poll_id },
      user_id: { integerValue: String(userId) },
      first_name: { stringValue: firstName },
      answered_at: { timestampValue: now },
      updated_at: { timestampValue: now },
    },
  });

  console.log(`Recorded ${status} for user ${userId} on ${scheduledDate}`);

  // Send confirmation to the group topic
  const statusText = status === "completed" ? "Going for it" : "Skipping today";
  const message = `<b>${escapeHtml(firstName)}</b>: ${statusText}`;
  await sendTelegramMessage(env.TELEGRAM_BOT_TOKEN, chatId, topicId, message);

  // Compute progress and DM the user into their weekday topic
  try {
    const dmTopicId = await getOrCreateUserTopic(dbPath, accessToken, env.TELEGRAM_BOT_TOKEN, userId, weekday);
    const progress = await computeProgress(dbPath, accessToken, userId, scheduledDate, weekday, status);
    const html = buildProgressHTML(firstName, progress);
    await sendTelegramDocument(env.TELEGRAM_BOT_TOKEN, userId, html, `progress-${scheduledDate}.html`, dmTopicId);
    console.log(`Sent progress report to user ${userId} in topic ${dmTopicId}`);
  } catch (err) {
    console.error(`Failed to send progress report to ${userId}:`, err);
  }
}

// ── Progress calculation ─────────────────────────────────────────────

async function computeProgress(dbPath, accessToken, userId, todayStr, todayWeekday, todayStatus) {
  // Fetch all checkins for this user
  const checkins = await firestoreList(dbPath, accessToken, `users/${userId}/checkins`);
  const checkinMap = {};
  const checkinWeekdays = {};
  for (const doc of checkins) {
    const dateVal = doc.fields?.date?.stringValue;
    const statusVal = doc.fields?.status?.stringValue;
    const wkday = doc.fields?.weekday?.stringValue;
    if (dateVal && statusVal) {
      checkinMap[dateVal] = statusVal;
      if (wkday) checkinWeekdays[dateVal] = wkday;
    }
  }

  // Check for cycle config
  const cycleDoc = await firestoreGet(dbPath, accessToken, "settings/routine_cycle");
  let cycleConfig = null;
  if (cycleDoc && cycleDoc.fields?.enabled?.booleanValue) {
    cycleConfig = parseCycleConfig(cycleDoc.fields);
  }

  // Fetch all routine tasks
  const tasks = await firestoreList(dbPath, accessToken, "routine_tasks");
  const scheduledWeekdays = new Set();
  const tasksByWeekday = {};
  const tasksByCycleDay = {};

  for (const task of tasks) {
    const title = task.fields?.title?.stringValue || "";
    const details = task.fields?.details?.stringValue || "";
    const cycleDayVal = task.fields?.cycle_day?.integerValue;

    if (cycleDayVal !== undefined) {
      const cd = Number(cycleDayVal);
      if (!tasksByCycleDay[cd]) tasksByCycleDay[cd] = [];
      tasksByCycleDay[cd].push({ title, details, order: Number(task.fields?.order?.integerValue || 0) });
    }

    const idx = task.fields?.weekday_index?.integerValue;
    const wkday = task.fields?.weekday?.stringValue;
    if (idx !== undefined) {
      scheduledWeekdays.add(Number(idx));
    }
    if (wkday) {
      if (!tasksByWeekday[wkday]) tasksByWeekday[wkday] = [];
      tasksByWeekday[wkday].push({ title, details });
    }
  }

  // Sort cycle day tasks by order
  for (const cd of Object.keys(tasksByCycleDay)) {
    tasksByCycleDay[cd].sort((a, b) => a.order - b.order);
  }

  // If cycle config is active, override scheduledWeekdays and todayTasks
  let todayTasks;
  let todayDayType = "gym";
  if (cycleConfig) {
    scheduledWeekdays.clear();
    for (const idx of cycleConfig.gymWeekdayIndices) scheduledWeekdays.add(idx);
    for (const idx of cycleConfig.runningWeekdayIndices) scheduledWeekdays.add(idx);

    const todayDate = new Date(todayStr + "T00:00:00Z");
    const { cycleDayIndex, dayType } = getCycleDayInfo(cycleConfig, todayDate);
    todayDayType = dayType;

    if (dayType === "running") {
      todayTasks = [{ title: "Running / Cardio", details: "" }];
    } else if (dayType === "gym" && cycleDayIndex !== null) {
      todayTasks = tasksByCycleDay[cycleDayIndex] || [];
    } else {
      todayTasks = [];
    }
  } else {
    todayTasks = tasksByWeekday[todayWeekday] || [];
  }

  // Fetch user's joined_on date; fall back to earliest checkin if missing
  const userDoc = await firestoreGet(dbPath, accessToken, `users/${userId}`);
  let joinedOnStr = userDoc?.fields?.joined_on?.stringValue;
  if (!joinedOnStr) {
    const allDates = Object.keys(checkinMap).sort();
    joinedOnStr = allDates.length > 0 ? allDates[0] : todayStr;
  }

  const streaks = calculateStreaks(joinedOnStr, scheduledWeekdays, checkinMap, todayStr);

  // Build history with weekday names and task titles
  const history = buildHistory(joinedOnStr, scheduledWeekdays, checkinMap, checkinWeekdays, tasksByWeekday, todayStr, cycleConfig, tasksByCycleDay);

  return {
    ...streaks,
    todayWeekday,
    todayStatus,
    todayTasks,
    history,
    setsRepsInfo: (cycleConfig && todayDayType === 'gym') ? cycleConfig.setsRepsInfo : '',
    globalSetsRepsInfo: cycleConfig ? cycleConfig.setsRepsInfo : '',
  };
}

// ── Cycle config parsing ─────────────────────────────────────────────

function parseCycleConfig(fields) {
  const cycleStartDate = fields.cycle_start_date?.stringValue;
  const numCycleDays = Number(fields.num_cycle_days?.integerValue || 3);

  const gymWeekdayIndices = (fields.gym_weekday_indices?.arrayValue?.values || [])
    .map((v) => Number(v.integerValue));
  const runningWeekdayIndices = (fields.running_weekday_indices?.arrayValue?.values || [])
    .map((v) => Number(v.integerValue));
  const restWeekdayIndices = (fields.rest_weekday_indices?.arrayValue?.values || [])
    .map((v) => Number(v.integerValue));

  return { cycleStartDate, numCycleDays, gymWeekdayIndices, runningWeekdayIndices, restWeekdayIndices, setsRepsInfo: fields.sets_reps_info?.stringValue || '' };
}

function getCycleDayInfo(cycleConfig, targetDate) {
  // targetDate is a JS Date in UTC
  const weekday = (targetDate.getUTCDay() + 6) % 7; // Mon=0 ... Sun=6

  if (cycleConfig.restWeekdayIndices.includes(weekday)) {
    return { cycleDayIndex: null, dayType: "rest" };
  }
  if (cycleConfig.runningWeekdayIndices.includes(weekday)) {
    return { cycleDayIndex: null, dayType: "running" };
  }
  if (!cycleConfig.gymWeekdayIndices.includes(weekday)) {
    return { cycleDayIndex: null, dayType: "rest" };
  }

  const startDate = new Date(cycleConfig.cycleStartDate + "T00:00:00Z");
  const daysElapsed = Math.round((targetDate - startDate) / (1000 * 60 * 60 * 24));
  const weekNumber = Math.floor(daysElapsed / 7);
  const gymSlotsSorted = [...cycleConfig.gymWeekdayIndices].sort((a, b) => a - b);
  const positionInWeek = gymSlotsSorted.indexOf(weekday);
  const gymSlotIndex = weekNumber * gymSlotsSorted.length + positionInWeek;
  const cycleDay = ((gymSlotIndex % cycleConfig.numCycleDays) + cycleConfig.numCycleDays) % cycleConfig.numCycleDays;

  return { cycleDayIndex: cycleDay, dayType: "gym" };
}

function buildHistory(joinedOnStr, scheduledWeekdays, checkins, checkinWeekdays, tasksByWeekday, todayStr, cycleConfig, tasksByCycleDay) {
  const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const DAY_LABELS = ["Day 1", "Day 2", "Day 3"];
  const joinedOn = new Date(joinedOnStr + "T00:00:00Z");
  const today = new Date(todayStr + "T00:00:00Z");
  const history = [];

  const current = new Date(joinedOn);
  while (current <= today) {
    const dayOfWeek = (current.getUTCDay() + 6) % 7;
    const key = current.toISOString().slice(0, 10);
    const weekdayName = WEEKDAY_NAMES[dayOfWeek];

    // Rest day indices (from cycle or Sunday)
    if (cycleConfig && cycleConfig.restWeekdayIndices.includes(dayOfWeek)) {
      if (key <= todayStr) {
        history.push({ date: key, weekday: weekdayName, status: "rest", dayLabel: "Rest", exercises: [] });
      }
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }
    if (!cycleConfig && dayOfWeek === REST_DAY_INDEX) {
      if (key <= todayStr) {
        history.push({ date: key, weekday: weekdayName, status: "rest", dayLabel: "Rest", exercises: [] });
      }
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }

    if (!scheduledWeekdays.has(dayOfWeek)) {
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }

    let status = checkins[key] || null;

    if (key === todayStr && !status) {
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }
    if (key < todayStr && !status) {
      status = "missed";
    }
    if (status) {
      let dayLabel = "";
      let exercises = [];

      if (cycleConfig) {
        if (cycleConfig.runningWeekdayIndices.includes(dayOfWeek)) {
          dayLabel = "Running";
          exercises = [{ title: "Running / Cardio" }];
        } else {
          const { cycleDayIndex } = getCycleDayInfo(cycleConfig, current);
          if (cycleDayIndex !== null) {
            dayLabel = DAY_LABELS[cycleDayIndex] || `Day ${cycleDayIndex + 1}`;
            exercises = (tasksByCycleDay[cycleDayIndex] || []).map((t) => ({ title: t.title }));
          }
        }
      } else {
        const dayTasks = tasksByWeekday[weekdayName] || [];
        dayLabel = dayTasks.map((t) => t.title).join(", ") || "—";
        exercises = dayTasks.map((t) => ({ title: t.title }));
      }

      history.push({
        date: key,
        weekday: weekdayName,
        status,
        dayLabel,
        exercises,
      });
    }

    current.setUTCDate(current.getUTCDate() + 1);
  }

  return history.reverse(); // most recent first
}

function calculateStreaks(joinedOnStr, scheduledWeekdays, checkins, todayStr) {
  if (scheduledWeekdays.size === 0) {
    return { currentStreak: 0, longestStreak: 0, totalCompleted: 0, totalMissed: 0, completionRate: 0 };
  }

  const joinedOn = new Date(joinedOnStr + "T00:00:00Z");
  const today = new Date(todayStr + "T00:00:00Z");
  const resolved = []; // [{date, status}]

  const current = new Date(joinedOn);
  while (current <= today) {
    const dayOfWeek = (current.getUTCDay() + 6) % 7; // Mon=0 ... Sun=6
    // Skip rest day (Sunday) — never counts for or against streaks
    if (dayOfWeek === REST_DAY_INDEX) {
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }
    if (!scheduledWeekdays.has(dayOfWeek)) {
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }

    const key = current.toISOString().slice(0, 10);
    let status = checkins[key] || null;

    if (key === todayStr && !status) {
      current.setUTCDate(current.getUTCDate() + 1);
      continue;
    }
    if (key < todayStr && !status) {
      status = "missed";
    }
    if (status) {
      resolved.push({ date: key, status });
    }

    current.setUTCDate(current.getUTCDate() + 1);
  }

  const totalCompleted = resolved.filter((r) => r.status === "completed").length;
  const totalMissed = resolved.length - totalCompleted;

  let currentStreak = 0;
  for (let i = resolved.length - 1; i >= 0; i--) {
    if (resolved[i].status === "completed") currentStreak++;
    else break;
  }

  let longestStreak = 0;
  let running = 0;
  for (const r of resolved) {
    if (r.status === "completed") {
      running++;
      if (running > longestStreak) longestStreak = running;
    } else {
      running = 0;
    }
  }

  const completionRate = resolved.length > 0 ? Math.round((totalCompleted / resolved.length) * 100 * 100) / 100 : 0;

  return { currentStreak, longestStreak, totalCompleted, totalMissed, completionRate };
}

function buildProgressHTML(firstName, stats) {
  const h = escapeHtml;
  const progressPct = stats.completionRate;
  const circumference = 2 * Math.PI * 54;
  const offset = circumference - (progressPct / 100) * circumference;

  const todayTasksHtml = stats.todayTasks.length > 0
    ? (stats.setsRepsInfo ? `<div class="today-sets-reps">${h(stats.setsRepsInfo)}</div>` : '') +
      stats.todayTasks.map((t) =>
        `<div class="today-task">
          <span class="task-name">${h(t.title)}</span>
          ${t.details ? `<span class="task-detail">${h(t.details)}</span>` : ""}
        </div>`
      ).join("")
    : `<div class="today-task"><span class="task-name">Rest Day</span></div>`;

  const todayStatusBadge = stats.todayStatus === "completed"
    ? `<span class="badge badge-done">Going for it</span>`
    : `<span class="badge badge-skip">Skipping</span>`;

  const historyHtml = stats.history.map((entry, idx) => {
    const d = new Date(entry.date + "T00:00:00Z");
    const dateStr = d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    const icon = entry.status === "completed" ? "check" : entry.status === "skipped" ? "skip" : entry.status === "rest" ? "rest" : "miss";
    const hasExercises = entry.exercises && entry.exercises.length > 0 && entry.dayLabel !== "Rest";
    const clickAttr = hasExercises ? `onclick="showPopup(${idx})" class="cell-workout clickable"` : `class="cell-workout"`;
    return `<tr class="row-${icon}">
      <td class="cell-date">
        <span class="date-day">${dateStr}</span>
        <span class="date-weekday">${h(entry.weekday)}</span>
      </td>
      <td ${clickAttr}>${h(entry.dayLabel || '—')}</td>
      <td class="cell-status"><span class="status-icon status-${icon}"></span></td>
    </tr>`;
  }).join("");

  // Build popup data as JSON for the script
  const popupData = stats.history.map((entry) => ({
    label: entry.dayLabel || '',
    weekday: entry.weekday,
    date: entry.date,
    exercises: (entry.exercises || []).map((e) => e.title),
    setsReps: (entry.dayLabel && entry.dayLabel !== 'Rest' && entry.dayLabel !== 'Running') ? (stats.globalSetsRepsInfo || '') : '',
  }));

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Progress Report – ${h(firstName)}</title>
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #13131a;
    --surface-2: #1a1a24;
    --border: #25253a;
    --text: #e4e4ed;
    --text-dim: #8888a0;
    --accent: #6c5ce7;
    --accent-glow: rgba(108, 92, 231, 0.15);
    --green: #00d26a;
    --green-dim: rgba(0, 210, 106, 0.12);
    --red: #ff4757;
    --red-dim: rgba(255, 71, 87, 0.12);
    --yellow: #ffa502;
    --yellow-dim: rgba(255, 165, 2, 0.12);
    --radius: 16px;
    --radius-sm: 10px;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 0;
    -webkit-font-smoothing: antialiased;
  }

  .container {
    max-width: 480px;
    margin: 0 auto;
    padding: 24px 16px 40px;
  }

  /* Header */
  .header {
    text-align: center;
    padding: 32px 0 8px;
  }
  .header h1 {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: var(--text);
  }
  .header .subtitle {
    font-size: 13px;
    color: var(--text-dim);
    margin-top: 4px;
  }

  /* Progress Ring */
  .ring-section {
    display: flex;
    justify-content: center;
    padding: 28px 0 20px;
  }
  .ring-wrap {
    position: relative;
    width: 140px;
    height: 140px;
  }
  .ring-wrap svg {
    transform: rotate(-90deg);
    width: 140px;
    height: 140px;
  }
  .ring-bg { stroke: var(--surface-2); }
  .ring-fill {
    stroke: var(--accent);
    stroke-linecap: round;
    transition: stroke-dashoffset 1s ease;
  }
  .ring-label {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .ring-pct {
    font-size: 32px;
    font-weight: 800;
    letter-spacing: -1px;
    line-height: 1;
  }
  .ring-sub {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
  }

  /* Stat Cards */
  .stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 20px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 16px;
    text-align: center;
  }
  .stat-value {
    font-size: 28px;
    font-weight: 800;
    letter-spacing: -0.5px;
    line-height: 1;
  }
  .stat-value.accent { color: var(--accent); }
  .stat-value.green { color: var(--green); }
  .stat-label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-top: 6px;
  }

  /* Today Section */
  .section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
  }
  .section-title {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .today-task {
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .today-task:last-child { border-bottom: none; }
  .task-name {
    font-size: 15px;
    font-weight: 600;
  }
  .task-detail {
    font-size: 13px;
    color: var(--text-dim);
  }
  .today-sets-reps {
    font-size: 13px;
    color: var(--green);
    font-weight: 600;
    padding: 6px 10px;
    margin-bottom: 8px;
    background: var(--green-dim);
    border-radius: 8px;
    text-align: center;
  }

  .badge {
    font-size: 11px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 20px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .badge-done { background: var(--green-dim); color: var(--green); }
  .badge-skip { background: var(--yellow-dim); color: var(--yellow); }

  /* History Table */
  .history-table {
    width: 100%;
    border-collapse: collapse;
  }
  .history-table tr {
    border-bottom: 1px solid var(--border);
  }
  .history-table tr:last-child { border-bottom: none; }
  .history-table td {
    padding: 12px 0;
    vertical-align: middle;
  }
  .cell-date {
    display: flex;
    flex-direction: column;
    gap: 1px;
    width: 110px;
  }
  .date-day {
    font-size: 14px;
    font-weight: 600;
  }
  .date-weekday {
    font-size: 11px;
    color: var(--text-dim);
  }
  .cell-workout {
    font-size: 13px;
    color: var(--text-dim);
    padding-left: 8px;
    padding-right: 8px;
  }
  .cell-status {
    text-align: right;
    width: 36px;
  }
  .status-icon {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
  }
  .status-check { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-skip { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .status-miss { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .status-rest { background: #8888a0; box-shadow: 0 0 6px #8888a0; }

  .legend {
    display: flex;
    gap: 16px;
    justify-content: center;
    padding: 12px 0 0;
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--text-dim);
  }
  .legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .footer {
    text-align: center;
    padding: 28px 0 8px;
    font-size: 12px;
    color: var(--text-dim);
  }
  .footer span { color: var(--accent); font-weight: 600; }

  /* Clickable workout cells */
  .cell-workout.clickable {
    cursor: pointer;
    color: var(--accent);
    font-weight: 600;
    transition: color 0.15s;
  }
  .cell-workout.clickable:hover { color: #8b7cf7; }

  /* Popup overlay */
  .popup-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(4px);
    z-index: 100;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .popup-overlay.active { display: flex; }
  .popup-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    max-width: 380px;
    width: 100%;
    position: relative;
    animation: popIn 0.2s ease;
  }
  @keyframes popIn { from { transform: scale(0.92); opacity: 0; } to { transform: scale(1); opacity: 1; } }
  .popup-close {
    position: absolute;
    top: 12px;
    right: 16px;
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: 22px;
    cursor: pointer;
    line-height: 1;
    padding: 4px;
  }
  .popup-close:hover { color: var(--text); }
  .popup-title {
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 4px;
    color: var(--accent);
  }
  .popup-subtitle {
    font-size: 12px;
    color: var(--text-dim);
    margin-bottom: 16px;
  }
  .popup-exercise {
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .popup-exercise:last-child { border-bottom: none; }
  .popup-exercise .ex-num {
    color: var(--accent);
    font-size: 12px;
    font-weight: 700;
    min-width: 22px;
  }
  .popup-sets-reps {
    font-size: 13px;
    color: var(--green);
    font-weight: 600;
    padding: 8px 12px;
    margin-bottom: 12px;
    background: var(--green-dim);
    border-radius: var(--radius-sm);
    text-align: center;
  }
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>${h(firstName)}'s Progress</h1>
    <div class="subtitle">Gym Buddy Report</div>
  </div>

  <div class="ring-section">
    <div class="ring-wrap">
      <svg viewBox="0 0 120 120">
        <circle class="ring-bg" cx="60" cy="60" r="54" fill="none" stroke-width="8"/>
        <circle class="ring-fill" cx="60" cy="60" r="54" fill="none" stroke-width="8"
          stroke-dasharray="${circumference.toFixed(2)}"
          stroke-dashoffset="${offset.toFixed(2)}"/>
      </svg>
      <div class="ring-label">
        <span class="ring-pct">${Math.round(progressPct)}%</span>
        <span class="ring-sub">Complete</span>
      </div>
    </div>
  </div>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-value accent">${stats.currentStreak}</div>
      <div class="stat-label">Current Streak</div>
    </div>
    <div class="stat-card">
      <div class="stat-value green">${stats.longestStreak}</div>
      <div class="stat-label">Longest Streak</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">
      <span>Today — ${h(stats.todayWeekday)}</span>
      ${todayStatusBadge}
    </div>
    ${todayTasksHtml}
  </div>

  <div class="section">
    <div class="section-title"><span>Workout History</span></div>
    <table class="history-table">
      ${historyHtml}
    </table>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:var(--green)"></div> Completed</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--yellow)"></div> Skipped</div>
      <div class="legend-item"><div class="legend-dot" style="background:var(--red)"></div> Missed</div>
      <div class="legend-item"><div class="legend-dot" style="background:#8888a0"></div> Rest</div>
    </div>
  </div>

  <div class="footer">Powered by <span>Gym Buddy</span></div>

</div>

<div class="popup-overlay" id="popupOverlay" onclick="if(event.target===this)closePopup()">
  <div class="popup-card">
    <button class="popup-close" onclick="closePopup()">&times;</button>
    <div class="popup-title" id="popupTitle"></div>
    <div class="popup-subtitle" id="popupSubtitle"></div>
    <div id="popupExercises"></div>
  </div>
</div>

<script>
  const _pd = ${JSON.stringify(popupData)};
  function showPopup(idx) {
    const d = _pd[idx];
    if (!d || !d.exercises.length) return;
    document.getElementById('popupTitle').textContent = d.label;
    document.getElementById('popupSubtitle').textContent = d.weekday + ' \\u2022 ' + d.date;
    const container = document.getElementById('popupExercises');
    let html = '';
    if (d.setsReps) {
      html += '<div class="popup-sets-reps">' + d.setsReps.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>';
    }
    html += d.exercises.map(function(ex, i) {
      return '<div class="popup-exercise"><span class="ex-num">' + (i + 1) + '.</span> ' + ex.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>';
    }).join('');
    container.innerHTML = html;
    document.getElementById('popupOverlay').classList.add('active');
  }
  function closePopup() {
    document.getElementById('popupOverlay').classList.remove('active');
  }
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closePopup(); });
</script>

</body>
</html>`;
}

// ── Firebase / Google auth ───────────────────────────────────────────

function parseServiceAccount(base64Encoded) {
  return JSON.parse(atob(base64Encoded));
}

function firestoreBasePath(projectId, databaseId) {
  const db = databaseId || "(default)";
  return `https://firestore.googleapis.com/v1/projects/${projectId}/databases/${db}/documents`;
}

async function getAccessToken(sa) {
  const now = Math.floor(Date.now() / 1000);

  const header = base64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = base64url(
    JSON.stringify({
      iss: sa.client_email,
      scope: "https://www.googleapis.com/auth/datastore",
      aud: "https://oauth2.googleapis.com/token",
      iat: now,
      exp: now + 3600,
    })
  );

  const signInput = `${header}.${payload}`;

  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToArrayBuffer(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );

  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(signInput)
  );

  const jwt = `${signInput}.${base64url(sig)}`;

  const resp = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=${jwt}`,
  });

  const data = await resp.json();
  if (!data.access_token) {
    throw new Error(`Google auth failed: ${JSON.stringify(data)}`);
  }
  return data.access_token;
}

// ── Firestore REST helpers ───────────────────────────────────────────

async function firestoreGet(basePath, token, docPath) {
  const resp = await fetch(`${basePath}/${docPath}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`Firestore GET ${resp.status}: ${await resp.text()}`);
  return resp.json();
}

async function firestorePatch(basePath, token, docPath, body) {
  const masks = Object.keys(body.fields)
    .map((f) => `updateMask.fieldPaths=${f}`)
    .join("&");

  const resp = await fetch(`${basePath}/${docPath}?${masks}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`Firestore PATCH ${resp.status}: ${await resp.text()}`);
  return resp.json();
}

async function firestoreList(basePath, token, collectionPath) {
  const docs = [];
  let pageToken = null;

  while (true) {
    let url = `${basePath}/${collectionPath}?pageSize=300`;
    if (pageToken) url += `&pageToken=${pageToken}`;

    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(`Firestore LIST ${resp.status}: ${await resp.text()}`);

    const data = await resp.json();
    if (data.documents) docs.push(...data.documents);
    if (!data.nextPageToken) break;
    pageToken = data.nextPageToken;
  }

  return docs;
}

// ── Telegram helpers ─────────────────────────────────────────────────

async function sendTelegramMessage(botToken, chatId, topicId, text) {
  const resp = await fetch(
    `https://api.telegram.org/bot${botToken}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        message_thread_id: Number(topicId),
        text,
        parse_mode: "HTML",
      }),
    }
  );
  if (!resp.ok) {
    console.error("Telegram sendMessage failed:", await resp.text());
  }
}

async function sendTelegramDM(botToken, userId, text) {
  const resp = await fetch(
    `https://api.telegram.org/bot${botToken}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: userId,
        text,
        parse_mode: "HTML",
      }),
    }
  );
  if (!resp.ok) {
    console.error(`Telegram DM to ${userId} failed:`, await resp.text());
  }
}

async function getOrCreateUserTopic(dbPath, accessToken, botToken, userId, weekday) {
  // Read stored topic id from Firestore
  const userDoc = await firestoreGet(dbPath, accessToken, `users/${userId}`);
  const storedTopicId = userDoc?.fields?.weekday_topics?.mapValue?.fields?.[weekday]?.integerValue;
  if (storedTopicId) return Number(storedTopicId);

  // Create a new forum topic in the user's private chat
  const resp = await fetch(
    `https://api.telegram.org/bot${botToken}/createForumTopic`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: userId, name: weekday }),
    }
  );
  if (!resp.ok) {
    console.error(`createForumTopic for user ${userId} weekday ${weekday} failed:`, await resp.text());
    return null;
  }
  const data = await resp.json();
  const topicId = data.result?.message_thread_id;
  if (!topicId) return null;

  // Persist it in Firestore under weekday_topics.<weekday> using dot-notation field mask
  const patchResp = await fetch(
    `${dbPath}/users/${userId}?updateMask.fieldPaths=${encodeURIComponent(`weekday_topics.${weekday}`)}`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        fields: {
          weekday_topics: {
            mapValue: {
              fields: {
                [weekday]: { integerValue: String(topicId) },
              },
            },
          },
        },
      }),
    }
  );
  if (!patchResp.ok) {
    console.error(`Failed to persist topic_id for user ${userId}:`, await patchResp.text());
  }
  return topicId;
}

async function sendTelegramDocument(botToken, userId, htmlContent, filename, threadId = null) {
  const boundary = "----FormBoundary" + Date.now().toString(36);
  const encoder = new TextEncoder();
  const fileBytes = encoder.encode(htmlContent);

  const parts = [
    `--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${userId}`,
    `--${boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\nYour progress report is ready. Open the file to view.`,
  ];
  if (threadId) {
    parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="message_thread_id"\r\n\r\n${threadId}`);
  }
  parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="document"; filename="${filename}"\r\nContent-Type: text/html\r\n\r\n`);

  const before = encoder.encode(parts.join("\r\n") + "\r\n");
  const after = encoder.encode(`\r\n--${boundary}--\r\n`);

  const body = new Uint8Array(before.length + fileBytes.length + after.length);
  body.set(before, 0);
  body.set(fileBytes, before.length);
  body.set(after, before.length + fileBytes.length);

  const resp = await fetch(
    `https://api.telegram.org/bot${botToken}/sendDocument`,
    {
      method: "POST",
      headers: {
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
      },
      body: body.buffer,
    }
  );
  if (!resp.ok) {
    console.error(`Telegram sendDocument to ${userId} failed:`, await resp.text());
  }
}

// ── Encoding utilities ───────────────────────────────────────────────

function base64url(input) {
  const str =
    typeof input === "string"
      ? btoa(input)
      : btoa(String.fromCharCode(...new Uint8Array(input)));
  return str.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToArrayBuffer(pem) {
  const b64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s/g, "");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
