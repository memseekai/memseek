import type { ExecuteRequest } from "./protocol";
import { sha256 } from "./security";
import type { ToolSet } from "ai";

export async function runAgentPhases<T extends { text: string; steps: unknown[] }>(options: {
  invocation: boolean; system: string; tools: ToolSet; maxSteps: number;
  generate: (system: string, tools: ToolSet, limit: number, offset?: number) => Promise<T>;
  parseDecision: (text: string) => { value: unknown; awaiting_input?: boolean };
}): Promise<{ outcome: T; completedSteps: unknown[] }> {
  const { invocation, system, tools, maxSteps, generate, parseDecision } = options;
  if (!invocation) {
    const outcome = await generate(system, tools, maxSteps);
    return { outcome, completedSteps: outcome.steps };
  }
  const decisionSystem = `${system}\n\nThis is the decision phase. No tools are available. Use the inline evidence and operator turns to decide whether an operator answer is needed now. If more evidence needs to be read, plan that work for the execution phase. Determine whether an operator answer is required before execution. If so, return {"value":{"question":"your concrete question"},"citation_ids":["authorized UUIDs"],"awaiting_input":true}. Otherwise return your execution plan inside value with awaiting_input false; the next phase will perform the writes. Do not claim any writes have occurred. If a listed skill applies, name it by exact name in the plan; the execution phase will load it.`;
  const decision = await generate(decisionSystem, {}, 1);
  const planned = parseDecision(decision.text);
  if (planned.awaiting_input) return { outcome: decision, completedSteps: decision.steps };
  if (decision.steps.length >= maxSteps) throw new Error("agent step budget exhausted before invocation execution");
  const executionSystem = `${system}\n\nThe read-only decision phase determined that execution can proceed. Its plan is: ${JSON.stringify(planned.value)}. Perform the required writes now, then return the final envelope. If you discover you need an operator answer, do not write any outbox files.`;
  const outcome = await generate(executionSystem, tools, maxSteps - decision.steps.length, decision.steps.length);
  return { outcome, completedSteps: [...decision.steps, ...outcome.steps] };
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, canonical(item)]));
  }
  return value;
}

// The cache key covers the request but nothing about this Worker, so a deploy
// that changes prompt assembly or tool grants would otherwise let a retry
// return a receipt produced under the previous contract. Bump this whenever
// either changes.
const RUNTIME_CONTRACT_VERSION = 2;

/** Same request retries reuse a receipt; new replies, authority or contract never do. */
export async function resultCachePath(request: ExecuteRequest): Promise<string> {
  const keyed = { v: RUNTIME_CONTRACT_VERSION, request: canonical(request) };
  return `/.memseek/results/${await sha256(JSON.stringify(keyed))}.json`;
}

export function recallPaths(context: Record<string, string>, workspaceFiles: string[]): string[] {
  return [...new Set([
    ...Object.keys(context).filter((path) => path !== "/.memseek/manifest.json"),
    ...workspaceFiles.filter((path) => path.startsWith("/workspace/") && !path.startsWith("/workspace/recalled/")),
  ])].sort();
}
