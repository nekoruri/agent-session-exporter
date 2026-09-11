import assert from "node:assert/strict";
import test from "node:test";

import worker, { MAX_BODY_BYTES } from "../src/index.js";
import { redactValue } from "../src/redaction.js";

class FakeStatement {
  constructor(database, sql) {
    this.database = database;
    this.sql = sql;
    this.values = [];
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  async run() {
    assert.match(this.sql, /INSERT OR IGNORE INTO events/);
    const [
      fingerprint,
      source,
      deviceId,
      sessionId,
      eventName,
      occurredAt,
      cwd,
      project,
      repository,
      branch,
      transcriptPath,
      payloadJson,
      receivedAt,
    ] = this.values;
    if (this.database.rows.some((row) => row.fingerprint === fingerprint)) {
      return { meta: { changes: 0 } };
    }
    this.database.rows.push({
      id: this.database.nextId,
      fingerprint,
      source,
      device_id: deviceId,
      session_id: sessionId,
      event_name: eventName,
      occurred_at: occurredAt,
      cwd,
      project,
      repository,
      branch,
      transcript_path: transcriptPath,
      payload_json: payloadJson,
      received_at: receivedAt,
    });
    this.database.nextId += 1;
    return { meta: { changes: 1 } };
  }

  async all() {
    assert.match(this.sql, /FROM events WHERE id > \?/);
    const [after, limit] = this.values;
    return {
      results: this.database.rows
        .filter((row) => row.id > after)
        .slice(0, limit),
    };
  }
}

class FakeD1 {
  constructor() {
    this.rows = [];
    this.nextId = 1;
  }

  prepare(sql) {
    return new FakeStatement(this, sql);
  }
}

function environment(overrides = {}) {
  return {
    DB: new FakeD1(),
    DEVICE_ID: "claude-cloud",
    INGEST_TOKEN: "ingest-secret",
    PULL_TOKEN: "pull-secret",
    ...overrides,
  };
}

function hookRequest(payload, token = "ingest-secret", remote = "true") {
  return new Request("https://inbox.example/v1/hooks/claude-cloud", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Claude-Code-Remote": remote,
    },
    body: JSON.stringify(payload),
  });
}

function sampleEvent(overrides = {}) {
  return {
    session_id: "session-1",
    hook_event_name: "userpromptsubmit",
    timestamp: "2026-08-01T00:00:00.000Z",
    cwd: "/workspace/example",
    prompt: "Hello",
    ...overrides,
  };
}

test("library detection masks complete credentials before D1 storage without network calls", async (t) => {
  t.mock.method(globalThis, "fetch", () => {
    throw new Error("credential detection must stay offline");
  });
  const env = environment();
  const github = "ghp_" + "a".repeat(36);
  const openai = "sk-" + "a".repeat(20) + "T3BlbkFJ" + "b".repeat(20);
  const payload = sampleEvent({
    prompt: `Keep this text. ${github} ${openai}`,
    repository: "https://user:p%40ss!word@example.invalid/repo.git?token=short&view=1",
    nested: [{ privateKey: "custom-format", accessToken: "short" }],
  });
  for (let i = 0; i < 2; i += 1) {
    assert.equal((await worker.fetch(hookRequest(payload), env)).status, 202);
  }
  assert.equal(env.DB.rows.length, 1);
  const stored = JSON.stringify(env.DB.rows);
  for (const secret of [github, "a".repeat(36), openai, "p%40ss", "custom-format", "token=short"]) {
    assert.equal(stored.includes(secret), false);
  }
  const saved = JSON.parse(env.DB.rows[0].payload_json);
  assert.match(saved.prompt, /^Keep this text\. \[REDACTED\] \[REDACTED\]$/);
  assert.deepEqual(saved.nested, [{ privateKey: "[REDACTED]", accessToken: "[REDACTED]" }]);
  assert.match(saved.repository, /view=1/);
  assert.equal(globalThis.fetch.mock.callCount(), 0);
});

test("conversation comments cannot disable Secretlint", async () => {
  const github = "ghp_" + "a".repeat(36);
  for (const comment of ["// secretlint-disable", "<!-- secretlint-disable -->"]) {
    const masked = await redactValue(`${comment}\n${github}`);
    assert.equal(masked.includes(github), false);
    assert.equal(masked.includes("a".repeat(36)), false);
  }
});

test("private key bodies and authorization schemes are fully masked", async () => {
  const body = "MI" + "A".repeat(128);
  const pem = `-----BEGIN PRIVATE KEY-----\n${body}\n-----END PRIVATE KEY-----`;
  const result = await redactValue({ message: pem, headers: ["Bearer short", "Basic dXNlcjpwYXNz"] });
  assert.equal(result.message, "[REDACTED]");
  assert.deepEqual(result.headers, ["[REDACTED]", "[REDACTED]"]);
});

test("ordinary text and JSON types survive masking, which is idempotent", async () => {
  const input = {
    text: "Please fix issue 123. See https://example.invalid/docs.",
    count: 3,
    done: false,
    nested: [null, "203.0.113.1"],
  };
  assert.deepEqual(await redactValue(input), input);
  const masked = await redactValue({ prompt: "ghp_" + "a".repeat(36) });
  assert.deepEqual(await redactValue(masked), masked);
});

test("batched scanning preserves field boundaries, Unicode, multiline keys and large payloads", async () => {
  const github = "ghp_" + "a".repeat(36);
  const openai = "sk-" + "a".repeat(20) + "T3BlbkFJ" + "b".repeat(20);
  const pem = "-----BEGIN PRIVATE KEY-----\nMI" + "A".repeat(128) + "\n-----END PRIVATE KEY-----";
  const input = {
    fields: ["", "🙂 日本語\n" + github, openai, pem, "ordinary\ntext", ""],
    splitPem: ["before", ...pem.split("\n"), "after"],
    password: { ignored: github },
    entries: Array(10000).fill("ordinary text"),
    tail: `${github} and ${openai}`,
  };
  const output = await redactValue(input);
  assert.deepEqual(output.fields, ["", "🙂 日本語\n[REDACTED]", "[REDACTED]", "[REDACTED]", "ordinary\ntext", ""]);
  assert.equal(output.password, "[REDACTED]");
  assert.deepEqual(output.splitPem, ["before", "[REDACTED]", "[REDACTED]", "[REDACTED]", "after"]);
  assert.deepEqual(output.entries, input.entries);
  assert.equal(output.tail, "[REDACTED] and [REDACTED]");
  assert.deepEqual(await redactValue(output), output);
  assert.equal(input.fields[1], "🙂 日本語\n" + github);
  assert.deepEqual(await redactValue([null, false, 42, { password: "short" }]),
    [null, false, 42, { password: "[REDACTED]" }]);
});

test("detected session identifiers remain distinct and stable in D1", async () => {
  const env = environment();
  const pseudonyms = new Set();
  for (const letter of ["a", "b"]) {
    const id = "ghp_" + letter.repeat(36);
    const payload = sampleEvent({ session_id: id });
    for (let repeat = 0; repeat < 2; repeat++) {
      assert.equal((await worker.fetch(hookRequest(payload), env)).status, 202);
    }
    const masked = await redactValue({ sessionId: id, task_id: id, deviceId: id });
    assert.equal(masked.sessionId, masked.task_id);
    assert.equal(masked.sessionId, masked.deviceId);
    assert.deepEqual(await redactValue(masked), masked);
    pseudonyms.add(masked.sessionId);
    assert.equal(JSON.stringify(env.DB.rows).includes(id), false);
  }
  assert.equal(env.DB.rows.length, 2);
  assert.equal(pseudonyms.size, 2);
  for (const row of env.DB.rows) {
    assert.ok(pseudonyms.has(row.session_id));
    assert.equal(JSON.parse(row.payload_json).session_id, row.session_id);
  }
});

test("health reports whether bindings and secrets are configured", async () => {
  const healthy = await worker.fetch(
    new Request("https://inbox.example/health"),
    environment(),
  );
  assert.equal(healthy.status, 200);
  assert.deepEqual(await healthy.json(), { status: "ok" });

  const unconfigured = await worker.fetch(
    new Request("https://inbox.example/health"),
    environment({ PULL_TOKEN: "" }),
  );
  assert.equal(unconfigured.status, 503);
});

test("local Claude hooks are ignored before authentication", async () => {
  const env = environment();
  const response = await worker.fetch(hookRequest(sampleEvent(), "", "false"), env);
  assert.equal(response.status, 204);
  assert.equal(env.DB.rows.length, 0);
});

test("ingest and pull tokens cannot be used interchangeably", async () => {
  const env = environment();
  const ingestWithPullToken = await worker.fetch(
    hookRequest(sampleEvent(), "pull-secret"),
    env,
  );
  assert.equal(ingestWithPullToken.status, 401);

  const pullWithIngestToken = await worker.fetch(
    new Request("https://inbox.example/v1/events", {
      headers: { Authorization: "Bearer ingest-secret" },
    }),
    env,
  );
  assert.equal(pullWithIngestToken.status, 401);
});

test("valid events are normalized, redacted, deduplicated, and pulled", async () => {
  const env = environment();
  const payload = sampleEvent({
    api_key: "secret-value",
    transcript_path: "/workspace/private.jsonl",
    note: "Bearer abcdefghijklmnop",
  });
  const first = await worker.fetch(hookRequest(payload), env);
  const duplicate = await worker.fetch(hookRequest(payload), env);
  assert.equal(first.status, 202);
  assert.equal(duplicate.status, 202);
  assert.equal(env.DB.rows.length, 1);

  const pull = await worker.fetch(
    new Request("https://inbox.example/v1/events?after=0&limit=10", {
      headers: { Authorization: "Bearer pull-secret" },
    }),
    env,
  );
  assert.equal(pull.status, 200);
  const result = await pull.json();
  assert.equal(result.next_after, 1);
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].event_name, "UserPromptSubmit");
  assert.equal(result.events[0].project, "example");
  assert.equal(result.events[0].transcript_path, "");
  assert.equal(result.events[0].payload.api_key, "[REDACTED]");
  assert.equal(result.events[0].payload.note, "[REDACTED]");
  assert.equal("transcript_path" in result.events[0].payload, false);
  assert.match(result.events[0].fingerprint, /^[a-f0-9]{64}$/);
});

test("unsupported events and oversized bodies are rejected", async () => {
  const env = environment();
  const unsupported = await worker.fetch(
    hookRequest(sampleEvent({ hook_event_name: "PreToolUse" })),
    env,
  );
  assert.equal(unsupported.status, 400);

  const oversized = new Request("https://inbox.example/v1/hooks/claude-cloud", {
    method: "POST",
    headers: {
      Authorization: "Bearer ingest-secret",
      "Content-Type": "application/json",
      "Content-Length": String(MAX_BODY_BYTES + 1),
      "X-Claude-Code-Remote": "true",
    },
    body: "{}",
  });
  const response = await worker.fetch(oversized, env);
  assert.equal(response.status, 413);
});
