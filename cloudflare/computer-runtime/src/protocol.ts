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
  tools: Array<"computer" | "recall">;
  limits: { max_steps: number; max_wall_s: number; max_output_bytes: number };
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
