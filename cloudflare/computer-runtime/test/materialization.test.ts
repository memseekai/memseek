import { describe, expect, it, vi } from "vitest";
import {
  buildContextView,
  classifyContext,
  renderContextSections,
} from "../src/materialization";
import type { ExecuteRequest } from "../src/protocol";

const SKILL_PATH = "/.memseek/skills/renewal-research/SKILL.md";
const SKILL_DOC =
  "---\nname: renewal-research\ndescription: Work evidence-first.\n---\n\nKeep a risk table.\n";

const FILES: Record<string, string> = {
  "/.memseek/instructions.md": "Analyze renewal evidence.",
  "/.memseek/manifest.json": '{"entity":"acme"}',
  "/.memseek/writeback-schemas.json": "[]",
  [SKILL_PATH]: SKILL_DOC,
};

function request(overrides: Partial<ExecuteRequest> = {}): ExecuteRequest {
  return { context_files: FILES, ...overrides } as unknown as ExecuteRequest;
}

const read = (files: Record<string, string> = FILES) =>
  vi.fn(async (path: string) => files[path]);

describe("classifyContext without a descriptor", () => {
  it("reproduces today's prompt exactly, manifest excluded and schemas inlined", async () => {
    const readFile = read();
    const view = await buildContextView(classifyContext(request()), readFile);
    const sections = renderContextSections(view);
    // What index.ts produced before this change: every context file except the
    // manifest, as `## <path>` sections in sorted order.
    expect(sections[0]).toBe("## /.memseek/instructions.md\n\nAnalyze renewal evidence.");
    expect(sections.some((s) => s.startsWith("## /.memseek/writeback-schemas.json"))).toBe(true);
    expect(sections.some((s) => s.includes("manifest.json"))).toBe(false);
  });

  it("treats a flat skills/NN.md as an inlined reference, not a skill", async () => {
    const files = { "/.memseek/skills/01.md": "legacy inline skill" };
    const view = await buildContextView(classifyContext(request({ context_files: files })), read(files));
    expect(view.skills).toEqual([]);
    expect(view.references.map((f) => f.path)).toEqual(["/.memseek/skills/01.md"]);
  });
});

describe("classifyContext with a descriptor", () => {
  const descriptor = {
    version: 1 as const,
    files: [
      { path: "/.memseek/instructions.md", role: "instructions" as const },
      { path: SKILL_PATH, role: "skill" as const },
      { path: "/.memseek/manifest.json", role: "manifest" as const },
      { path: "/.memseek/writeback-schemas.json", role: "writeback_schemas" as const, prompt: false },
    ],
  };

  it("keeps skill bodies out of the prompt but offers them in the index", async () => {
    const view = await buildContextView(
      classifyContext(request({ materialization: descriptor })),
      read(),
    );
    const prompt = renderContextSections(view).join("\n");
    expect(prompt).toContain("renewal-research");
    expect(prompt).toContain("Work evidence-first.");
    // The procedure itself is on disk until the model asks for it.
    expect(prompt).not.toContain("Keep a risk table");
    expect(view.skills.map((s) => s.name)).toEqual(["renewal-research"]);
    expect(view.hidden).toContain("/.memseek/writeback-schemas.json");
  });

  it("reads exactly the files it needs, exactly once each", async () => {
    const readFile = read();
    await buildContextView(classifyContext(request({ materialization: descriptor })), readFile);
    // The manifest and the hidden schemas are never opened: their bytes are
    // already hashed into the receipt and nothing here needs them.
    expect(readFile.mock.calls.map(([path]) => path).sort()).toEqual([
      "/.memseek/instructions.md",
      SKILL_PATH,
    ]);
  });

  it.each([
    [
      "an unmaterialized path",
      [{ path: "/.memseek/absent.md", role: "reference" as const }],
      "unmaterialized path",
    ],
    [
      "a skill outside the SKILL.md shape",
      [{ path: "/.memseek/instructions.md", role: "skill" as const }],
      "<name>/SKILL.md",
    ],
    [
      "an unknown role",
      [{ path: "/.memseek/instructions.md", role: "secrets" as never }],
      "unknown materialization role",
    ],
    [
      "writeback schemas at a moved path",
      [{ path: "/.memseek/instructions.md", role: "writeback_schemas" as const }],
      "writeback schemas must live at",
    ],
  ])("refuses %s", (_case, files, detail) => {
    expect(() => classifyContext(request({ materialization: { version: 1, files } }))).toThrow(detail);
  });

  it("refuses a descriptor that describes a file differently than the file does", async () => {
    // The descriptor is signed but is not in `immutable_hashes`, so it may not
    // be the source of prompt text about a file the tamper check covers.
    const lying = {
      version: 1 as const,
      files: [
        {
          path: SKILL_PATH,
          role: "skill" as const,
          skill: { name: "renewal-research", description: "Something else entirely." },
        },
      ],
    };
    await expect(
      buildContextView(classifyContext(request({ materialization: lying })), read()),
    ).rejects.toThrow("disagrees with the frontmatter");
  });
});
