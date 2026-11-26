// OTLP/JSON spans for one pi run, built from the events run.mjs already reads.
//
// The worker owns the run's root span and names it in TRACEPARENT; every span
// here is its child. Without TRACEPARENT and an endpoint this does nothing.
// No dependencies: OTLP/JSON is the wire format, posted with fetch.

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;
const FLUSH_INTERVAL_MS = 1_000;
const EXPORT_TIMEOUT_MS = 5_000;
// Full content is still bounded per attribute, so one huge tool result cannot
// make an export request the store refuses.
const MAX_ATTRIBUTE_CHARS = 65_536;

// GenAI semantic convention names (Development status). Kept in one place so a
// convention change is one edit; the Python episode renderer reads the same.
export const ATTR = Object.freeze({
  operation: "gen_ai.operation.name",
  provider: "gen_ai.provider.name",
  requestModel: "gen_ai.request.model",
  responseModel: "gen_ai.response.model",
  responseId: "gen_ai.response.id",
  finishReasons: "gen_ai.response.finish_reasons",
  inputTokens: "gen_ai.usage.input_tokens",
  outputTokens: "gen_ai.usage.output_tokens",
  systemInstructions: "gen_ai.system_instructions",
  inputMessages: "gen_ai.input.messages",
  outputMessages: "gen_ai.output.messages",
  toolName: "gen_ai.tool.name",
  toolCallId: "gen_ai.tool.call.id",
  toolArguments: "gen_ai.tool.call.arguments",
  toolResult: "gen_ai.tool.call.result",
  errorType: "error.type",
});

const TRACE_ENV = [
  "TRACEPARENT",
  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
  "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
  "MEMSEEK_TRACE_CONTENT",
];

// The agent's environment: the trace variables are the harness's, not pi's.
export function agentEnvironment(env) {
  const copy = { ...env };
  for (const name of TRACE_ENV) delete copy[name];
  return copy;
}

function headers(raw) {
  const result = { "Content-Type": "application/json" };
  for (const pair of (raw ?? "").split(",")) {
    const at = pair.indexOf("=");
    if (at > 0) result[pair.slice(0, at).trim()] = decodeURIComponent(pair.slice(at + 1).trim());
  }
  return result;
}

function spanId() {
  return [...crypto.getRandomValues(new Uint8Array(8))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function nanos(ms) {
  return `${BigInt(Math.round(ms * 1_000)) * 1_000n}`;
}

function clip(text) {
  return text.length <= MAX_ATTRIBUTE_CHARS
    ? text
    : `${text.slice(0, MAX_ATTRIBUTE_CHARS)}… (+${text.length - MAX_ATTRIBUTE_CHARS} chars)`;
}

function attribute(key, value) {
  if (Array.isArray(value) && value.every((item) => typeof item !== "object" || item === null)) {
    return {
      key,
      value: { arrayValue: { values: value.map((item) => ({ stringValue: String(item) })) } },
    };
  }
  if (typeof value === "number" && Number.isInteger(value)) {
    return { key, value: { intValue: String(value) } };
  }
  if (typeof value === "number") return { key, value: { doubleValue: value } };
  if (typeof value === "boolean") return { key, value: { boolValue: value } };
  return { key, value: { stringValue: clip(typeof value === "string" ? value : JSON.stringify(value)) } };
}

// pi message parts in the GenAI message shape.
function parts(content) {
  if (typeof content === "string") return [{ type: "text", content }];
  if (!Array.isArray(content)) return [];
  return content.flatMap((part) => {
    if (part?.type === "text" && typeof part.text === "string") {
      return [{ type: "text", content: part.text }];
    }
    if (part?.type === "toolCall") {
      return [{ type: "tool_call", id: part.id, name: part.name, arguments: part.arguments }];
    }
    return [];
  });
}

const DISABLED = Object.freeze({
  enabled: false,
  observe() {},
  resume() {},
  async close() {},
});

export function createTracer(env, input, { now: wall = Date.now, post = fetch } = {}) {
  const parent = TRACEPARENT.exec(env.TRACEPARENT ?? "");
  const endpoint = env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
  if (!parent || !endpoint) return DISABLED;
  const [, traceId, rootSpanId] = parent;
  const full = env.MEMSEEK_TRACE_CONTENT === "full";
  const requestHeaders = headers(env.OTEL_EXPORTER_OTLP_TRACES_HEADERS);

  const pending = [];
  const openTools = new Map();
  let chatStart = null;
  let firstChat = true;
  let newInputs = [];
  // Events often arrive together; a clock that always moves forward by at least
  // the store's microsecond keeps steps in the order they happened.
  let last = 0;
  const now = () => (last = Math.max(wall(), last + 0.001));

  function finish(name, start, end, attributes, error = null) {
    pending.push({
      traceId,
      spanId: spanId(),
      parentSpanId: rootSpanId,
      name,
      kind: 1,
      startTimeUnixNano: nanos(start),
      endTimeUnixNano: nanos(end),
      attributes: Object.entries(attributes)
        .filter(([, value]) => value !== undefined && value !== null)
        .map(([key, value]) => attribute(key, value)),
      status: error ? { code: 2, message: error } : { code: 1 },
    });
  }

  function chat(message) {
    const end = now();
    const usage = message.usage ?? {};
    const attributes = {
      [ATTR.operation]: "chat",
      [ATTR.provider]: message.provider ?? input.model.provider,
      [ATTR.requestModel]: input.model.model,
      [ATTR.responseModel]: message.model,
      [ATTR.responseId]: message.responseId,
      [ATTR.finishReasons]: typeof message.stopReason === "string" ? [message.stopReason] : null,
      [ATTR.inputTokens]: typeof usage.input === "number" ? usage.input : null,
      [ATTR.outputTokens]: typeof usage.output === "number" ? usage.output : null,
      [ATTR.errorType]: message.errorMessage ? "model_error" : null,
    };
    if (full) {
      if (firstChat) attributes[ATTR.systemInstructions] = input.system_prompt;
      if (newInputs.length) attributes[ATTR.inputMessages] = newInputs;
      attributes[ATTR.outputMessages] = [{ role: "assistant", parts: parts(message.content) }];
    }
    finish(`chat ${input.model.model}`, chatStart ?? end, end, attributes, message.errorMessage ?? null);
    firstChat = false;
    newInputs = [];
    chatStart = null;
  }

  function toolEnd(event) {
    const end = now();
    const open = openTools.get(event.toolCallId) ?? { start: end, args: undefined };
    openTools.delete(event.toolCallId);
    const attributes = {
      [ATTR.operation]: "execute_tool",
      [ATTR.toolName]: event.toolName,
      [ATTR.toolCallId]: event.toolCallId,
      [ATTR.errorType]: event.isError === true ? "tool_error" : null,
    };
    if (full) {
      attributes[ATTR.toolArguments] = open.args;
      attributes[ATTR.toolResult] = event.result;
    }
    finish(`execute_tool ${event.toolName ?? "unknown"}`, open.start, end, attributes,
      event.isError === true ? "tool_error" : null);
  }

  async function flush() {
    if (!pending.length) return;
    const spans = pending.splice(0, pending.length);
    const body = {
      resourceSpans: [{
        resource: { attributes: [attribute("service.name", "memseek-harness-pi")] },
        scopeSpans: [{ scope: { name: "memseek.harness.pi" }, spans }],
      }],
    };
    try {
      const response = await post(endpoint, {
        method: "POST",
        headers: requestHeaders,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(EXPORT_TIMEOUT_MS),
      });
      if (!response.ok) process.stderr.write(`pi harness: trace export got ${response.status}\n`);
    } catch (error) {
      process.stderr.write(`pi harness: trace export failed: ${error?.message ?? error}\n`);
    }
  }

  const timer = setInterval(() => { flush(); }, FLUSH_INTERVAL_MS);
  timer.unref();

  return {
    enabled: true,
    observe(event) {
      if (!event || typeof event !== "object") return;
      if (event.type === "message_start" && event.message?.role === "assistant") {
        chatStart = now();
      } else if (event.type === "message_end" && event.message?.role === "assistant") {
        chat(event.message);
      } else if (event.type === "message_end" && event.message?.role === "user" && full) {
        newInputs.push({ role: "user", parts: parts(event.message.content) });
      } else if (event.type === "tool_execution_start") {
        openTools.set(event.toolCallId, { start: now(), args: event.args });
      } else if (event.type === "tool_execution_end") {
        toolEnd(event);
      }
    },
    resume(attempt, reason) {
      const at = now();
      finish("harness_resume", at, at, { "memseek.resume.attempt": attempt, "memseek.resume.reason": reason });
    },
    // Ends whatever was still open, as interrupted, and sends everything.
    async close() {
      clearInterval(timer);
      const at = now();
      for (const [id, open] of openTools) {
        finish("execute_tool interrupted", open.start, at, {
          [ATTR.operation]: "execute_tool",
          [ATTR.toolCallId]: id,
          [ATTR.errorType]: "interrupted",
        }, "interrupted");
      }
      openTools.clear();
      await flush();
    },
  };
}
