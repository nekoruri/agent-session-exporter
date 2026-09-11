import { redactValue } from "./redaction.js";

const MAX_BODY_BYTES = 1024 * 1024;
const MAX_PULL_LIMIT = 500;
const CLAUDE_CLOUD_EVENTS = new Map(
  [
    "UserPromptSubmit",
    "MessageDisplay",
    "Stop",
    "StopFailure",
    "SessionEnd",
  ].map((name) => [name.toLowerCase(), name]),
);

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function bearerToken(request) {
  const authorization = request.headers.get("Authorization") || "";
  return authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
}

function sameSecret(left, right) {
  if (!left || !right) return false;
  const encoder = new TextEncoder();
  const leftBytes = encoder.encode(left);
  const rightBytes = encoder.encode(right);
  let mismatch = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    mismatch |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return mismatch === 0;
}

function requireToken(request, expected) {
  if (!sameSecret(bearerToken(request), expected)) {
    throw new HttpError(401, "unauthorized");
  }
}

function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, stableValue(value[key])]),
    );
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(stableValue(value));
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function firstString(payload, keys) {
  for (const key of keys) {
    const value = payload[key];
    if (value !== undefined && value !== null && String(value).trim()) {
      return String(value);
    }
  }
  return "";
}

function singleWorkspaceRoot(payload) {
  const roots = payload.workspace_roots || payload.workspaceRoots;
  if (!Array.isArray(roots)) return "";
  const values = roots
    .filter((root) => root !== undefined && root !== null)
    .map((root) => String(root).trim())
    .filter(Boolean);
  return values.length === 1 ? values[0] : "";
}

function basename(path) {
  const parts = path.replace(/\\/g, "/").replace(/\/+$/, "").split("/");
  return parts.at(-1) || "unknown";
}

async function normalizeEvent(rawPayload, env) {
  if (
    !rawPayload ||
    Array.isArray(rawPayload) ||
    typeof rawPayload !== "object"
  ) {
    throw new HttpError(400, "request body must be a JSON object");
  }
  const payload = await redactValue(rawPayload);
  delete payload.transcript_path;
  delete payload.transcriptPath;

  const rawEventName = firstString(payload, [
    "hook_event_name",
    "event_name",
    "event",
    "type",
  ]);
  const eventName = CLAUDE_CLOUD_EVENTS.get(rawEventName.toLowerCase());
  if (!eventName) throw new HttpError(400, "unsupported Claude Cloud event");
  for (const eventKey of ["hook_event_name", "event_name", "event", "type"]) {
    if (String(payload[eventKey] || "").trim() === rawEventName) {
      payload[eventKey] = eventName;
      break;
    }
  }

  const sessionId = firstString(payload, ["session_id", "sessionId"]);
  if (!sessionId) throw new HttpError(400, "session_id is required");
  const occurredAt =
    firstString(payload, [
      "timestamp",
      "occurred_at",
      "created_at",
      "createdAt",
    ]) ||
    new Date().toISOString();
  const cwd =
    firstString(payload, ["cwd", "working_directory"]) ||
    singleWorkspaceRoot(payload);
  const envelope = {
    source: "claude-cloud",
    device_id: String(env.DEVICE_ID || "claude-cloud"),
    session_id: sessionId,
    event_name: eventName,
    occurred_at: occurredAt,
    cwd,
    project: String(payload.project || (cwd ? basename(cwd) : "unknown")),
    repository: String(payload.repository || ""),
    branch: String(payload.branch || ""),
    transcript_path: "",
    payload,
    received_at: new Date().toISOString(),
  };
  envelope.fingerprint = await sha256({
    source: envelope.source,
    device_id: envelope.device_id,
    session_id: envelope.session_id,
    event_name: envelope.event_name,
    occurred_at: envelope.occurred_at,
    payload: envelope.payload,
  });
  return envelope;
}

async function readJsonBody(request) {
  const contentType = request.headers.get("Content-Type") || "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    throw new HttpError(415, "content type must be application/json");
  }
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    throw new HttpError(413, "request body is too large");
  }
  if (!request.body) throw new HttpError(400, "request body is required");

  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new HttpError(413, "request body is too large");
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new HttpError(400, "request body is not valid JSON");
  }
}

async function insertEvent(db, event) {
  await db
    .prepare(
      `INSERT OR IGNORE INTO events (
        fingerprint, source, device_id, session_id, event_name,
        occurred_at, cwd, project, repository, branch,
        transcript_path, payload_json, received_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .bind(
      event.fingerprint,
      event.source,
      event.device_id,
      event.session_id,
      event.event_name,
      event.occurred_at,
      event.cwd,
      event.project,
      event.repository,
      event.branch,
      event.transcript_path,
      canonicalJson(event.payload),
      event.received_at,
    )
    .run();
}

function parseInteger(value, fallback, minimum, maximum) {
  if (value === null || value === "") return fallback;
  if (!/^\d+$/.test(value)) throw new HttpError(400, "invalid cursor or limit");
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < minimum || result > maximum) {
    throw new HttpError(400, "invalid cursor or limit");
  }
  return result;
}

async function listEvents(db, url) {
  const after = parseInteger(
    url.searchParams.get("after"),
    0,
    0,
    Number.MAX_SAFE_INTEGER,
  );
  const limit = parseInteger(
    url.searchParams.get("limit"),
    MAX_PULL_LIMIT,
    1,
    MAX_PULL_LIMIT,
  );
  const result = await db
    .prepare(
      `SELECT id, fingerprint, source, device_id, session_id, event_name,
        occurred_at, cwd, project, repository, branch, transcript_path,
        payload_json, received_at
       FROM events WHERE id > ? ORDER BY id LIMIT ?`,
    )
    .bind(after, limit)
    .all();
  const rows = result.results || [];
  const events = rows.map((row) => ({
    fingerprint: row.fingerprint,
    source: row.source,
    device_id: row.device_id,
    session_id: row.session_id,
    event_name: row.event_name,
    occurred_at: row.occurred_at,
    cwd: row.cwd,
    project: row.project,
    repository: row.repository,
    branch: row.branch,
    transcript_path: row.transcript_path,
    payload: JSON.parse(row.payload_json),
    received_at: row.received_at,
  }));
  return {
    events,
    next_after: rows.length ? Number(rows.at(-1).id) : after,
  };
}

async function handleRequest(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/health") {
    if (request.method !== "GET") throw new HttpError(405, "method not allowed");
    const configured = Boolean(env.DB && env.INGEST_TOKEN && env.PULL_TOKEN);
    return jsonResponse(
      { status: configured ? "ok" : "unconfigured" },
      configured ? 200 : 503,
    );
  }
  if (url.pathname === "/v1/hooks/claude-cloud") {
    if (request.method !== "POST") throw new HttpError(405, "method not allowed");
    if (request.headers.get("X-Claude-Code-Remote") !== "true") {
      return new Response(null, { status: 204 });
    }
    requireToken(request, env.INGEST_TOKEN);
    const payload = await readJsonBody(request);
    await insertEvent(env.DB, await normalizeEvent(payload, env));
    return new Response(null, { status: 202 });
  }
  if (url.pathname === "/v1/events") {
    if (request.method !== "GET") throw new HttpError(405, "method not allowed");
    requireToken(request, env.PULL_TOKEN);
    return jsonResponse(await listEvents(env.DB, url));
  }
  throw new HttpError(404, "not found");
}

export default {
  async fetch(request, env) {
    try {
      return await handleRequest(request, env);
    } catch (error) {
      if (error instanceof HttpError) {
        return jsonResponse({ error: error.message }, error.status);
      }
      console.error("Worker request failed", error);
      return jsonResponse({ error: "service unavailable" }, 503);
    }
  },
};

export { MAX_BODY_BYTES, handleRequest, normalizeEvent };
