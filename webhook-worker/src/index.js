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

// ── Telegram helper ──────────────────────────────────────────────────

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
