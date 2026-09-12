import { lintSource } from "@secretlint/core";
import { creator as preset } from "@secretlint/secretlint-rule-preset-recommend";

const REDACTED = "[REDACTED]";
const IDENTITY_KEYS = new Set(["id", "session_id", "sessionId", "task_id", "taskId", "device_id", "deviceId", "message_id", "messageId"]);
// Field names and protocol syntax are policy; provider formats belong to Secretlint.
const SECRET_KEY_RE =
  /(?:^|[_-])(?:api[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret|secret|token|password|passwd|authorization|cookie)(?:$|[_-])/i;
const rules = preset.rules
  // Conversation text must never be able to disable credential detection.
  .filter((rule) => rule.meta.id !== "@secretlint/secretlint-rule-filter-comments")
  .map((rule) => ({ id: rule.meta.id, rule, options: {} }));

function secretKey(key) {
  return SECRET_KEY_RE.test(key.replace(/([a-z0-9])([A-Z])/g, "$1_$2"));
}

function cleanUrl(value) {
  try {
    const url = new URL(value);
    let changed = Boolean(url.username || url.password);
    url.username = "";
    url.password = "";
    for (const key of new Set(url.searchParams.keys())) {
      if (secretKey(key)) {
        url.searchParams.set(key, REDACTED);
        changed = true;
      }
    }
    return changed ? url.href : value;
  } catch {
    return REDACTED;
  }
}

async function credentialRanges(text) {
  let result;
  try {
    result = await lintSource({
      source: { content: text, filePath: "conversation.txt", contentType: "text" },
      options: { noPhysicFilePath: true, maskSecrets: true, config: { rules } },
    });
  } catch {
    // Do not persist the input or include it in an error response/log.
    throw new Error("Credential redaction failed");
  }
  const ranges = [];
  for (const { range } of result.messages.sort((a, b) => a.range[0] - b.range[0])) {
    const previous = ranges.at(-1);
    if (previous && range[0] <= previous[1]) {
      previous[1] = Math.max(previous[1], range[1]);
    } else {
      ranges.push([...range]);
    }
  }
  return ranges;
}

function mapStrings(value, transform, key = "") {
  if (key && secretKey(key)) return REDACTED;
  if (typeof value === "string") return transform(value, key);
  if (Array.isArray(value)) {
    return value.map((item) => mapStrings(item, transform));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(
      ([childKey, childValue]) => [childKey, mapStrings(childValue, transform, childKey)],
    ));
  }
  return value;
}

export async function redactValue(value, key = "") {
  const fields = [];
  let offset = 0;
  const prepared = mapStrings(value, (original, fieldKey) => {
    const text = original
      .replace(/\b[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s"`<>]+/g, cleanUrl)
      .replace(/\b(?:Bearer|Basic)\s+[^\s"'`<>]+/gi, REDACTED);
    fields.push({ original, key: fieldKey, text, start: offset });
    offset += text.length + 1;
    return text;
  }, key);
  if (!fields.length) return prepared;

  // Scan once per payload. Map UTF-16 ranges back by offsets, never by splitting on input text.
  const ranges = await credentialRanges(fields.map((field) => field.text).join("\n"));
  let rangeIndex = 0;
  for (const field of fields) {
    const { text, start } = field;
    const end = start + text.length;
    const parts = [];
    let cursor = start;
    while (rangeIndex < ranges.length && ranges[rangeIndex][1] <= start) rangeIndex++;
    while (rangeIndex < ranges.length && ranges[rangeIndex][0] < end) {
      const [from, to] = ranges[rangeIndex];
      parts.push(text.slice(cursor - start, Math.max(start, from) - start), REDACTED);
      cursor = Math.min(end, to);
      // A multiline credential may span several fields; mask its intersection with each.
      if (to > end) break;
      rangeIndex++;
    }
    parts.push(text.slice(cursor - start));
    field.text = parts.join("");
    if (IDENTITY_KEYS.has(field.key) && field.text !== field.original) {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(field.original));
      field.text = "redacted-" + [...new Uint8Array(digest)]
        .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    }
  }
  let fieldIndex = 0;
  return mapStrings(prepared, () => fields[fieldIndex++].text, key);
}
