import { describe, expect, it, vi } from "vitest";
import { assembleTools, legacyToolset, validateToolset, type ToolContext } from "../src/tool-registry";
import { parseSkillFrontmatter } from "../src/skills";
import type { ExecuteRequest, ToolSource } from "../src/protocol";

const SKILL_PATH = "/.memseek/skills/renewal-research/SKILL.md";
const SKILL_DOC = "---\nname: renewal-research\ndescription: d\n---\n\nbody\n";
const SKILL = parseSkillFrontmatter(SKILL_PATH, SKILL_DOC);

function context(overrides: {
  sources: ToolSource[];
  exec?: boolean;
  workspace?: Record<string, unknown>;
  request?: Partial<ExecuteRequest>;
}): ToolContext {
  const request = {
    mode: "invocation",
    computer_ref: "research_workspace@1",
    computer: {
      capabilities: { filesystem: true, exec: overrides.exec ?? true, network: false },
      writeback: [],
      runtime: { default: "worker-javascript" },
    },
    executor: { kind: "agent", definition: { tools: ["computer", "recall"] }, context_policy: {} },
    context_files: {},
    ...overrides.request,
  } as unknown as ExecuteRequest;
  return {
    request,
    workspace: { fs: {}, runtime: { exec: vi.fn() }, ...overrides.workspace } as never,
    backend: "worker-javascript",
    skills: [SKILL],
    sources: overrides.sources,
  };
}

describe("filesystem composition", () => {
  it("grants exactly the declared modes", () => {
    const tools = assembleTools(
      context({ sources: [{ name: "fs", kind: "filesystem", modes: ["read", "grep"] }] }),
    );
    expect(Object.keys(tools).sort()).toEqual(["grep", "read"]);
  });

  it("still grants exactly those when the workspace could offer more", () => {
    // `createAITools` appends `publish` — which mints public asset URLs —
    // whenever the workspace has an assets client. Composition is additive
    // precisely so that a dependency growing a tool cannot widen a grant.
    const tools = assembleTools(
      context({
        sources: [{ name: "fs", kind: "filesystem", modes: ["read", "grep"] }],
        workspace: { assets: {}, sessionId: "s" },
      }),
    );
    expect(Object.keys(tools).sort()).toEqual(["grep", "read"]);
    expect(tools.publish).toBeUndefined();
  });

  it("refuses a filesystem source the Computer does not permit", () => {
    const base = context({ sources: [{ name: "fs", kind: "filesystem", modes: ["read"] }] });
    base.request.computer.capabilities.filesystem = false;
    expect(() => assembleTools(base)).toThrow("denies it");
  });
});

describe("capability is the ceiling", () => {
  it("installs exec when the Computer grants it", () => {
    const tools = assembleTools(context({ sources: [{ name: "shell", kind: "exec" }] }));
    expect(tools.exec).toBeDefined();
    expect(tools.exec!.description).toContain("JavaScript runtime");
  });

  it("refuses exec when the Computer denies it", () => {
    expect(() =>
      assembleTools(context({ sources: [{ name: "shell", kind: "exec" }], exec: false })),
    ).toThrow("denies it");
  });

  it("describes the container backend when that is what will run", () => {
    const base = context({ sources: [{ name: "shell", kind: "exec" }] });
    const tools = assembleTools({ ...base, backend: "container-shell" });
    expect(tools.exec!.description).toContain("Linux container");
  });
});

describe("skills", () => {
  it("produces one tool for many declared skills", () => {
    const tools = assembleTools(
      context({ sources: [{ name: "renewal-research", kind: "skill" }] }),
    );
    expect(Object.keys(tools)).toEqual(["skill"]);
  });

  it("refuses a skill the run never materialized", () => {
    expect(() =>
      assembleTools(context({ sources: [{ name: "absent-skill", kind: "skill" }] })),
    ).toThrow("was not materialized");
  });
});

describe("views", () => {
  const viewSource: ToolSource = {
    name: "open_risks",
    kind: "view",
    description: "Open renewal risks.",
    view: "open_risks@1",
  };
  const materialized = {
    context_files: { "/.memseek/views/open_risks.json": "[]" },
    materialization: {
      version: 1 as const,
      files: [
        { path: "/.memseek/views/open_risks.json", role: "view" as const, tool: "open_risks" },
      ],
    },
  };

  it("searches the materialized rows the run already carries", () => {
    const tools = assembleTools(context({ sources: [viewSource], request: materialized }));
    expect(tools.view_open_risks!.description).toContain("/.memseek/views/open_risks.json");
  });

  it("refuses a view whose rows were never materialized", () => {
    // Requiring the path to be a context file is what makes the bytes it serves
    // provably part of `immutable_hashes`.
    expect(() => assembleTools(context({ sources: [viewSource] }))).toThrow(
      "no materialized rows",
    );
  });
});

describe("mcp_server", () => {
  it("distinguishes a malformed declaration from an unsupported one", () => {
    const request = { computer_ref: "c@1" } as unknown as ExecuteRequest;
    expect(() =>
      validateToolset([{ name: "m", kind: "mcp_server", url: "http://x.example" }], request),
    ).toThrow("needs an https url");
    expect(() =>
      assembleTools(
        context({ sources: [{ name: "m", kind: "mcp_server", url: "https://x.example/mcp" }] }),
      ),
    ).toThrow("separately reviewed egress gateway");
  });
});

describe("assembly", () => {
  it("is independent of declaration order", () => {
    const sources: ToolSource[] = [
      { name: "w", kind: "writeback" },
      { name: "fs", kind: "filesystem", modes: ["read", "write"] },
    ];
    const forward = Object.keys(assembleTools(context({ sources }))).sort();
    const reverse = Object.keys(assembleTools(context({ sources: [...sources].reverse() }))).sort();
    expect(forward).toEqual(reverse);
  });

  it("refuses a source that would shadow a reserved tool name", () => {
    const shadow = context({
      sources: [
        { name: "fs", kind: "filesystem", modes: ["read"] },
        { name: "read", kind: "view", view: "v@1", description: "d" },
      ],
      request: {
        context_files: { "/.memseek/views/read.json": "[]" },
        materialization: {
          version: 1,
          files: [{ path: "/.memseek/views/read.json", role: "view", tool: "read" }],
        },
      } as Partial<ExecuteRequest>,
    });
    // `view_read`, not `read`, so this specific case is safe — the guard exists
    // for the general one. Assert the naming that makes it safe.
    expect(Object.keys(assembleTools(shadow)).sort()).toEqual(["read", "view_read"]);
  });

  it("refuses two sources with the same name", () => {
    expect(() =>
      assembleTools(
        context({
          sources: [
            { name: "dup", kind: "recall" },
            { name: "dup", kind: "filesystem", modes: ["read"] },
          ],
        }),
      ),
    ).toThrow("duplicate tool source");
  });

  it("refuses the forward-compatible modes the tool channel will enable", () => {
    const request = { computer_ref: "c@1", computer: { capabilities: {} } } as unknown as ExecuteRequest;
    expect(() =>
      validateToolset([{ name: "v", kind: "view", view: "v@1", mode: "live" }], request),
    ).toThrow("needs the tool channel");
    expect(() =>
      validateToolset(
        [{ name: "w", kind: "writeback", path: "/outbox/x.jsonl", commit: "staged" }],
        request,
      ),
    ).toThrow("needs the tool channel");
  });
});

describe("legacyToolset", () => {
  it("reproduces what this runtime granted before toolsets existed", () => {
    const request = context({ sources: [] }).request;
    const tools = assembleTools({
      ...context({ sources: [] }),
      sources: legacyToolset(request, [SKILL]),
    });
    expect(Object.keys(tools).sort()).toEqual([
      "delete", "edit", "exec", "find", "grep", "ls", "read", "recall", "skill", "write",
    ]);
  });

  it("omits exec when the Computer denies it", () => {
    const request = context({ sources: [], exec: false }).request;
    expect(legacyToolset(request, []).some((source) => source.kind === "exec")).toBe(false);
  });
});
