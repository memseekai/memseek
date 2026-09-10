import {
  WorkspaceFileStore,
  createDeleteTool,
  createEditTool,
  createExecTool,
  createFindTool,
  createGrepTool,
  createListTool,
  createReadTool,
  createWriteTool,
  type ExecToolOptions,
  type WorkspaceLike,
} from "@cloudflare/computer/tools";
import { tool, type Tool, type ToolSet } from "ai";
import { z } from "zod";
import {
  limitRecallMatches,
  type ExecuteRequest,
  type FilesystemMode,
  type ToolSource,
  type ToolSourceKind,
} from "./protocol";
import { createRecallTool, type RecallWorkspace } from "./recall-tool";
import type { Skill } from "./skills";
import { createSkillTool } from "./skills";
import { installWritebackTools } from "./writeback-tools";

export type BackendName = "worker-javascript" | "container-shell";

export type ToolWorkspace = WorkspaceLike & Partial<ExecToolOptions["workspace"]>;

export type ToolContext = {
  request: ExecuteRequest;
  workspace: ToolWorkspace;
  backend: BackendName;
  skills: Skill[];
  sources: ToolSource[];
};

type ToolProvider<K extends ToolSourceKind> = (
  source: ToolSource & { kind: K },
  context: ToolContext,
  tools: ToolSet,
) => void;

const BACKEND_DESCRIPTION: Record<BackendName, string> = {
  "container-shell": "A network-denied Linux container sharing the durable workspace.",
  "worker-javascript": "A fast network-denied JavaScript runtime sharing the durable workspace.",
};

const FILESYSTEM_MODES: readonly FilesystemMode[] = [
  "read",
  "ls",
  "find",
  "grep",
  "write",
  "edit",
  "delete",
];

// Assembly order, not declaration order: the writeback provider wraps a `write`
// tool the filesystem provider installs, and that must not depend on how the
// catalog YAML happened to be written.
const ORDER: ToolSourceKind[] = [
  "filesystem",
  "exec",
  "view",
  "recall",
  "skill",
  "writeback",
  "mcp_server",
];

// Names a source may only claim through the provider that owns them. A `view`
// named `read` must not be able to shadow the filesystem read.
const OWNED: Record<string, ToolSourceKind> = {
  ...Object.fromEntries(FILESYSTEM_MODES.map((mode) => [mode, "filesystem" as const])),
  exec: "exec",
  recall: "recall",
  skill: "skill",
};

const MAX_VIEW_MATCHES = 50;
const VIEW_EXPOSED_BYTES = 64 * 1024;

export function validateToolset(sources: ToolSource[], request: ExecuteRequest): void {
  const names = new Set<string>();
  for (const source of sources) {
    if (names.has(source.name)) throw new Error(`duplicate tool source: ${source.name}`);
    names.add(source.name);
    if (source.kind === "filesystem") {
      if (!source.modes?.length) throw new Error(`filesystem source ${source.name} declares no modes`);
      for (const mode of source.modes) {
        if (!FILESYSTEM_MODES.includes(mode)) {
          throw new Error(`filesystem source ${source.name} declares unknown mode: ${mode}`);
        }
      }
    }
    if (source.kind === "mcp_server") {
      // Shape first, so a malformed descriptor and a well-formed but
      // unsupported one are distinguishable in a receipt.
      if (!source.url || !source.url.startsWith("https://")) {
        throw new Error(`mcp_server source ${source.name} needs an https url`);
      }
      if (source.allowed_tools && !Array.isArray(source.allowed_tools)) {
        throw new Error(`mcp_server source ${source.name} has invalid allowed_tools`);
      }
    }
    if (source.kind === "view" && source.mode === "live") {
      throw new Error(`view source ${source.name} requests live mode, which needs the tool channel`);
    }
    if (source.kind === "writeback" && source.commit === "staged") {
      throw new Error(`writeback source ${source.name} requests staged commit, which needs the tool channel`);
    }
    if (source.kind === "exec" && !request.computer.capabilities.exec) {
      throw new Error(`toolset declares exec but computer ${request.computer_ref} denies it`);
    }
    if (source.kind === "filesystem" && !request.computer.capabilities.filesystem) {
      throw new Error(`toolset declares filesystem but computer ${request.computer_ref} denies it`);
    }
  }
}

const filesystem: ToolProvider<"filesystem"> = (source, context, tools) => {
  // Compose the granular factories rather than subtracting from `createAITools`.
  // That set grows with the dependency — it appends `publish`, which mints
  // public asset URLs, whenever the workspace has an assets client — so a
  // denylist would grant whatever is added next. An allowlist grants exactly
  // what the catalog declared.
  const store = new WorkspaceFileStore(context.workspace);
  const factories: Record<FilesystemMode, () => Tool> = {
    read: () => createReadTool({ store, maxBytes: 32 * 1024, maxLines: 800 }),
    ls: () => createListTool({ workspace: context.workspace }),
    find: () => createFindTool({ workspace: context.workspace }),
    grep: () => createGrepTool({ workspace: context.workspace }),
    write: () => createWriteTool({ store }),
    edit: () => createEditTool({ store }),
    delete: () => createDeleteTool({ store }),
  };
  for (const mode of new Set(source.modes ?? [])) tools[mode] = factories[mode]();
};

const exec: ToolProvider<"exec"> = (_source, context, tools) => {
  const workspace = context.workspace as ExecToolOptions["workspace"];
  tools.exec = createExecTool({
    workspace,
    defaultBackend: context.backend,
    backends: { [context.backend]: { description: BACKEND_DESCRIPTION[context.backend] } },
  });
};

const recall: ToolProvider<"recall"> = (_source, context, tools) => {
  tools.recall = createRecallTool(context.workspace as unknown as RecallWorkspace, context.request);
};

const writeback: ToolProvider<"writeback"> = (_source, context, tools) => {
  // `installWritebackTools` reads the Computer's own declarations and self-gates
  // on invocation mode, so every writeback source resolves to the same call.
  installWritebackTools(tools, context.request, context.workspace.fs as never);
};

const skill: ToolProvider<"skill"> = (source, context, tools) => {
  const wanted = source.skill_name ?? source.name;
  if (!context.skills.some((candidate) => candidate.name === wanted)) {
    throw new Error(`toolset declares skill ${wanted} but it was not materialized`);
  }
  // Many skill sources produce one tool with many choices, so the tool is built
  // once from everything materialized rather than once per source.
  tools.skill = createSkillTool(context.skills, context.workspace.fs as never);
};

const view: ToolProvider<"view"> = (source, context, tools) => {
  const path = viewPath(source, context);
  tools[`view_${source.name}`] = tool({
    description:
      `${source.description ?? "A declared view over authorized evidence."} `
      + `Searches ${path}; every row there is already-authorized evidence you may cite.`,
    inputSchema: z.object({
      query: z.string().min(1).max(512),
      limit: z.number().int().min(1).max(MAX_VIEW_MATCHES).optional(),
    }),
    execute: async ({ query, limit }: { query: string; limit?: number }) => {
      const workspace = context.workspace as unknown as RecallWorkspace;
      const found = await workspace.fs.grep(query, path, {
        ignoreCase: true,
        regex: false,
        context: 1,
        limit: limit ?? MAX_VIEW_MATCHES,
      });
      const matches = found.map((match) => ({
        line: match.line,
        text: match.text,
        context: match.context ?? [],
      }));
      const exposed = limitRecallMatches(matches, VIEW_EXPOSED_BYTES);
      return {
        view: source.view ?? source.name,
        path,
        matches: exposed,
        truncated: exposed.length < matches.length,
      };
    },
  });
};

/** The materialized rows this view tool searches, proven to be hashed context. */
function viewPath(source: ToolSource, context: ToolContext): string {
  const declared = context.request.materialization?.files.find(
    (file) => file.role === "view" && file.tool === source.name,
  );
  const path = declared?.path;
  if (!path || !(path in context.request.context_files)) {
    throw new Error(`view source ${source.name} has no materialized rows`);
  }
  return path;
}

const mcp_server: ToolProvider<"mcp_server"> = (source) => {
  // Refused rather than ignored. Tools execute in the Agent Durable Object,
  // which has unreviewed egress, while the Computer this run declares refuses
  // network access outright. Running an Agent that is missing a capability its
  // author declared would produce a plausible, under-equipped result; failing
  // here costs nothing and says exactly what is missing.
  throw new Error(
    `mcp_server tool source ${source.name} is declared but not executable in this runtime: `
    + "remote tool calls need the separately reviewed egress gateway",
  );
};

const PROVIDERS: { [K in ToolSourceKind]: ToolProvider<K> } = {
  filesystem,
  exec,
  recall,
  writeback,
  skill,
  view,
  mcp_server,
};

export function assembleTools(context: ToolContext, providers = PROVIDERS): ToolSet {
  validateToolset(context.sources, context.request);
  const tools: ToolSet = {};
  const ordered = [...context.sources].sort(
    (left, right) => ORDER.indexOf(left.kind) - ORDER.indexOf(right.kind),
  );
  for (const source of ordered) {
    const before = new Set(Object.keys(tools));
    const provider = providers[source.kind] as ToolProvider<ToolSourceKind>;
    provider(source, context, tools);
    for (const name of Object.keys(tools)) {
      if (before.has(name)) continue;
      const owner = OWNED[name];
      if (owner !== undefined && owner !== source.kind) {
        throw new Error(`tool source ${source.name} claimed reserved tool name: ${name}`);
      }
    }
  }
  return tools;
}

/**
 * What this runtime granted before toolsets existed, for a Memseek that has not
 * been deployed yet.
 *
 * `definition.tools.includes("computer")` is deliberately still not consulted:
 * it is not consulted today either, so honoring it here would silently disarm a
 * running Agent whose YAML says `tools: [recall]`. A toolset that wants no
 * filesystem declares no filesystem source instead.
 */
export function legacyToolset(request: ExecuteRequest, skills: Skill[]): ToolSource[] {
  const agentTools = request.executor.kind === "agent" ? request.executor.definition.tools : [];
  return [
    {
      name: "filesystem",
      kind: "filesystem",
      modes: [...FILESYSTEM_MODES],
    },
    ...(request.computer.capabilities.exec
      ? [{ name: "shell", kind: "exec" } as ToolSource]
      : []),
    { name: "writeback", kind: "writeback" },
    ...(agentTools.includes("recall") ? [{ name: "recall", kind: "recall" } as ToolSource] : []),
    ...(skills.length ? [{ name: skills[0].name, kind: "skill" } as ToolSource] : []),
  ];
}
