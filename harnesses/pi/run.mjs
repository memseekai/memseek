// The pi harness: run one pi session over a prepared Computer root and report
// it in the memseek harness contract (see ../README.md).
//
// Plain Node ESM with no dependencies, so the same file runs on a developer
// machine and inside the Computer container unchanged.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const MAX_EVENTS = 200;
const KILL_GRACE_MS = 5_000;

const root = process.cwd();
const input = JSON.parse(readFileSync(join(root, ".harness", "input.json"), "utf8"));
const promptFile = join(root, ".harness", "system-prompt.md");
writeFileSync(promptFile, input.system_prompt);

const args = [
  "--mode", "json",
  "--no-session",
  "--provider", input.model.provider,
  "--model", input.model.model,
  "--append-system-prompt", promptFile,
  // Only the skills the Computer mounted, never whatever sits in the
  // operator's home directory.
  "--no-skills",
  ...input.skills.flatMap((skill) => ["--skill", skill.dir]),
];
if (typeof input.model.params?.thinking === "string") {
  args.push("--thinking", input.model.params.thinking);
}
args.push("--", input.task);

const started = Date.now();
const child = spawn("pi", args, {
  cwd: join(root, "workspace"),
  env: process.env,
  stdio: ["ignore", "pipe", "inherit"],
});

let stopped = null;
function stop(reason) {
  if (stopped) return;
  stopped = reason;
  child.kill("SIGTERM");
  setTimeout(() => child.kill("SIGKILL"), KILL_GRACE_MS).unref();
}
const wallTimer = setTimeout(
  () => stop(`exceeded max_wall_s ${input.limits.max_wall_s}`),
  input.limits.max_wall_s * 1000,
);

const metrics = { steps: 0, tool_calls: 0, tool_errors: 0 };
const usage = { seen: false, input: 0, output: 0, cost: 0, costSeen: false };
const events = [];
let finalText = "";

function record(kind, payload) {
  if (events.length < MAX_EVENTS) events.push({ kind, payload });
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

// pi's event shapes are only partly documented, so every field read here is
// optional: an unknown event is skipped, never fatal.
function observe(event) {
  if (!event || typeof event !== "object") return;
  if (event.type === "message_end" && event.message?.role === "assistant") {
    const message = event.message;
    metrics.steps += 1;
    const content = Array.isArray(message.content) ? message.content : [];
    const calls = content.filter((part) => part?.type === "toolCall").length;
    metrics.tool_calls += calls;
    const text = content
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("");
    if (text.trim()) finalText = text;
    if (message.usage && typeof message.usage === "object") {
      usage.seen = true;
      usage.input += number(message.usage.input)
        + number(message.usage.cacheRead)
        + number(message.usage.cacheWrite);
      usage.output += number(message.usage.output);
      if (typeof message.usage.cost?.total === "number") {
        usage.costSeen = true;
        usage.cost += number(message.usage.cost.total);
      }
    }
    record("model_step", {
      index: metrics.steps - 1,
      stop_reason: typeof message.stopReason === "string" ? message.stopReason : null,
      tool_calls: calls,
    });
    if (metrics.steps > input.limits.max_steps) {
      stop(`exceeded max_steps ${input.limits.max_steps}`);
    }
  } else if (event.type === "tool_execution_end") {
    if (event.isError === true) metrics.tool_errors += 1;
    record("harness_tool", {
      tool: typeof event.toolName === "string" ? event.toolName.slice(0, 64) : null,
      is_error: event.isError === true,
    });
  }
}

const lines = createInterface({ input: child.stdout });
lines.on("line", (line) => {
  if (!line.trim()) return;
  try {
    observe(JSON.parse(line));
  } catch {
    // A non-JSON line is pi talking to a human; it carries nothing we count.
  }
});

// The envelope is the final assistant text. Models wrap it in prose or a code
// fence often enough that the first parseable object holding `value` wins.
function parseEnvelope(text) {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const end = trimmed.lastIndexOf("}");
  for (let start = trimmed.indexOf("{"); start !== -1 && start < end;
    start = trimmed.indexOf("{", start + 1)) {
    try {
      const parsed = JSON.parse(trimmed.slice(start, end + 1));
      if (parsed && typeof parsed === "object" && "value" in parsed) return parsed;
    } catch {
      // Keep scanning: the object may start later in the text.
    }
  }
  return null;
}

function fail(message) {
  process.stderr.write(`pi harness: ${message}\n`);
  process.exit(1);
}

const [code, signal] = await new Promise((resolve) => {
  child.on("error", (error) => fail(`cannot start pi: ${error.message}`));
  child.on("close", (exitCode, exitSignal) => resolve([exitCode, exitSignal]));
});
clearTimeout(wallTimer);

if (stopped) fail(stopped);
if (code !== 0) fail(`pi exited ${code ?? signal}`);
const envelope = parseEnvelope(finalText);
if (!envelope) fail("final assistant message holds no {value, citation_ids} envelope");
if (!Array.isArray(envelope.citation_ids ?? [])) fail("envelope citation_ids is not a list");

const output = {
  value: envelope.value,
  citation_ids: (envelope.citation_ids ?? []).map(String),
  steps: metrics.steps,
  awaiting_input: envelope.awaiting_input === true,
  events,
  metrics: {
    wall_s: (Date.now() - started) / 1000,
    steps: metrics.steps,
    tool_calls: metrics.tool_calls,
    tool_errors: metrics.tool_errors,
    input_tokens: usage.seen ? usage.input : null,
    output_tokens: usage.seen ? usage.output : null,
    cost_usd: usage.costSeen ? usage.cost : null,
  },
};
process.stdout.write(`${JSON.stringify(output)}\n`);
