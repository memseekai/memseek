import { afterEach, describe, expect, it, vi } from "vitest";
import { ExecutionDiagnostics } from "../src/diagnostics";

afterEach(() => vi.restoreAllMocks());

describe("execution diagnostics", () => {
  it("correlates the failing stage, identities, and nested SDK error without dumping bodies", () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const diagnostics = new ExecutionDiagnostics("agent", "super-secret");
    diagnostics.identify({ task_id: "turn-1", session_key: "abc", executor: { ref: "analyst@1" } });
    diagnostics.at("model_generation");
    const cause = Object.assign(new Error("request rejected, token=super-secret"), {
      statusCode: 400, requestBody: "private prompt", responseBody: "private response", headers: { authorization: "secret" },
    });
    const response = diagnostics.failure(new Error("generation failed", { cause }));
    const entry = JSON.parse(log.mock.calls[0][0]);
    expect(entry.stage).toBe("model_generation");
    expect(entry.task_id).toBe("turn-1");
    expect(entry.error.cause.statusCode).toBe(400);
    expect(entry.error.stack).toContain("generation failed");
    expect(response).toEqual({ error: "generation failed", stage: "model_generation", request_id: diagnostics.requestId });
    expect(JSON.stringify(entry)).not.toContain("super-secret");
    expect(JSON.stringify(entry)).not.toContain("private prompt");
    expect(JSON.stringify(entry)).not.toContain("private response");
  });

  it("bounds cyclic causes, retries, and messages", () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const error = Object.assign(new Error("x".repeat(10000)), { cause: {} as unknown, errors: Array(30).fill(new Error("retry")) });
    error.cause = error;
    new ExecutionDiagnostics("runtime", "").failure(error);
    const entry = JSON.parse(log.mock.calls[0][0]);
    expect(entry.error.message.length).toBeLessThanOrEqual(2001);
    expect(entry.error.errors).toHaveLength(3);
    expect(entry.error.cause.message).toBe("[cause truncated]");
  });

  it("keeps concurrent execution state separate", () => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const first = new ExecutionDiagnostics("runtime", "");
    const second = new ExecutionDiagnostics("runtime", "");
    first.at("outbox_collection");
    second.at("model_generation");
    expect(first.failure("error").stage).toBe("outbox_collection");
    expect(first.requestId).not.toBe(second.requestId);
  });
});

it("reports tool progress without model text or tool result bodies", () => {
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  const diagnostics = new ExecutionDiagnostics("agent", "");
  diagnostics.modelStep(23, "tool-calls", 0, ["read"], [{ tool: "read", error: new Error("missing file") }]);
  const entry = JSON.parse(log.mock.calls[0][0]);
  expect(entry.event).toBe("computer.model_step");
  expect(entry.index).toBe(23);
  expect(entry.tools).toEqual(["read"]);
  expect(entry.tool_errors[0].error.message).toBe("missing file");
  expect(entry.text).toBeUndefined();
  expect(entry.input).toBeUndefined();
  expect(entry.output).toBeUndefined();
});

it("omits SDK validation input dumps while retaining field errors", () => {
  const log = vi.spyOn(console, "error").mockImplementation(() => {});
  new ExecutionDiagnostics("agent", "").failure(new Error('Type validation failed: Value: {"text":"private evidence"}.\nError message: records must be an array'));
  const entry = JSON.stringify(JSON.parse(log.mock.calls[0][0]));
  expect(entry).not.toContain("private evidence");
  expect(entry).toContain("records must be an array");
});
