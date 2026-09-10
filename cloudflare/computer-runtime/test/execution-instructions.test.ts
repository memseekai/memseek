import { describe, expect, it } from "vitest";
import { executionInstructions, finalAnswerStep, repeatedToolStep } from "../src/execution-instructions";
import type { ExecuteRequest } from "../src/protocol";

const request = {
  mode: "derivation", output_path: "/outbox/final-result.json",
  computer: { writeback: [
    { path: "/outbox/final-result.json", type: "final_result", review: false },
    { path: "/outbox/observations.jsonl", type: "observations", review: false },
    { path: "/outbox/proposals", type: "maintained_state", review: true },
  ] },
} as ExecuteRequest;

describe("the final step", () => {
  it("forbids inventing a skill it can no longer load", () => {
    // At the last step `activeTools` is empty, so a model holding skill names
    // and no way to open them is exactly where confabulation happens.
    const step = finalAnswerStep(3, 4, "system");
    expect(step?.system).toContain("or skill contents");
  });
});

describe("effective execution instructions", () => {
  it("routes derivation results through emit despite a reusable writeback policy", () => {
    const prompt = executionInstructions(request);
    expect(prompt).toContain("pipeline's emit");
    expect(prompt).toContain("Do not write any files under /outbox");
    expect(prompt).toContain("awaiting_input false");
    expect(prompt).not.toContain('"type":"observations"');
  });

  it("describes invocation writeback and the no-write pause contract", () => {
    const prompt = executionInstructions({ ...request, mode: "invocation" });
    expect(prompt).toContain('"path":"/outbox/observations.jsonl"');
    expect(prompt).toContain('"review":true');
    expect(prompt).toContain("A paused turn must not leave");
    expect(prompt).not.toContain('"type":"final_result"');
  });

  it("does not invent writeback when no paths are declared", () => {
    const prompt = executionInstructions({ ...request, mode: "invocation", computer: { ...request.computer, writeback: [] } });
    expect(prompt).toContain("No observation or proposal writeback is permitted");
  });
});

it("reserves only the last step without changing the overall budget", () => {
  expect(finalAnswerStep(22, 24, "base instructions")).toBeUndefined();
  const last = finalAnswerStep(23, 24, "base instructions");
  expect(last?.toolChoice).toBe("none");
  expect(last?.activeTools).toEqual([]);
  expect(last?.system).toContain("base instructions");
  expect(last?.system).toContain("Do not invent");
  expect(finalAnswerStep(0, 1, "base")?.toolChoice).toBe("none");
});

it("ends identical completed tool loops without stopping tools that make progress", () => {
  const step = {
    toolCalls: [{ toolName: "write", input: { path: "/workspace/notes", content: "notes" } }],
    toolResults: [{ toolName: "write", output: { bytesWritten: 5 } }],
  };
  expect(repeatedToolStep([step])).toBe(false);
  const read = { toolCalls: [{ toolName: "read", input: { path: "/workspace/notes" } }], toolResults: [{ toolName: "read", output: "notes" }] };
  expect(repeatedToolStep([step, read, step, read])).toBe(true);
  expect(repeatedToolStep([step, read, step, { ...read, toolResults: [{ toolName: "read", output: "changed notes" }] }])).toBe(false);
  expect(finalAnswerStep(2, 24, "base", [step, step])?.toolChoice).toBe("none");
  expect(repeatedToolStep([step, { ...step, toolResults: [] }])).toBe(false);
  const failed = { ...step, toolResults: [], content: [{ type: "tool-error", toolName: "write", error: new Error("invalid input") }] };
  expect(repeatedToolStep([failed, failed])).toBe(true);
  expect(repeatedToolStep([step, { ...step, toolResults: [{ toolName: "write", output: { bytesWritten: 6 } }] }])).toBe(false);
  expect(repeatedToolStep([step, { ...step, toolCalls: [{ toolName: "write", input: { path: "/workspace/other", content: "notes" } }] }])).toBe(false);
  expect(repeatedToolStep([{ toolCalls: [], toolResults: [] }, { toolCalls: [], toolResults: [] }])).toBe(false);
});
