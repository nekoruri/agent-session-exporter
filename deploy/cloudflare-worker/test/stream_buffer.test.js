import assert from "node:assert/strict";
import test from "node:test";
import { createHash, randomBytes } from "node:crypto";
import worker from "../src/index.js";
import { TestD1 } from "./database.js";

function keyring(previous = { keys: {} }) {
  const key = randomBytes(32);
  const active = createHash("sha256").update(key).digest("hex").slice(0, 16);
  return { active, keys: { ...previous.keys, [active]: key.toString("hex") } };
}
function environment() {
  return { DB: new TestD1(), INGEST_TOKEN: "ingest-secret", PULL_TOKEN: "pull-secret",
    BUFFER_ENCRYPTION_KEYS: JSON.stringify(keyring()) };
}
function send(env, index, delta, final = false, overrides = {}) {
  return worker.fetch(new Request("https://inbox.example/v1/hooks/claude-cloud", {
    method: "POST", headers: { "Content-Type": "application/json",
      Authorization: "Bearer ingest-secret", "X-Claude-Code-Remote": "true" },
    body: JSON.stringify({ session_id: "session-1", hook_event_name: "MessageDisplay",
      message_id: "message-1", index, delta, final, ...overrides }),
  }), { ...env }); // Each request has fresh bindings, with durable data as the only shared state.
}
function dump(db) {
  return JSON.stringify([db.bindings, db.rows, db.connection.prepare("SELECT * FROM message_chunks").all(),
    db.connection.prepare("SELECT * FROM message_receipts").all()]);
}

test("reordered chunks, empty final and retries publish only complete redacted messages", async () => {
  for (const chunks of [["ghp_", "a".repeat(36), ""],
    ["-----BEGIN PRIVATE KEY-----\n", "MI" + "A".repeat(128) + "\n", "-----END PRIVATE KEY-----"]]) {
    const env = environment();
    for (const index of [2, 1]) {
      assert.equal((await send(env, index, chunks[index], index === 2)).status, 202);
      assert.equal(env.DB.rows.length, 0);
      for (const part of chunks.filter(Boolean)) assert.equal(dump(env.DB).includes(part), false);
    }
    assert.equal((await send(env, 0, chunks[0])).status, 202);
    assert.equal(env.DB.rows.length, 1);
    assert.equal(JSON.parse(env.DB.rows[0].payload_json).delta, "[REDACTED]");
    for (const index of [1, 0, 2])
      assert.equal((await send(env, index, chunks[index], index === 2)).status, 202);
    assert.equal(env.DB.rows.length, 1);
    assert.equal(env.DB.connection.prepare("SELECT count(*) AS n FROM message_chunks").get().n, 0);
    for (const part of chunks.filter(Boolean)) assert.equal(dump(env.DB).includes(part), false);
    const pull = await worker.fetch(new Request("https://inbox.example/v1/events", {
      headers: { Authorization: "Bearer pull-secret" },
    }), env);
    assert.equal((await pull.json()).events[0].payload.delta, "[REDACTED]");
  }
});

test("concurrent duplicate delivery does not duplicate publication or lose chunks", async () => {
  const env = environment();
  const replies = await Promise.all([send(env, 0, "ghp_"), send(env, 0, "ghp_"),
    send(env, 1, "a".repeat(36)), send(env, 2, "", true)]);
  assert.ok(replies.every((response) => response.status === 202));
  assert.equal(env.DB.rows.length, 1);
  assert.equal(JSON.parse(env.DB.rows[0].payload_json).delta, "[REDACTED]");
});

test("key rotation retains pending data; missing keys and corruption fail closed", async (t) => {
  t.mock.method(console, "error", () => {});
  const env = environment();
  assert.equal((await send(env, 0, "ghp_")).status, 202);
  const original = env.BUFFER_ENCRYPTION_KEYS;
  env.BUFFER_ENCRYPTION_KEYS = "";
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 503);
  env.BUFFER_ENCRYPTION_KEYS = JSON.stringify(keyring());
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 503);
  // Restore every key used by pending chunks, then rotate without dropping any.
  const combined = { ...JSON.parse(original), keys: {
    ...JSON.parse(original).keys, ...JSON.parse(env.BUFFER_ENCRYPTION_KEYS).keys } };
  env.BUFFER_ENCRYPTION_KEYS = JSON.stringify(keyring(combined));
  const ciphertext = env.DB.connection.prepare("SELECT ciphertext FROM message_chunks WHERE idx = 0").get().ciphertext;
  env.DB.connection.prepare("UPDATE message_chunks SET ciphertext = 'broken' WHERE idx = 0").run();
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 503);
  assert.equal(env.DB.rows.length, 0);
  env.DB.connection.prepare("UPDATE message_chunks SET ciphertext = ? WHERE idx = 0").run(ciphertext);
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 202);
  assert.equal(JSON.parse(env.DB.rows[0].payload_json).delta, "[REDACTED]");
});

test("failed atomic publication can be retried without losing buffered data", async (t) => {
  t.mock.method(console, "error", () => {});
  const env = environment();
  await send(env, 0, "ghp_");
  env.DB.connection.exec(`CREATE TRIGGER injected_failure BEFORE INSERT ON message_receipts
    BEGIN SELECT RAISE(ABORT, 'injected failure'); END`);
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 503);
  assert.equal(env.DB.rows.length, 0);
  assert.equal(env.DB.connection.prepare("SELECT count(*) AS n FROM message_chunks").get().n, 2);
  env.DB.connection.exec("DROP TRIGGER injected_failure");
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 202);
  assert.equal(env.DB.rows.length, 1);
});

test("invalid fields and conflicting retries are rejected; ordinary Unicode text is preserved", async () => {
  const env = environment();
  for (const [index, final] of [[true, false], [-1, false], [4096, false], [0, "false"]])
    assert.equal((await send(env, index, "text", final)).status, 400);
  await send(env, 0, "こんにちは、");
  assert.equal((await send(env, 0, "changed")).status, 400);
  assert.equal((await send(env, 1, "世界🙂", true)).status, 202);
  assert.equal(JSON.parse(env.DB.rows[0].payload_json).delta, "こんにちは、世界🙂");
});

test("conflicting final markers do not poison the stream, and secret message IDs remain distinct", async () => {
  const env = environment();
  await send(env, 2, "", true);
  assert.equal((await send(env, 1, "wrong", true)).status, 400);
  assert.equal((await send(env, 3, "wrong")).status, 400);
  await send(env, 0, "ghp_");
  assert.equal((await send(env, 1, "a".repeat(36))).status, 202);
  assert.equal(JSON.parse(env.DB.rows[0].payload_json).delta, "[REDACTED]");
  for (const letter of ["a", "b"])
    await send(env, 0, "Hello " + letter, true, { message_id: "ghp_" + letter.repeat(36) });
  const ids = env.DB.rows.slice(1).map((row) => JSON.parse(row.payload_json).message_id);
  assert.equal(new Set(ids).size, 2);
  assert.ok(ids.every((id) => id.startsWith("redacted-")));
});

test("D1 staging quotas reject new chunks without deleting accepted data", async (t) => {
  t.mock.method(console, "error", () => {});
  const env = environment();
  await send(env, 0, "ghp_");
  env.DB.connection.prepare("UPDATE message_chunks SET ciphertext = ?").run("x".repeat(8 * 1024 * 1024));
  assert.equal((await send(env, 1, "a".repeat(36), true)).status, 503);
  assert.equal(env.DB.rows.length, 0);
  assert.equal(env.DB.connection.prepare("SELECT count(*) AS n FROM message_chunks").get().n, 1);
});
