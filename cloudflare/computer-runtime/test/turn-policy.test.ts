import { expect, it, vi } from "vitest";
import { recallPaths, resultCachePath, runAgentPhases } from "../src/turn-policy";
import type { ExecuteRequest } from "../src/protocol";
import type { ToolSet } from "ai";

it("returns a paused decision without ever exposing write tools", async () => {
  const decision = { text: "question", steps: [1] };
  const generate = vi.fn().mockResolvedValue(decision);
  const tools = { read: {}, write: {}, exec: {} } as unknown as ToolSet;
  const result = await runAgentPhases({ invocation: true, system: "base", tools, maxSteps: 24, generate, parseDecision: () => ({ value: {}, awaiting_input: true }) });
  expect(generate).toHaveBeenCalledTimes(1);
  expect(Object.keys(generate.mock.calls[0][1])).toEqual([]);
    // The planner cannot load a skill — it has no tools and one step — so it is
    // told to name one instead, and that plan text reaches the execution phase.
    expect(generate.mock.calls[0][0]).toContain("name it by exact name in the plan");
  expect(generate.mock.calls[0][2]).toBe(1);
  expect(result.outcome).toBe(decision);
});

it("shares the existing step budget and audits steps from both phases", async () => {
  const generate = vi.fn().mockResolvedValueOnce({ text: "plan", steps: [1] }).mockResolvedValueOnce({ text: "done", steps: [2, 3, 4, 5] });
  const tools = { write: {} } as unknown as ToolSet;
  const result = await runAgentPhases({ invocation: true, system: "base", tools, maxSteps: 5, generate, parseDecision: () => ({ value: { plan: "write" }, awaiting_input: false }) });
  expect(generate.mock.calls[1].slice(1)).toEqual([tools, 4, 1]);
  expect(result.completedSteps).toEqual([1, 2, 3, 4, 5]);
  expect(result.outcome.text).toBe("done");
});

it("does not report a plan as completed execution when the step budget is exhausted", async () => {
  const generate = vi.fn().mockResolvedValue({ text: "plan", steps: [1] });
  await expect(runAgentPhases({ invocation: true, system: "base", tools: {}, maxSteps: 1, generate, parseDecision: () => ({ value: {}, awaiting_input: false }) })).rejects.toThrow("step budget exhausted");
  expect(generate).toHaveBeenCalledTimes(1);
});

it("replays retries but executes each human reply and changed authority separately", async () => {
  const request = { task_id: "same-invocation", input: { prompt: "renew", turns: [] }, citation_ids: ["first"] } as unknown as ExecuteRequest;
  const initial = await resultCachePath(request);
  expect(await resultCachePath({ ...request })).toBe(initial);
  expect(await resultCachePath({ citation_ids: ["first"], input: { turns: [], prompt: "renew" }, task_id: "same-invocation" } as unknown as ExecuteRequest)).toBe(initial);
  const reply = { ...request, input: { prompt: "renew", turns: [{ prompt: "12% maximum" }] } };
  expect(await resultCachePath(reply)).not.toBe(initial);
  expect(await resultCachePath({ ...reply, input: { prompt: "renew", turns: [{ prompt: "12% maximum" }, { prompt: "two years" }] } })).not.toBe(await resultCachePath(reply));
  expect(await resultCachePath({ ...request, citation_ids: ["second"] })).not.toBe(initial);
});

it("recalls supplied context and scratch files without indexing metadata or past receipts", () => {
  expect(recallPaths({ "/.memseek/context.txt": "evidence", "/.memseek/manifest.json": "metadata" }, [
    "/workspace/notes.txt", "/workspace/recalled/first.json", "/.memseek/results/cache.json",
  ])).toEqual(["/.memseek/context.txt", "/workspace/notes.txt"]);
});
