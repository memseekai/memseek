import { DurableObject } from "cloudflare:workers";
import {
  type DurableObjectStorageLike,
  getWorkspace,
  type WorkspaceClient,
  type WorkspaceOptions,
  WorkspaceProxy,
  withWorkspace,
} from "@cloudflare/computer";
import {
  CloudflareContainerBackend,
  withWorkspaceContainer,
} from "@cloudflare/computer/backends/container";
import { WorkerJavaScriptBackend } from "@cloudflare/computer/backends/worker-javascript";
import { Agent, getAgentByName } from "agents";
import { generateText, Output, stepCountIs, type ToolSet } from "ai";
import { createWorkersAI } from "workers-ai-provider";
import { z } from "zod";

import { ExecutionDiagnostics } from "./diagnostics";
import { executionInstructions, finalAnswerStep } from "./execution-instructions";
import { resultCachePath, runAgentPhases } from "./turn-policy";
import { assembleTools, legacyToolset } from "./tool-registry";
import { buildContextView, buildSystemPrompt, classifyContext } from "./materialization";

import type { ExecuteRequest, ExecuteResponse, RuntimeName } from "./protocol";
import { safeAbsolutePath, safeRelativePath, sha256, shellQuote, verifySignedBody } from "./security";

export { WorkspaceProxy };

interface Env {
  COMPUTER: DurableObjectNamespace<MemSeekComputer>;
  AGENT: DurableObjectNamespace<MemSeekAgent>;
  LOADER: WorkerLoader;
  AI: Ai;
  MEMSEEK_RUNTIME_SECRET: string;
}

class ComputerBase extends withWorkspaceContainer(class extends DurableObject<Env> {}) {
  readonly containerBackend = new CloudflareContainerBackend({
    container: () => this,
    workspace: { binding: "COMPUTER", id: this.ctx.id.toString() },
    egress: { mode: "none" },
  });
}

function workspaceOptions(self: InstanceType<typeof ComputerBase>): WorkspaceOptions {
  const { ctx, env } = self as unknown as { ctx: DurableObjectState; env: Env };
  return {
    storage: ctx.storage as unknown as DurableObjectStorageLike,
    backends: [
      new WorkerJavaScriptBackend({ loader: env.LOADER, egress: { mode: "none" } }),
      self.containerBackend,
    ],
  };
}

export class MemSeekComputer extends withWorkspace(ComputerBase, workspaceOptions) {
  override fetch(request: Request): Promise<Response> {
    return this.containerBackend.handleFetch(request);
  }
}

type RuntimeEvent = { kind: string; payload: Record<string, unknown> };
type AgentOutcome = {
  value: unknown;
  citation_ids: string[];
  steps: number;
  events?: RuntimeEvent[];
  awaiting_input?: boolean;
};

type SafeGenerationOptions = {
  temperature?: number;
  topP?: number;
  seed?: number;
  stopSequences?: string[];
  frequencyPenalty?: number;
  presencePenalty?: number;
  maxOutputTokens?: number;
};

export class MemSeekAgent extends Agent<Env> {
  async execute(request: ExecuteRequest): Promise<AgentOutcome> {
    const diagnostics = new ExecutionDiagnostics("agent", this.env.MEMSEEK_RUNTIME_SECRET);
    diagnostics.identify(request);
    try {
      return await this.keepAliveWhile(async () => {
        diagnostics.at("agent_workspace");
        const computer = this.env.COMPUTER.get(
          this.env.COMPUTER.idFromName(request.session_key),
        );
        using workspace = await getWorkspace(
          computer as unknown as Parameters<typeof getWorkspace>[0],
        );
        diagnostics.at("model_configuration");
        const target = request.executor.kind === "agent" ? request.executor.model.targets[0] : "";
        const prefixes = ["workers-ai:", "workers_ai:"];
        const prefix = prefixes.find((candidate) => target.startsWith(candidate));
        if (!prefix) {
          throw new Error("Cloudflare runtime accepts only workers-ai model targets");
        }
        const modelName = target.slice(prefix.length);
        if (!modelName.startsWith("@cf/")) throw new Error("invalid Workers AI model target");
        const model = createWorkersAI({ binding: this.env.AI })(modelName);
        diagnostics.at("context_view");
        const classified = classifyContext(request);
        const context = await buildContextView(classified, (path) =>
          workspace.fs.readFile(path, "utf8"));
        diagnostics.at("system_prompt");
        const system = buildSystemPrompt(context, request);
        diagnostics.at("tool_configuration");
        const backend = backendFor(request);
        const sources = request.toolset?.tools ?? legacyToolset(request, context.skills);
        const tools = assembleTools({
          request, workspace, backend, skills: context.skills, sources,
        });
        const maxSteps = request.executor.kind === "agent" ? request.executor.definition.limits.max_steps : 1;
        const generation = request.executor.kind === "agent"
          ? safeGenerationOptions(request.executor.model.params)
          : {};
        const encodedInput = JSON.stringify(request.input);
        // Every request carries the system prompt, the input, AND the full JSON
        // Schema of every tool. On a small context window the tool schemas are
        // easily the largest term, and they are invisible in the request Memseek
        // sent — so measure them and put the breakdown in the receipt.
        const budget = promptBudget(system, encodedInput, tools, request.toolset);
        console.log("prompt budget", JSON.stringify(budget));
        diagnostics.at("model_generation");
        const envelopeOutput = Output.object({ schema: z.object({
          value: z.unknown(), citation_ids: z.array(z.string()), awaiting_input: z.boolean(),
        }) });
        const generate = async (phaseSystem: string, phaseTools: ToolSet, limit: number, offset = 0) => {
          const outcome = await generateText({
          model,
          system: phaseSystem,
          prompt: encodedInput,
          tools: phaseTools,
          ...(limit === 1 ? { output: envelopeOutput } : {}),
          stopWhen: stepCountIs(Math.max(1, limit - 1)),
          prepareStep: ({ stepNumber, steps }) => finalAnswerStep(stepNumber, limit, phaseSystem, steps),
          onStepFinish: (step) => {
            diagnostics.modelStep(
              offset + step.stepNumber, step.finishReason, step.text.length,
              step.toolCalls.map((call) => call.toolName),
              [
                ...step.content.filter((part) => part.type === "tool-error")
                  .map((part) => ({ tool: part.toolName, error: part.error })),
                ...step.toolResults.flatMap((part) => {
                  const output = part.output;
                  return output && typeof output === "object" && "error" in output
                    ? [{ tool: part.toolName, error: output.error }] : [];
                }),
              ],
            );
          },
          ...generation,
          });
          try {
            parseAgentResult(outcome.text, request.citation_ids);
          } catch (error) {
            const message = error instanceof Error ? error.message : "";
            if (outcome.steps.length >= limit || !(message.startsWith("agent final output") || message.startsWith("agent returned no final"))) throw error;
            const formatted = await generateText({
              model, system: phaseSystem, output: envelopeOutput,
              messages: [
                { role: "user", content: encodedInput }, ...outcome.response.messages,
                { role: "user", content: "Return the final MemSeek envelope now. Preserve the evidence and successful tool results; do not claim unperformed writes. Include every authorized citation used in your written observation/proposal files in citation_ids. If this is the decision phase, return the question or execution plan, without claiming writes." },
              ],
              onStepFinish: (step) => diagnostics.modelStep(offset + outcome.steps.length, step.finishReason, step.text.length, [], []),
              ...generation,
            });
            return { text: formatted.text, finishReason: formatted.finishReason, steps: [...outcome.steps, ...formatted.steps] };
          }
          return { text: outcome.text, finishReason: outcome.finishReason, steps: outcome.steps };
        };
        const { outcome, completedSteps } = await runAgentPhases({
          invocation: request.mode === "invocation",
          system, tools, maxSteps, generate,
          parseDecision: (text) => parseAgentResult(text, request.citation_ids),
        });
        diagnostics.at("agent_output_validation");
        const parsed = parseAgentResult(
          outcome.text,
          request.citation_ids,
          ` (finish reason: ${outcome.finishReason}, steps: ${completedSteps.length}/${maxSteps})`,
        );
        diagnostics.at("agent_audit");
        const events: RuntimeEvent[] = [
          {
            kind: "model_request",
            payload: {
              target,
              params: generation,
              input_sha256: await sha256(encodedInput),
              prompt_budget: budget,
            },
          },
        ];
        for (const [index, rawStep] of completedSteps.entries()) {
          const step = rawStep as unknown as {
            finishReason?: unknown;
            usage?: unknown;
            toolCalls?: Array<Record<string, unknown>>;
            toolResults?: Array<Record<string, unknown>>;
          };
          events.push({
            kind: "model_step",
            payload: {
              index,
              finish_reason: step.finishReason ?? null,
              usage: await boundedAuditValue(step.usage),
            },
          });
          for (const call of step.toolCalls ?? []) {
            events.push({
              kind: "tool_call",
              payload: {
                index,
                tool_call_id: call.toolCallId ?? null,
                tool_name: call.toolName ?? null,
                input: await boundedAuditValue(call.input),
              },
            });
          }
          for (const toolResult of step.toolResults ?? []) {
            events.push({
              kind: "tool_result",
              payload: {
                index,
                tool_call_id: toolResult.toolCallId ?? null,
                tool_name: toolResult.toolName ?? null,
                output: await boundedAuditValue(toolResult.output),
              },
            });
          }
        }
        diagnostics.at("agent_complete");
        return { ...parsed, steps: completedSteps.length, events };
      });
    } catch (error) {
      // Log here before Durable Object RPC serialization can erase causes.
      diagnostics.failure(error);
      throw error;
    }
  }
}

async function boundedAuditValue(value: unknown): Promise<unknown> {
  if (value === undefined) return null;
  let encoded: string;
  try {
    encoded = JSON.stringify(value);
  } catch {
    encoded = JSON.stringify(String(value));
  }
  const bytes = new TextEncoder().encode(encoded);
  if (bytes.length <= 32 * 1024) return JSON.parse(encoded) as unknown;
  return { pointer: true, bytes: bytes.length, sha256: await sha256(bytes) };
}

function safeGenerationOptions(params: Record<string, unknown>): SafeGenerationOptions {
  const result: SafeGenerationOptions = {};
  const numeric = (
    source: string,
    target: keyof SafeGenerationOptions,
    minimum: number,
    maximum: number,
  ): void => {
    const value = params[source];
    if (value === undefined) return;
    if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
      throw new Error(`invalid model parameter: ${source}`);
    }
    (result as Record<string, unknown>)[target] = value;
  };
  numeric("temperature", "temperature", 0, 2);
  numeric("top_p", "topP", 0, 1);
  numeric("frequency_penalty", "frequencyPenalty", -2, 2);
  numeric("presence_penalty", "presencePenalty", -2, 2);

  const seed = params.seed;
  if (seed !== undefined) {
    if (!Number.isSafeInteger(seed)) throw new Error("invalid model parameter: seed");
    result.seed = seed as number;
  }
  const maxOutputTokens = params.max_output_tokens;
  if (maxOutputTokens !== undefined) {
    if (!Number.isSafeInteger(maxOutputTokens) || (maxOutputTokens as number) <= 0) {
      throw new Error("invalid model parameter: max_output_tokens");
    }
    result.maxOutputTokens = maxOutputTokens as number;
  }
  const stop = params.stop;
  if (stop !== undefined) {
    const values = typeof stop === "string" ? [stop] : stop;
    if (
      !Array.isArray(values)
      || values.length > 8
      || values.some((value) => typeof value !== "string" || value.length > 512)
    ) {
      throw new Error("invalid model parameter: stop");
    }
    result.stopSequences = values;
  }
  return result;
}

function backendFor(request: ExecuteRequest): "worker-javascript" | "container-shell" {
  const runtime = request.executor.kind === "program"
    ? request.executor.definition.runtime
    : request.computer.runtime.default;
  return runtime === "container" ? "container-shell" : "worker-javascript";
}

// What the model is actually charged for, before it has done anything. Sizes
// are bytes of serialized JSON, which is within a rounding error of tokens/4
// for this material and — unlike a token count — needs no tokenizer.
function promptBudget(
  system: string,
  input: string,
  tools: ToolSet,
  toolset: { ref: string | null } | undefined,
): Record<string, unknown> {
  const perTool: Record<string, number> = {};
  let toolBytes = 0;
  for (const [name, definition] of Object.entries(tools)) {
    const described = (definition as { description?: string }).description ?? "";
    const schema = (definition as { inputSchema?: unknown }).inputSchema;
    let encodedSchema = "";
    try {
      // zod 4 schemas convert natively; an AI-SDK Schema already carries one.
      const asJsonSchema = (schema as { jsonSchema?: unknown })?.jsonSchema;
      encodedSchema = JSON.stringify(
        asJsonSchema ?? (schema ? z.toJSONSchema(schema as z.ZodType) : {}),
      );
    } catch {
      encodedSchema = "";  // unmeasurable, not zero — reported as such below
    }
    const size = name.length + described.length + encodedSchema.length;
    perTool[name] = size;
    toolBytes += size;
  }
  return {
    system_bytes: system.length,
    input_bytes: input.length,
    tool_bytes: toolBytes,
    total_bytes: system.length + input.length + toolBytes,
    // A declared toolset can measure *smaller* than the legacy surface, which
    // nothing here has ever done before. Say which surface produced the number
    // so a step in the series explains itself.
    tools_source: toolset ? "toolset" : "legacy",
    toolset_ref: toolset?.ref ?? null,
    tools: perTool,
  };
}

// Memseek validates `value` against the caller's declared schema and rejects
// the whole run on a mismatch, so withholding the schema asks the model to hit
// an undisclosed target. Stated here, and only when there is one to state.
function parseAgentResult(
  text: string,
  visible: string[],
  diagnostics = "",
): Pick<AgentOutcome, "value" | "citation_ids" | "awaiting_input"> {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  // A model that ends its last step on a tool call returns no text at all.
  // Name that case instead of surfacing a bare JSON SyntaxError.
  if (trimmed === "") {
    throw new Error(
      `agent returned no final text; expected the MemSeek envelope${diagnostics}`,
    );
  }
  let parsed: {
    value?: unknown;
    citation_ids?: unknown;
    awaiting_input?: unknown;
  };
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error("agent final output was not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || !("value" in parsed) || !Array.isArray(parsed.citation_ids)) {
    const hasValue = Boolean(parsed && typeof parsed === "object" && "value" in parsed);
    const hasCitations = Boolean(parsed && Array.isArray(parsed.citation_ids));
    throw new Error(`agent final output does not match the MemSeek envelope (value present: ${hasValue}; citation_ids array: ${hasCitations})`);
  }
  const allowed = new Set(visible);
  const citations = parsed.citation_ids.map(String);
  if (citations.some((value) => !allowed.has(value))) {
    throw new Error("agent widened citation authority");
  }
  if (parsed.awaiting_input !== undefined && typeof parsed.awaiting_input !== "boolean") {
    throw new Error("agent awaiting_input flag must be boolean");
  }
  return {
    value: parsed.value,
    citation_ids: citations,
    awaiting_input: parsed.awaiting_input ?? false,
  };
}

const MAX_REQUEST_BYTES = 12 * 1024 * 1024;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return Response.json({ ok: true, provider: "cloudflare" });
    if (url.pathname !== "/v1/execute") return new Response("not found", { status: 404 });
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
    const declared = Number(request.headers.get("content-length") ?? 0);
    if (declared > MAX_REQUEST_BYTES) return errorResponse("request_too_large", 413);
    const body = new Uint8Array(await request.arrayBuffer());
    if (body.length > MAX_REQUEST_BYTES) return errorResponse("request_too_large", 413);
    if (!env.MEMSEEK_RUNTIME_SECRET) return errorResponse("runtime_secret_missing", 503);
    if (!(await verifySignedBody(body, request.headers, env.MEMSEEK_RUNTIME_SECRET))) {
      return errorResponse("unauthorized", 401);
    }
    const diagnostics = new ExecutionDiagnostics("runtime", env.MEMSEEK_RUNTIME_SECRET);
    try {
      diagnostics.at("parse_request");
      const payload = JSON.parse(new TextDecoder().decode(body)) as ExecuteRequest;
      diagnostics.at("validate_request");
      validateRequest(payload);
      diagnostics.identify(payload);
      const result = await execute(payload, env, diagnostics);
      return Response.json(result);
    } catch (error) {
      return Response.json(diagnostics.failure(error), { status: 422 });
    }
  },
} satisfies ExportedHandler<Env>;

async function execute(
  request: ExecuteRequest, env: Env, diagnostics: ExecutionDiagnostics,
): Promise<ExecuteResponse> {
  diagnostics.at("workspace_open");
  const stub = env.COMPUTER.get(env.COMPUTER.idFromName(request.session_key));
  using workspace = await getWorkspace(stub as unknown as Parameters<typeof getWorkspace>[0]);
  diagnostics.at("fork_materialization");
  await materializeFork(workspace, request, env);
  diagnostics.at("result_cache_read");
  const cachePath = await resultCachePath(request);
  try {
    const cached = await workspace.fs.readFile(cachePath, "utf8");
    const result = JSON.parse(cached) as ExecuteResponse;
    result.receipt.resumed = true;
    return result;
  } catch (error) {
    if ((error as { code?: string }).code !== "ENOENT") throw error;
  }

  diagnostics.at("context_materialization");
  const immutable = await materialize(workspace, request);
  diagnostics.at("snapshot_before");
  const before = await snapshot(workspace, ["/"]);
  const commands: Array<Record<string, unknown>> = [];
  let outcome: AgentOutcome;
  if (request.executor.kind === "program") {
    diagnostics.at("program_execution");
    outcome = await runProgram(workspace, request, commands);
  } else {
    diagnostics.at("agent_execution");
    const agent = await getAgentByName(env.AGENT, `${request.session_key}:${request.executor.ref}`);
    outcome = await agent.execute(request);
  }
  diagnostics.at("immutable_validation");
  const tampered = await restoreAndFindTampering(workspace, immutable);
  if (tampered.length) throw new Error(`immutable files changed: ${tampered.join(", ")}`);
  diagnostics.at("writable_validation");
  const afterExecution = await snapshot(workspace, ["/"]);
  const unauthorized = diffSnapshots(before, afterExecution)
    .map((entry) => String(entry.path))
    .filter((path) => !safeAbsolutePath(path, request.computer.writable));
  if (unauthorized.length) {
    throw new Error(`files changed outside writable roots: ${unauthorized.join(", ")}`);
  }
  diagnostics.at("output_validation");
  const encoded = JSON.stringify(outcome.value);
  const encodedBytes = new TextEncoder().encode(encoded);
  const limit = request.executor.kind === "agent"
    ? request.executor.definition.limits.max_output_bytes
    : 10 * 1024 * 1024;
  if (encodedBytes.length > limit) throw new Error("output byte limit exceeded");
  diagnostics.at("output_write");
  await workspace.fs.mkdir(parentPath(request.output_path), { recursive: true });
  await workspace.fs.writeFile(request.output_path, encoded);
  diagnostics.at("outbox_collection");
  const outbox = await collectOutbox(workspace, request);
  diagnostics.at("snapshot_after");
  const after = await snapshot(workspace, ["/"]);
  const files = diffSnapshots(before, after);
  const outputHash = await sha256(encodedBytes);
  const response: ExecuteResponse = {
    value: outcome.value,
    citation_ids: outcome.citation_ids,
    steps: outcome.steps,
    awaiting_input: outcome.awaiting_input ?? false,
    receipt: {
      provider: "cloudflare",
      backend: request.executor.kind === "program"
        ? request.executor.definition.runtime
        : request.computer.runtime.default,
      session_key: request.session_key,
      task_id: request.task_id,
      executor_ref: request.executor.ref,
      output_path: request.output_path,
      output_sha256: outputHash,
      bytes: encodedBytes.length,
      commands,
      files,
      events: outcome.events ?? [],
      outbox,
      immutable_hashes: Object.fromEntries(
        await Promise.all([...immutable].map(async ([path, value]) => [path, await sha256(value)])),
      ),
      resumed: false,
    },
  };
  diagnostics.at("result_cache_write");
  await workspace.fs.mkdir(parentPath(cachePath), { recursive: true });
  await workspace.fs.writeFile(cachePath, JSON.stringify(response));
  diagnostics.at("complete");
  return response;
}

async function collectOutbox(
  workspace: WorkspaceClient,
  request: ExecuteRequest,
): Promise<Array<{
  path: string;
  type: "observations" | "maintained_state";
  content: string;
  sha256: string;
  bytes: number;
}>> {
  const configured = request.mode === "invocation"
    ? request.computer.writeback.filter((item) => item.type !== "final_result")
    : [];
  const allowed = [request.output_path, ...configured.map((item) => item.path)];
  const files = await listFiles(workspace, "/outbox");
  const unknown = files.filter(
    (path) => !allowed.some((root) => path === root || path.startsWith(`${root}/`)),
  );
  if (unknown.length) throw new Error(`unknown outbox files: ${unknown.join(", ")}`);

  const result: Array<{
    path: string;
    type: "observations" | "maintained_state";
    content: string;
    sha256: string;
    bytes: number;
  }> = [];
  let total = 0;
  for (const declaration of configured) {
    const declaredFiles = files.filter(
      (path) => path === declaration.path || path.startsWith(`${declaration.path}/`),
    );
    if (declaration.type === "observations" && declaredFiles.some(
      (path) => path !== declaration.path,
    )) {
      throw new Error("observations writeback must be one JSONL file");
    }
    for (const path of declaredFiles) {
      if (declaration.type === "maintained_state" && !path.endsWith(".json")) {
        throw new Error(`proposal is not JSON: ${path}`);
      }
      const stat = await workspace.fs.lstat(path);
      if (!stat.isFile || stat.isSymbolicLink) throw new Error(`invalid outbox file: ${path}`);
      total += stat.size;
      if (result.length >= 100 || total > 1024 * 1024) {
        throw new Error("interactive outbox limit exceeded");
      }
      const content = await workspace.fs.readFile(path, "utf8");
      result.push({
        path,
        type: declaration.type as "observations" | "maintained_state",
        content,
        sha256: await sha256(content),
        bytes: stat.size,
      });
    }
  }
  return result;
}

async function listFiles(workspace: WorkspaceClient, root: string): Promise<string[]> {
  const files: string[] = [];
  const pending = [root];
  while (pending.length) {
    const directory = pending.pop()!;
    let entries;
    try {
      entries = await workspace.fs.readdir(directory);
    } catch (error) {
      if ((error as { code?: string }).code === "ENOENT") continue;
      throw error;
    }
    for (const entry of entries) {
      const path = `${directory}/${entry.name}`.replaceAll("//", "/");
      if (entry.isSymbolicLink) throw new Error(`outbox refuses symbolic link: ${path}`);
      if (entry.isDirectory) pending.push(path);
      else files.push(path);
      if (files.length + pending.length > 2_000) throw new Error("outbox entry limit exceeded");
    }
  }
  return files.sort();
}

async function materializeFork(
  workspace: WorkspaceClient,
  request: ExecuteRequest,
  env: Env,
): Promise<void> {
  if (!request.parent_session_key) return;
  const marker = "/.memseek/forked-from";
  try {
    const existing = await workspace.fs.readFile(marker, "utf8");
    if (existing !== request.parent_session_key) throw new Error("fork parent mismatch");
    return;
  } catch (error) {
    if ((error as { code?: string }).code !== "ENOENT") throw error;
  }
  const parentStub = env.COMPUTER.get(env.COMPUTER.idFromName(request.parent_session_key));
  using parent = await getWorkspace(
    parentStub as unknown as Parameters<typeof getWorkspace>[0],
  );
  const counter = { files: 0, bytes: 0 };
  for (const path of request.computer.retention.preserve) {
    await copyPreservedPath(parent, workspace, path, counter);
  }
  await workspace.fs.mkdir(parentPath(marker), { recursive: true });
  await workspace.fs.writeFile(marker, request.parent_session_key);
}

async function copyPreservedPath(
  source: WorkspaceClient,
  target: WorkspaceClient,
  path: string,
  counter: { files: number; bytes: number },
): Promise<void> {
  let stat;
  try {
    stat = await source.fs.lstat(path);
  } catch (error) {
    if ((error as { code?: string }).code === "ENOENT") return;
    throw error;
  }
  if (stat.isSymbolicLink) throw new Error(`fork refuses symbolic link: ${path}`);
  if (stat.isDirectory) {
    await target.fs.mkdir(path, { recursive: true });
    for (const entry of await source.fs.readdir(path)) {
      await copyPreservedPath(source, target, `${path}/${entry.name}`, counter);
    }
    return;
  }
  counter.files += 1;
  counter.bytes += stat.size;
  if (counter.files > 2_000 || counter.bytes > 50 * 1024 * 1024) {
    throw new Error("fork preserve limit exceeded");
  }
  await target.fs.mkdir(parentPath(path), { recursive: true });
  await target.fs.writeFile(path, await source.fs.readFile(path, {}));
}

async function materialize(
  workspace: WorkspaceClient,
  request: ExecuteRequest,
): Promise<Map<string, string>> {
  const immutable = new Map<string, string>();
  for (const [path, content] of Object.entries(request.context_files)) {
    if (!safeAbsolutePath(path, ["/.memseek"])) throw new Error(`invalid context path: ${path}`);
    await writeImmutable(workspace, immutable, path, content);
  }
  const inputPath = `/inputs/${await sha256(request.task_id)}/input.json`;
  await writeImmutable(workspace, immutable, inputPath, JSON.stringify(request.input));
  if (request.executor.kind === "program") {
    if (request.executor.definition.bundle) {
      throw new Error("external Program bundles require an installed artifact resolver");
    }
    const root = "/.memseek/program";
    for (const [relative, content] of Object.entries(request.executor.definition.files ?? {})) {
      if (!safeRelativePath(relative)) throw new Error(`invalid Program path: ${relative}`);
      await writeImmutable(workspace, immutable, `${root}/${relative}`, content);
    }
  }
  const manifest = {
    invocation: {
      mode: request.mode,
      workspace: request.workspace,
      entity: request.entity,
      task_id: request.task_id,
    },
    definitions: { computer: request.computer_ref, executor: request.executor.ref },
    permissions: {
      writable: request.computer.writable,
      capabilities: request.computer.capabilities,
    },
    sources: request.source_ids,
    citations: request.citation_ids,
  };
  await writeImmutable(workspace, immutable, "/.memseek/runtime-manifest.json", JSON.stringify(manifest));
  await workspace.fs.mkdir("/workspace", { recursive: true });
  await workspace.fs.mkdir("/outbox", { recursive: true });
  return immutable;
}

async function writeImmutable(
  workspace: WorkspaceClient,
  immutable: Map<string, string>,
  path: string,
  content: string,
): Promise<void> {
  await workspace.fs.mkdir(parentPath(path), { recursive: true });
  await workspace.fs.writeFile(path, content);
  immutable.set(path, content);
}

async function restoreAndFindTampering(
  workspace: WorkspaceClient,
  immutable: Map<string, string>,
): Promise<string[]> {
  const changed: string[] = [];
  for (const [path, expected] of immutable) {
    let actual: string | undefined;
    try {
      actual = await workspace.fs.readFile(path, "utf8");
    } catch {
      actual = undefined;
    }
    if (actual !== expected) {
      changed.push(path);
      await workspace.fs.mkdir(parentPath(path), { recursive: true });
      await workspace.fs.writeFile(path, expected);
    }
  }
  return changed;
}

async function runProgram(
  workspace: WorkspaceClient,
  request: ExecuteRequest,
  commands: Array<Record<string, unknown>>,
): Promise<AgentOutcome> {
  if (request.executor.kind !== "program") throw new Error("expected Program executor");
  const program = request.executor.definition;
  let source: string;
  let backend: "worker-javascript" | "container-shell";
  if (program.runtime === "worker-javascript") {
    source = program.files?.[program.entrypoint] ?? "";
    backend = "worker-javascript";
  } else {
    if (!program.command?.length) throw new Error("container Program has no command argv");
    source = program.command.map(shellQuote).join(" ");
    backend = "container-shell";
  }
  const started = Date.now();
  // The worker-javascript backend confines code paths to its own root, which is
  // /workspace; /.memseek is deliberately outside every writable root, so naming
  // it as cwd is rejected before the Program runs. The entrypoint is passed as
  // source, not resolved from disk, so the fast path needs no cwd at all. The
  // container backend is not confined that way and still runs beside its bundle.
  using handle = await workspace.runtime.exec(source, {
    backend,
    ...(backend === "container-shell" ? { cwd: "/.memseek/program" } : {}),
    input: backend === "worker-javascript" ? request.input as never : undefined,
    encoding: "utf8",
  });
  const result = await handle.result();
  commands.push({
    backend,
    source_sha256: await sha256(source),
    exit_code: result.exitCode,
    duration_ms: Date.now() - started,
    stdout_sha256: await sha256(result.stdout),
    stderr_sha256: await sha256(result.stderr),
  });
  if (result.exitCode !== 0 || result.status !== "completed") {
    throw new Error(`Program failed with exit code ${result.exitCode}`);
  }
  const value = backend === "worker-javascript"
    ? result.value
    : JSON.parse(await workspace.fs.readFile(request.output_path, "utf8"));
  return { value, citation_ids: request.citation_ids, steps: 1 };
}

type Snapshot = Map<string, string>;

async function snapshot(workspace: WorkspaceClient, roots: string[]): Promise<Snapshot> {
  const values = new Map<string, string>();
  const pending = [...roots];
  let count = 0;
  while (pending.length) {
    const directory = pending.pop()!;
    let entries;
    try {
      entries = await workspace.fs.readdir(directory);
    } catch (error) {
      if ((error as { code?: string }).code === "ENOENT") continue;
      throw error;
    }
    for (const entry of entries) {
      const path = `${directory}/${entry.name}`.replaceAll("//", "/");
      if (entry.isDirectory) pending.push(path);
      else {
        if (++count > 2_000) throw new Error("preserved file count limit exceeded");
        const bytes = new Uint8Array(await new Response(await workspace.fs.readFile(path, {})).arrayBuffer());
        values.set(path, await sha256(bytes));
      }
    }
  }
  return values;
}

function diffSnapshots(before: Snapshot, after: Snapshot): Array<Record<string, unknown>> {
  const paths = new Set([...before.keys(), ...after.keys()]);
  return [...paths].sort().flatMap((path) => {
    const oldHash = before.get(path);
    const newHash = after.get(path);
    if (oldHash === newHash) return [];
    return [{ path, before_sha256: oldHash ?? null, after_sha256: newHash ?? null }];
  });
}

function parentPath(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

function validateRequest(value: ExecuteRequest): void {
  if (!value || typeof value !== "object") throw new Error("invalid request");
  if (!/^[0-9a-f]{64}$/.test(value.session_key)) throw new Error("invalid session key");
  if (value.parent_session_key && !/^[0-9a-f]{64}$/.test(value.parent_session_key)) {
    throw new Error("invalid parent session key");
  }
  if (value.mode !== "derivation" && value.mode !== "invocation") {
    throw new Error("invalid execution mode");
  }
  if (!value.task_id || !value.executor?.ref) throw new Error("missing invocation identity");
  if (!safeAbsolutePath(value.output_path, ["/outbox"])) throw new Error("invalid output path");
  if (!value.computer.capabilities.filesystem) throw new Error("filesystem capability is required");
  if (value.computer.capabilities.network) {
    throw new Error("network-enabled Computers require a separately reviewed egress gateway");
  }
  for (const root of value.computer.writable) {
    if (!safeAbsolutePath(root, ["/workspace", "/outbox"])) throw new Error("invalid writable root");
  }
}

function errorResponse(error: string, status: number): Response {
  return Response.json({ error }, { status });
}
