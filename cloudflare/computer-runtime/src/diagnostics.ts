/** Request-local diagnostics: identities and error fields, never request/model bodies. */
export class ExecutionDiagnostics {
  readonly requestId = crypto.randomUUID();
  stage = "parse_request";
  private identity: Record<string, string> = {};
  private readonly started = Date.now();

  constructor(private readonly scope: "runtime" | "agent", private readonly secret: string) {}

  identify(request: { task_id: string; session_key: string; executor: { ref: string } }): void {
    this.identity = {
      task_id: this.clean(request.task_id, 200),
      session_key: this.clean(request.session_key, 64),
      executor_ref: this.clean(request.executor.ref, 200),
    };
  }

  at(stage: string): void {
    this.stage = stage;
    console.log(JSON.stringify({ event: "computer.stage", ...this.fields() }));
  }

  modelStep(
    index: number, finishReason: string, textChars: number, tools: string[],
    errors: Array<{ tool: string; error: unknown }>,
  ): void {
    console.log(JSON.stringify({
      event: "computer.model_step", ...this.fields(), index,
      finish_reason: this.clean(finishReason, 100), text_chars: textChars,
      tools: tools.map((name) => this.clean(name, 100)),
      tool_errors: errors.map(({ tool, error }) => ({ tool: this.clean(tool, 100), error: this.describe(error) })),
    }));
  }

  failure(error: unknown): { error: string; stage: string; request_id: string } {
    const detail = this.describe(error);
    console.error(JSON.stringify({ event: "computer.execution_failed", ...this.fields(), error: detail }));
    return { error: String(detail.message), stage: this.stage, request_id: this.requestId };
  }

  private fields(): Record<string, unknown> {
    return {
      scope: this.scope, request_id: this.requestId, ...this.identity,
      stage: this.stage, elapsed_ms: Date.now() - this.started,
    };
  }

  private clean(value: string, limit: number): string {
    let text = this.secret ? value.split(this.secret).join("[redacted]") : value;
    text = text.replace(/Type validation failed: Value: [\s\S]*?\nError message:/g, "Type validation failed: [input omitted]. Error message:");
    text = text.replace(/\bBearer\s+[^\s,"']+/gi, "Bearer [redacted]")
      .replace(/((?:api[_-]?key|token|secret|authorization)["']?\s*[:=]\s*["']?)[^\s,"'&}]+/gi, "$1[redacted]");
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  }

  private describe(error: unknown, depth = 0, seen = new Set<unknown>()): Record<string, unknown> {
    if (depth > 3 || seen.has(error)) return { message: "[cause truncated]" };
    seen.add(error);
    if (typeof error !== "object" || error === null) {
      return { name: "ThrownValue", message: this.clean(String(error), 2000) };
    }
    const value = error as Record<string, unknown>;
    const result: Record<string, unknown> = {
      name: typeof value.name === "string" ? this.clean(value.name, 100) : "Error",
      message: typeof value.message === "string" ? this.clean(value.message, 2000) : "execution_failed",
    };
    if (typeof value.stack === "string") result.stack = this.clean(value.stack, 6000);
    // Explicit allowlist avoids SDK error.requestBody / responseBody / headers.
    for (const key of ["code", "statusCode"]) {
      if (typeof value[key] === "string") result[key] = this.clean(value[key], 100);
      else if (typeof value[key] === "number") result[key] = value[key];
    }
    if (value.cause !== undefined) result.cause = this.describe(value.cause, depth + 1, seen);
    if (value.lastError !== undefined) result.last_error = this.describe(value.lastError, depth + 1, seen);
    if (Array.isArray(value.errors)) {
      result.errors = value.errors.slice(0, 3).map((item) => this.describe(item, depth + 1, seen));
    }
    return result;
  }
}
