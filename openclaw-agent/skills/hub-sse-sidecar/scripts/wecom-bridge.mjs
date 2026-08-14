import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import {normalizeTextFrame, parsePythonLine, serializeBridgeEvent} from "./wecom-protocol.mjs";

const ENDPOINT = "wss://openws.work.weixin.qq.com";
const CREDENTIAL_KEYS = ["schema", "bot_id", "secret", "owner_user_id", "allowed_user_ids", "allowed_chat_ids"];

function readCredentials(file) {
  const value = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== [...CREDENTIAL_KEYS].sort().join(",") || value.schema !== 2) {
    throw new Error("INVALID_CREDENTIALS");
  }
  if (!value.bot_id || !value.secret) throw new Error("INVALID_CREDENTIALS");
  return value;
}

function loadSdk(root) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  if (pkg.name !== "@wecom/aibot-node-sdk" || pkg.version !== "1.0.7") throw new Error("SDK_VERSION_MISMATCH");
  return createRequire(path.join(root, "package.json"))(root);
}

function safeError(error) {
  return String(error?.message || error || "bridge error")
    .replace(/(secret|token|password)(\s*[=:]\s*)\S+/gi, "$1$2<redacted>").slice(0, 512);
}

async function main() {
  let client;
  let secret = "";
  const frames = new Map();
  const streams = new Map();
  const emit = event => process.stdout.write(serializeBridgeEvent(event) + "\n");
  const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  for await (const line of input) {
    if (!line) continue;
    let command;
    try {
      command = parsePythonLine(line);
      if (command.type === "configure") {
        if (client) throw new Error("ALREADY_CONFIGURED");
        const credentials = readCredentials(command.credentials_path);
        secret = credentials.secret;
        const sdk = loadSdk(command.sdk_root);
        const WSClient = sdk.WSClient || sdk.default?.WSClient;
        if (typeof WSClient !== "function") throw new Error("SDK_ENTRY_INVALID");
        const sanitize = error => safeError(error).split(secret).join("<redacted>");
        const logger = {debug(){}, info(){}, warn(value){process.stderr.write(sanitize(value)+"\n");}, error(value){process.stderr.write(sanitize(value)+"\n");}};
        client = new WSClient({
          botId: credentials.bot_id, secret: credentials.secret, wsUrl: ENDPOINT,
          heartbeatInterval: 30000, maxReconnectAttempts: -1, logger,
        });
        client.on("authenticated", () => emit({v:1, type:"ack", request_id:"activate"}));
        client.on("error", error => emit({v:1, type:"error", code:"SDK_ERROR", message:sanitize(error)}));
        client.on("message.text", frame => {
          const event = normalizeTextFrame(frame);
          frames.set(event.event_id, frame);
          emit(event);
        });
        emit({v:1, type:"ack", request_id:"configure"});
      } else if (!client) {
        throw new Error("CONFIGURE_REQUIRED");
      } else if (command.type === "activate") {
        client.connect();
      } else if (command.type === "reply") {
        const frame = frames.get(command.event_id);
        if (!frame) throw new Error("UNKNOWN_EVENT");
        const streamId = streams.get(command.event_id) || `stream_${command.event_id}`;
        streams.set(command.event_id, streamId);
        await client.replyStream(frame, streamId, command.text, !command.stream);
        if (!command.stream) { frames.delete(command.event_id); streams.delete(command.event_id); }
      } else if (command.type === "shutdown") {
        client.disconnect();
        break;
      }
    } catch (error) {
      const message = secret ? safeError(error).split(secret).join("<redacted>") : safeError(error);
      emit({v:1, type:"error", code:"COMMAND_FAILED", message});
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { process.stderr.write(safeError(error) + "\n"); process.exitCode = 1; });
}
