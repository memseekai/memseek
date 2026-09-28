// The pi harness: run one pi session over a prepared Computer root and report
// it in the memseek harness contract (see ../README.md).
//
// Plain Node ESM with no dependencies, so the same file runs on a developer
// machine and inside the Computer container unchanged.
import { spawn, spawnSync } from "node:child_process";
import { createInterface } from "node:readline";
import { finished } from "node:stream/promises";
import { createWriteStream, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const MAX_EVENTS = 200;
const KILL_GRACE_MS = 5_000;
// A turn can end on a provider error (a live run got a one-off 400 "Invalid
// request data") or without the envelope. The session is saved, so the run
// resumes it rather than losing everything done so far.
const MAX_RESUMES = 2;
const RESUME_PROMPT =
  "Your previous turn ended before you finished. Continue the task from where you left off, "
  + "and end with the final JSON object.";

const root = process.cwd();
const input = JSON.parse(readFileSync(join(root, ".harness", "input.json"), "utf8"));
const promptFile = join(root, ".harness", "system-prompt.md");
writeFileSync(promptFile, input.system_prompt);
// Kept for debugging: pi's own session (open it with `pi --session`, or read
// transcript.html), the event stream as it happens, and pi's stderr.
const sessionDir = join(root, ".harness", "pi-sessions");
mkdirSync(sessionDir, { recursive: true });
// Earlier runs on this Computer left their sessions here; this run resumes and
// exports only its own.
const earlierSessions = new Set(listSessions());
const eventLog = createWriteStream(join(root, ".harness", "pi-events.jsonl"));
const stderrLog = createWriteStream(join(root, ".harness", "pi-stderr.log"));
// Per-token deltas would be most of the file and none of the insight.
const NOISY_EVENTS = new Set(["message_update", "tool_execution_update"]);

const args = [
  "--mode", "json",
  "--session-dir", sessionDir,
  "--provider", input.model.provider,
  "--model", input.model.model,
  "--append-system-prompt", promptFile,
  // Only the skills the Computer mounted, never whatever sits in the
  // operator's home directory.
  "--no-skills",
  ...input.skills.flatMap((skill) => ["--skill", skill.dir]),
  "--extension", join(dirname(fileURLToPath(import.meta.url)), "memseek-tools.mjs"),
];
if (typeof input.model.params?.thinking === "string") {
  args.push("--thinking", input.model.params.thinking);
}

const started = Date.now();
let child = null;

let stopped = null;
function stop(reason) {
  if (stopped) return;
  stopped = reason;
  child?.kill("SIGTERM");
  setTimeout(() => child?.kill("SIGKILL"), KILL_GRACE_MS).unref();
}
const wallTimer = setTimeout(
  () => stop(`exceeded max_wall_s ${input.limits.max_wall_s}`),
  input.limits.max_wall_s * 1000,
);

const metrics = { steps: 0, tool_calls: 0, tool_errors: 0 };
const usage = {
  seen: false, input: 0, cacheRead: 0, cacheWrite: 0, output: 0, cost: 0, costSeen: false,
};
const events = [];
let finalText = "";
let lastStopReason = null;

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
    lastStopReason = typeof message.stopReason === "string" ? message.stopReason : null;
    if (message.usage && typeof message.usage === "object") {
      usage.seen = true;
      usage.input += number(message.usage.input);
      usage.cacheRead += number(message.usage.cacheRead);
      usage.cacheWrite += number(message.usage.cacheWrite);
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

function runPi(extra, prompt) {
  child = spawn("pi", [...args, ...extra, "--", prompt], {
    cwd: join(root, "workspace"),
    env: { ...process.env, MEMSEEK_HARNESS_ROOT: root },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stderr.on("data", (chunk) => {
    stderrLog.write(chunk);
    process.stderr.write(chunk);
  });
  createInterface({ input: child.stdout }).on("line", (line) => {
    if (!line.trim()) return;
    try {
      const event = JSON.parse(line);
      if (!NOISY_EVENTS.has(event?.type)) eventLog.write(`${line}\n`);
      observe(event);
    } catch {
      // A non-JSON line is pi talking to a human; it carries nothing we count.
    }
  });
  return new Promise((resolve) => {
    child.on("error", (error) => fail(`cannot start pi: ${error.message}`));
    child.on("close", (exitCode, exitSignal) => resolve([exitCode, exitSignal]));
  });
}

function listSessions() {
  return readdirSync(sessionDir, { recursive: true })
    .map(String)
    .filter((entry) => entry.endsWith(".jsonl"));
}

function sessionFile() {
  const name = listSessions().find((entry) => !earlierSessions.has(entry));
  return name ? join(sessionDir, name) : null;
}

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

// Exit 2 tells the provider the run spent its budget, so it is not retried.
function fail(message, code = 1) {
  process.stderr.write(`pi harness: ${message}\n`);
  process.exit(code);
}

let [code, signal] = await runPi([], input.task);
let envelope = parseEnvelope(finalText);
for (let resume = 1; resume <= MAX_RESUMES; resume += 1) {
  if (stopped || (code === 0 && envelope && lastStopReason !== "error")) break;
  const session = sessionFile();
  if (!session) break;
  const reason = lastStopReason === "error" ? "model_error" : code !== 0 ? "exit" : "no_envelope";
  record("harness_resume", { attempt: resume, reason });
  eventLog.write(`${JSON.stringify({ type: "harness_resume", attempt: resume, reason })}\n`);
  finalText = "";
  [code, signal] = await runPi(["--session", session], RESUME_PROMPT);
  envelope = parseEnvelope(finalText);
}
clearTimeout(wallTimer);
eventLog.end();
stderrLog.end();
await Promise.all([finished(eventLog), finished(stderrLog)]);
exportTranscript();

// Best effort: a failed export must not hide the run's real outcome.
function exportTranscript() {
  try {
    const session = sessionFile();
    if (!session) return;
    spawnSync("pi", ["--export", session, join(root, ".harness", "transcript.html")], {
      stdio: "ignore",
      timeout: 30_000,
    });
  } catch {
    // The session file and event log are still there to read.
  }
}

if (stopped) fail(stopped, 2);
if (code !== 0) fail(`pi exited ${code ?? signal}`);
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
    cache_read_tokens: usage.seen ? usage.cacheRead : null,
    cache_write_tokens: usage.seen ? usage.cacheWrite : null,
    output_tokens: usage.seen ? usage.output : null,
    cost_usd: usage.costSeen ? usage.cost : null,
  },
};
process.stdout.write(`${JSON.stringify(output)}\n`);
