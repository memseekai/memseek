// Print what a pi run did, turn by turn, from its .harness/pi-events.jsonl.
//
//   node harnesses/pi/trace.mjs [root] [--follow] [--full]
//
// With no root it picks the newest run under LOCAL_COMPUTER_ROOT
// (default ~/.memseek/computers). --follow keeps printing while the run is
// live. --full prints tool arguments and results untruncated.
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const follow = args.includes("--follow");
const full = args.includes("--full");
const root = args.find((arg) => !arg.startsWith("--")) ?? newestRoot();
const eventsPath = join(root, ".harness", "pi-events.jsonl");

const color = process.stdout.isTTY
  ? { dim: "\x1b[2m", red: "\x1b[31m", green: "\x1b[32m", cyan: "\x1b[36m", bold: "\x1b[1m", off: "\x1b[0m" }
  : { dim: "", red: "", green: "", cyan: "", bold: "", off: "" };

function newestRoot() {
  const base = (process.env.LOCAL_COMPUTER_ROOT ?? join(homedir(), ".memseek", "computers"))
    .replace(/^~(?=\/)/, homedir());
  const runs = readdirSync(base)
    .map((name) => join(base, name))
    .filter((path) => existsSync(join(path, ".harness", "input.json")))
    .sort((a, b) => statSync(join(b, ".harness", "input.json")).mtimeMs
      - statSync(join(a, ".harness", "input.json")).mtimeMs);
  if (!runs.length) {
    console.error(`no harness runs under ${base}`);
    process.exit(1);
  }
  return runs[0];
}

function clip(text, limit) {
  const value = typeof text === "string" ? text : JSON.stringify(text);
  if (full || value.length <= limit) return value;
  return `${value.slice(0, limit)}${color.dim}… (+${value.length - limit} chars, --full)${color.off}`;
}

function textOf(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((part) => part?.type === "text").map((part) => part.text).join("");
}

let turn = 0;
let cost = 0;
function show(event) {
  switch (event.type) {
    case "session":
      console.log(`${color.bold}pi session${color.off} ${event.id ?? ""} ${color.dim}${event.cwd ?? ""}${color.off}`);
      break;
    case "turn_start":
      turn += 1;
      console.log(`\n${color.bold}── turn ${turn}${color.off}`);
      break;
    case "message_end": {
      const message = event.message ?? {};
      if (message.role === "user") {
        console.log(`${color.cyan}user${color.off} ${clip(textOf(message.content), 400)}`);
      } else if (message.role === "assistant") {
        const text = textOf(message.content).trim();
        if (text) console.log(`${color.cyan}assistant${color.off} ${clip(text, 800)}`);
        for (const part of message.content ?? []) {
          if (part?.type === "toolCall") {
            console.log(`  → ${color.bold}${part.name}${color.off} ${clip(part.arguments ?? {}, 300)}`);
          }
        }
        const usage = message.usage ?? {};
        cost += usage.cost?.total ?? 0;
        console.log(`  ${color.dim}in ${usage.input ?? "?"} · cache read ${usage.cacheRead ?? "?"} · `
          + `cache write ${usage.cacheWrite ?? "?"} · out ${usage.output ?? "?"} · `
          + `$${(usage.cost?.total ?? 0).toFixed(4)} (run $${cost.toFixed(4)}) · ${message.stopReason ?? ""}${color.off}`);
        if (message.errorMessage) console.log(`  ${color.red}model error: ${message.errorMessage}${color.off}`);
      }
      break;
    }
    case "tool_execution_end": {
      const mark = event.isError ? `${color.red}ERROR${color.off}` : `${color.green}ok${color.off}`;
      console.log(`  ← ${event.toolName} ${mark} ${clip(textOf(event.result?.content) || event.result, 400)}`);
      break;
    }
    case "harness_resume":
      console.log(`\n${color.red}resumed the session (attempt ${event.attempt}): ${event.reason}${color.off}`);
      break;
    case "auto_retry_start":
      console.log(`  ${color.red}retry ${event.attempt}/${event.maxAttempts}: ${event.errorMessage}${color.off}`);
      break;
    case "compaction_start":
      console.log(`  ${color.dim}compacting context (${event.reason})${color.off}`);
      break;
    case "extension_error":
      console.log(`  ${color.red}extension error in ${event.extensionPath}: ${event.error}${color.off}`);
      break;
    case "agent_end":
      console.log(`\n${color.bold}done${color.off} after ${turn} turns, $${cost.toFixed(4)}`);
      return true;
    default:
      break;
  }
  return false;
}

console.log(`${color.dim}${root}${color.off}`);
let offset = 0;
let pending = "";
let ended = false;
let grewAt = Date.now();
// A harness can resume pi after an agent_end, so following stops only once
// the log has also gone quiet.
const QUIET_MS = 8_000;
function drain() {
  if (!existsSync(eventsPath)) return;
  const data = readFileSync(eventsPath);
  if (data.length <= offset) return;
  grewAt = Date.now();
  pending += data.subarray(offset).toString("utf8");
  offset = data.length;
  const lines = pending.split("\n");
  pending = lines.pop() ?? "";
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      if (event.type === "harness_resume") ended = false;
      if (show(event)) ended = true;
    } catch {
      // A torn or non-JSON line; the next read completes it or it carries nothing.
    }
  }
}

drain();
if (!existsSync(eventsPath) && !follow) {
  console.error(`no ${eventsPath}; the run has not started pi yet, or predates event logging`);
  process.exit(1);
}
if (follow && !ended) {
  const timer = setInterval(() => {
    drain();
    if (ended && Date.now() - grewAt > QUIET_MS) clearInterval(timer);
  }, 500);
}
for (const [label, name] of [["transcript", "transcript.html"], ["stderr", "pi-stderr.log"]]) {
  const path = join(root, ".harness", name);
  if (!follow && existsSync(path)) console.log(`${color.dim}${label}: ${path}${color.off}`);
}
