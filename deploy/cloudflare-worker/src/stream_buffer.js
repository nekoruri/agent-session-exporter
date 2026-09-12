// Encrypt whole hook payloads until every batch is present. No plaintext staging in D1.
import { Buffer } from "node:buffer";
import { isDeepStrictEqual } from "node:util";

const MAX_CHUNKS = 4096;
const MAX_CHUNK_BYTES = 1024 * 1024;
const MAX_MESSAGE_BYTES = 8 * 1024 * 1024;
const MAX_BUFFER_BYTES = 64 * 1024 * 1024;
const encoder = new TextEncoder();

export class StreamError extends Error {}

async function digest(value) {
  return Buffer.from(await crypto.subtle.digest("SHA-256", value)).toString("hex");
}

export async function bufferKeys(text) {
  try {
    const value = JSON.parse(text);
    if (!value.keys || !Object.hasOwn(value.keys, value.active)) throw new Error();
    for (const [id, key] of Object.entries(value.keys)) {
      if (typeof key !== "string" || !/^[a-f0-9]{64}$/.test(key)
          || (await digest(Buffer.from(key, "hex"))).slice(0, 16) !== id) throw new Error();
    }
    return value;
  } catch {
    throw new Error("Buffer encryption keys are missing or invalid");
  }
}

function fields(payload) {
  const messageId = payload.message_id ?? payload.messageId;
  const { index, final } = payload;
  const delta = payload.delta ?? payload.text ?? payload.message ?? "";
  if (typeof messageId !== "string" || !messageId || messageId.length > 1024
      || !Number.isInteger(index) || index < 0 || index >= MAX_CHUNKS
      || typeof final !== "boolean" || typeof delta !== "string") {
    throw new StreamError("invalid MessageDisplay message_id, index, final or delta");
  }
  for (const alias of ["message_id", "messageId"]) {
    if (Object.hasOwn(payload, alias) && payload[alias] !== messageId)
      throw new StreamError("conflicting MessageDisplay identifiers");
  }
  for (const alias of ["delta", "text", "message"]) {
    if (Object.hasOwn(payload, alias) && payload[alias] !== delta)
      throw new StreamError("conflicting MessageDisplay text fields");
  }
  if (encoder.encode(JSON.stringify(payload)).length > MAX_CHUNK_BYTES)
    throw new StreamError("MessageDisplay chunk is too large");
  return { messageId, index, final, delta };
}

async function cipherKey(keys, id) {
  if (!Object.hasOwn(keys.keys, id)) throw new Error("Buffered message key is unavailable");
  return crypto.subtle.importKey("raw", Buffer.from(keys.keys[id], "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function stageMessage(db, keys, payload, deviceId) {
  const { messageId, index, final } = fields(payload);
  const sessionId = payload.session_id ?? payload.sessionId;
  if (typeof sessionId !== "string" || !sessionId)
    throw new StreamError("session_id is required");
  const stream = await digest(encoder.encode(JSON.stringify(["claude-cloud", deviceId, sessionId, messageId])));
  const receipt = () => db.prepare("SELECT event_id FROM message_receipts WHERE stream = ?").bind(stream).first();
  if (await receipt()) return null;

  // Store the receipt time inside the ciphertext, so retrying completion has a stable timestamp.
  const plaintext = encoder.encode(JSON.stringify({ payload, received: new Date().toISOString() }));
  const keyId = keys.active;
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const aad = (idx, last, id) => encoder.encode(JSON.stringify([stream, idx, Boolean(last), id]));
  const ciphertext = Buffer.from(await crypto.subtle.encrypt({
    name: "AES-GCM", iv: nonce, additionalData: aad(index, final, keyId),
  }, await cipherKey(keys, keyId), plaintext)).toString("base64");

  // The quota, final-index and receipt checks run in the INSERT, including concurrent requests.
  // ponytail: quota SUM scans at most 64 MiB of staging; add counters if this becomes a bottleneck.
  await db.prepare(`INSERT OR IGNORE INTO message_chunks (stream, idx, final, key_id, nonce, ciphertext)
    SELECT ?, ?, ?, ?, ?, ?
    WHERE NOT EXISTS (SELECT 1 FROM message_receipts WHERE stream = ?)
      AND NOT EXISTS (SELECT 1 FROM message_chunks WHERE stream = ?
        AND ((final = 1 AND idx < ?) OR (? = 1 AND (idx > ? OR (final = 1 AND idx <> ?)))))
      AND (SELECT COALESCE(SUM(length(ciphertext)), 0) FROM message_chunks WHERE stream = ?) + ? <= ?
      AND (SELECT COALESCE(SUM(length(ciphertext)), 0) FROM message_chunks) + ? <= ?`)
    .bind(stream, index, Number(final), keyId, Buffer.from(nonce).toString("base64"), ciphertext,
      stream, stream, index, Number(final), index, index,
      stream, ciphertext.length, MAX_MESSAGE_BYTES, ciphertext.length, MAX_BUFFER_BYTES).run();
  if (await receipt()) return null;
  const { results: rows } = await db.prepare("SELECT * FROM message_chunks WHERE stream = ? ORDER BY idx").bind(stream).all();
  const finals = rows.filter((row) => row.final).map((row) => row.idx);
  if ((finals.length && (index > finals[0] || (final && index !== finals[0])))
      || (final && rows.some((row) => row.idx > index)))
    throw new StreamError("conflicting MessageDisplay final index");
  const previous = rows.find((row) => row.idx === index);
  if (!previous) {
    if (await receipt()) return null;
    throw new Error("Encrypted message buffer is full; pending data was retained");
  }
  async function decrypt(row) {
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(await crypto.subtle.decrypt({
        name: "AES-GCM", iv: Buffer.from(row.nonce, "base64"),
        additionalData: aad(row.idx, row.final, row.key_id),
      }, await cipherKey(keys, row.key_id), Buffer.from(row.ciphertext, "base64"))));
    } catch {
      throw new Error("Cannot decrypt buffered message; restore the correct keys");
    }
  }
  const previousPayload = (await decrypt(previous)).payload;
  // Compare objects independent of property order, without persisting a hash of raw text.
  if (!isDeepStrictEqual(previousPayload, payload))
    throw new StreamError("conflicting MessageDisplay retry");
  if (finals.length > 1 || (finals.length && rows.at(-1).idx > finals[0]))
    throw new StreamError("conflicting MessageDisplay final index");
  if (!finals.length || rows.length !== finals[0] + 1) return null;
  const chunks = await Promise.all(rows.map(decrypt));
  const complete = { ...chunks[0].payload };
  delete complete.text;
  delete complete.message;
  delete complete.messageId;
  Object.assign(complete, { message_id: messageId, index: 0, final: true,
    delta: chunks.map((chunk) => fields(chunk.payload).delta).join("") });
  if (!["timestamp", "occurred_at", "created_at", "createdAt"].some((key) => complete[key]))
    complete.timestamp = chunks[0].received;
  return { stream, payload: complete };
}

export function finishStatements(db, stream, fingerprint) {
  return [
    db.prepare(`INSERT OR IGNORE INTO message_receipts (stream, event_id)
      SELECT ?, id FROM events WHERE fingerprint = ?`).bind(stream, fingerprint),
    db.prepare(`DELETE FROM message_chunks WHERE stream = ?
      AND EXISTS (SELECT 1 FROM message_receipts WHERE stream = ?)`).bind(stream, stream),
  ];
}
