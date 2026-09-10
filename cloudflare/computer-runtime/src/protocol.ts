export type RuntimeName = "worker-javascript" | "container";

export interface ComputerDefinition {
  provider: string;
  writable: string[];
  runtime: {
    default: RuntimeName;
    fallback?: RuntimeName;
    fallback_requires?: "explicit_policy";
  };
  capabilities: { filesystem: boolean; exec: boolean; network: boolean };
  writeback: Array<{
    path: string;
    type: "observations" | "maintained_state" | "final_result";
    review: boolean;
    collection?: string;
    record_type?: string;
  }>;
  retention: { workspace_days: number; preserve: string[] };
}

export interface ProgramDefinition {
  runtime: RuntimeName;
  entrypoint: string;
  command?: string[];
  files?: Record<string, string>;
  bundle?: { uri: string; sha256: string };
  capabilities: string[];
}

export interface AgentDefinition {
  model: string;
  // The pre-toolset spelling. Still sent, still honored when no descriptor
  // accompanies it; superseded by `ExecuteRequest.toolset` when one does.
  tools: Array<"computer" | "recall">;
  toolset?: string;
  limits: { max_steps: number; max_wall_s: number; max_output_bytes: number };
}

export type FilesystemMode = "read" | "ls" | "find" | "grep" | "write" | "edit" | "delete";
export type ToolSourceKind =
  | "filesystem"
  | "exec"
  | "recall"
  | "writeback"
  | "skill"
  | "view"
  | "mcp_server";

/**
 * One capability the catalog granted this run, mirroring the Python
 * `ToolSourceDefinition`. Bindings a source does not use are omitted rather
 * than nulled, so absence and "declared empty" never look alike.
 */
export interface ToolSource {
  name: string;
  kind: ToolSourceKind;
  description?: string;
  root?: string;
  modes?: FilesystemMode[];
  path?: string;
  commit?: "outbox" | "staged";
  artifact?: string;
  skill_name?: string;
  view?: string;
  arguments?: Record<string, string | number | boolean>;
  mode?: "snapshot" | "live";
  url?: string;
  allowed_tools?: string[];
}

export interface ToolsetDescriptor {
  /** The authored `name@version`, or null when the surface was desugared. */
  ref: string | null;
  hash?: string | null;
  instructions?: string | null;
  tools: ToolSource[];
}

export type MaterializedRole =
  | "instructions"
  | "skill"
  | "reference"
  | "view"
  | "manifest"
  | "writeback_schemas";

/**
 * What one materialized file is for. `context_files` still carries the bytes —
 * this only says how to treat them, so `immutable_hashes` and the tamper check
 * are unaffected by anything here.
 */
export interface MaterializedFile {
  path: string;
  role: MaterializedRole;
  prompt?: boolean;
  // Advisory only, and cross-checked against the file's own frontmatter: the
  // descriptor is signed but is not in `immutable_hashes`, so it may not be the
  // source of prompt text about a file the tamper check covers.
  skill?: { name: string; description: string };
  tool?: string;
}

export interface Materialization {
  version: 1;
  files: MaterializedFile[];
}

export interface ExecuteRequest {
  mode: "derivation" | "invocation";
  workspace: string;
  entity: string;
  session_key: string;
  parent_session_key?: string;
  task_id: string;
  computer_ref: string;
  computer: ComputerDefinition;
  executor:
    | { kind: "program"; ref: string; definition: ProgramDefinition }
    | {
        kind: "agent";
        ref: string;
        definition: AgentDefinition;
        context_policy_ref: string;
        context_policy: Record<string, unknown>;
        model: { alias: string; targets: string[]; params: Record<string, unknown> };
      };
  input: unknown;
  source_ids: string[];
  citation_ids: string[];
  output_path: string;
  context_files: Record<string, string>;
  // How to treat each `context_files` entry. Absent from older Memseek
  // deployments, in which case the runtime falls back to path conventions.
  materialization?: Materialization;
  // The resolved tool surface. Absent likewise; `legacyToolset` reconstructs
  // what this runtime granted before toolsets existed.
  toolset?: ToolsetDescriptor;
  // The JSON Schema `value` must satisfy. Memseek validates the response
  // against it either way, so an executor that ignores this field simply fails
  // later instead of sooner; a model needs to be told.
  output_schema?: Record<string, unknown>;
}

export interface ExecuteResponse {
  value: unknown;
  citation_ids: string[];
  steps: number;
  awaiting_input: boolean;
  receipt: {
    provider: "cloudflare";
    backend: RuntimeName;
    session_key: string;
    task_id: string;
    executor_ref: string;
    output_path: string;
    output_sha256: string;
    bytes: number;
    commands: Array<Record<string, unknown>>;
    files: Array<Record<string, unknown>>;
    events: Array<{ kind: string; payload: Record<string, unknown> }>;
    outbox: Array<{
      path: string;
      type: "observations" | "maintained_state";
      content: string;
      sha256: string;
      bytes: number;
    }>;
    immutable_hashes: Record<string, string>;
    resumed: boolean;
  };
}

/** Keep the longest recall prefix whose serialized JSON array fits the budget. */
export function limitRecallMatches<T extends Record<string, unknown>>(matches: T[], byteLimit: number): T[] {
  const encoder = new TextEncoder();
  let bytes = 2; // Array brackets; commas are counted between matches.
  let count = 0;
  for (const match of matches) {
    bytes += encoder.encode(JSON.stringify(match)).length + (count ? 1 : 0);
    if (bytes > byteLimit) break;
    count++;
  }
  return count === matches.length ? matches : matches.slice(0, count);
}
