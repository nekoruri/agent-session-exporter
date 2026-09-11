import { lintSource } from "@secretlint/core";
import { creator as preset } from "@secretlint/secretlint-rule-preset-recommend";

const REDACTED = "[REDACTED]";
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

async function redactText(value) {
  const text = value
    .replace(/\b[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s"`<>]+/g, cleanUrl)
    .replace(/\b(?:Bearer|Basic)\s+[^\s"'`<>]+/gi, REDACTED);
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
  let redacted = text;
  for (const [start, end] of ranges.reverse()) {
    redacted = redacted.slice(0, start) + REDACTED + redacted.slice(end);
  }
  return redacted;
}

export async function redactValue(value, key = "") {
  if (key && secretKey(key)) return REDACTED;
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) {
    const result = [];
    for (const item of value) result.push(await redactValue(item));
    return result;
  }
  if (value && typeof value === "object") {
    const entries = [];
    for (const [childKey, childValue] of Object.entries(value)) {
      entries.push([childKey, await redactValue(childValue, childKey)]);
    }
    return Object.fromEntries(entries);
  }
  return value;
}
