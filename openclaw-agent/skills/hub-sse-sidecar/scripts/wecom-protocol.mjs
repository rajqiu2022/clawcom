const MAX_LINE_BYTES = 256 * 1024;
const SCHEMAS = {
  configure: ["v", "type", "credentials_path", "sdk_root"],
  activate: ["v", "type"],
  reply: ["v", "type", "event_id", "stream", "text"],
  shutdown: ["v", "type"],
};

function fail(code) { throw new Error(code); }
function text(value, name, max = MAX_LINE_BYTES) {
  if (typeof value !== "string" || !value || value.includes("\0") || Buffer.byteLength(value) > max) {
    fail(`INVALID_${name.toUpperCase()}`);
  }
}
function exactKeys(value, allowed) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) fail("INVALID_OBJECT");
  for (const key of Object.keys(value)) if (!allowed.includes(key)) fail("UNEXPECTED_FIELD");
}

export function parsePythonLine(line) {
  if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) fail("LINE_TOO_LARGE");
  let value;
  try { value = JSON.parse(line); } catch { fail("INVALID_JSON"); }
  if (!value || value.v !== 1 || !Object.hasOwn(SCHEMAS, value.type)) fail("UNSUPPORTED_COMMAND");
  exactKeys(value, SCHEMAS[value.type]);
  if (value.type === "configure") {
    text(value.credentials_path, "credentials_path", 32768);
    text(value.sdk_root, "sdk_root", 32768);
  } else if (value.type === "reply") {
    text(value.event_id, "event_id", 256);
    text(value.text, "text");
    if (typeof value.stream !== "boolean") fail("INVALID_STREAM");
  }
  return value;
}

export function normalizeTextFrame(frame) {
  const body = frame?.body;
  text(body?.msgid, "event_id", 256);
  text(body?.from?.userid, "sender_id", 256);
  text(body?.text?.content, "text");
  if (!["single", "group"].includes(body.chattype)) fail("INVALID_CHATTYPE");
  const group = body.chattype === "group";
  const id = group ? body.chatid : body.from.userid;
  text(id, "conversation_id", 256);
  return {
    v: 1, type: "inbound", event_id: body.msgid,
    conversation: {kind: group ? "group" : "user", id},
    sender_id: body.from.userid, mentioned: group, text: body.text.content,
  };
}

export function serializeBridgeEvent(event) {
  const line = JSON.stringify(event);
  if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES || line.includes("\0")) fail("LINE_TOO_LARGE");
  return line;
}
