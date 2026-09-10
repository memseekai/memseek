import { describe, expect, it, vi } from "vitest";
import { createSkillTool, parseSkillFrontmatter, skillIndex } from "../src/skills";

const BODY = "Work evidence-first.\n\n---\n\nKeep a risk table.\n";
const DOCUMENT = `---\nname: renewal-research\ndescription: Work a renewal file evidence-first.\n---\n\n${BODY}`;
const PATH = "/.memseek/skills/renewal-research/SKILL.md";

describe("parseSkillFrontmatter", () => {
  it("separates the offer from the body, keeping the body verbatim", () => {
    const skill = parseSkillFrontmatter(PATH, DOCUMENT);
    expect(skill.name).toBe("renewal-research");
    expect(skill.description).toBe("Work a renewal file evidence-first.");
    // A `---` rule inside the body must survive: only the first one closes.
    expect(skill.body).toBe(BODY);
  });

  it.each([
    ["no frontmatter", "name: x\n", "does not start with frontmatter"],
    ["unterminated", "---\nname: x\n", "unterminated frontmatter"],
    [
      "unknown key",
      "---\nname: a\ndescription: d\nallowed-tools: read\n---\n",
      "unsupported frontmatter key: allowed-tools",
    ],
    ["duplicate key", "---\nname: a\nname: b\ndescription: d\n---\n", "declares name twice"],
    ["missing description", "---\nname: a\n---\n", "declares no description"],
    ["invalid name", "---\nname: Renewal_Research\ndescription: d\n---\n", "invalid name"],
  ])("throws on %s", (_case, document, detail) => {
    expect(() => parseSkillFrontmatter(PATH, document)).toThrow(detail);
  });

  it("refuses a name that disagrees with its directory", () => {
    // The path is what `immutable_hashes` keys on, so a disagreeing name would
    // let one skill be offered under another's identity.
    const document = "---\nname: other-skill\ndescription: d\n---\nbody\n";
    expect(() => parseSkillFrontmatter(PATH, document)).toThrow("which is not its directory");
  });

  it("rejects a description too long to belong in a prompt", () => {
    const document = `---\nname: renewal-research\ndescription: ${"x".repeat(501)}\n---\n`;
    expect(() => parseSkillFrontmatter(PATH, document)).toThrow("exceeds 500 characters");
  });
});

describe("skillIndex", () => {
  it("is empty when nothing is offered", () => {
    expect(skillIndex([])).toBe("");
  });

  it("offers name, description and path, and says the bodies are absent", () => {
    const index = skillIndex([parseSkillFrontmatter(PATH, DOCUMENT)]);
    expect(index).toContain("renewal-research");
    expect(index).toContain("Work a renewal file evidence-first.");
    expect(index).toContain(PATH);
    expect(index).toContain("NOT in your context");
    expect(index).toContain("did not load");
    // The point of the mechanism: the procedure itself is not in the prompt.
    expect(index).not.toContain("Keep a risk table");
  });
});

describe("createSkillTool", () => {
  const setup = () => {
    const skill = parseSkillFrontmatter(PATH, DOCUMENT);
    const fs = { readFile: vi.fn(async () => DOCUMENT) };
    return { skill, fs, tool: createSkillTool([skill], fs) };
  };
  const options = { toolCallId: "test", messages: [] } as never;

  it("constrains the input to the declared names", () => {
    const { tool } = setup();
    const schema = tool.inputSchema as { safeParse(value: unknown): { success: boolean } };
    expect(schema.safeParse({ name: "renewal-research" }).success).toBe(true);
    expect(schema.safeParse({ name: "not-declared" }).success).toBe(false);
  });

  it("reads through the filesystem so the bytes are the hashed bytes", async () => {
    const { fs, tool } = setup();
    const result = await tool.execute!({ name: "renewal-research" }, options);
    expect(fs.readFile.mock.calls).toEqual([[PATH, "utf8"]]);
    expect(result).toMatchObject({ name: "renewal-research", path: PATH });
    expect((result as { content: string }).content).toBe(BODY);
    // Frontmatter is the offer, not part of the procedure.
    expect((result as { content: string }).content).not.toContain("description:");
  });

  it("truncates an oversized body and points at the remainder", async () => {
    const skill = parseSkillFrontmatter(PATH, DOCUMENT);
    const huge = `---\nname: renewal-research\ndescription: d\n---\n${"x".repeat(40 * 1024)}`;
    const fs = { readFile: vi.fn(async () => huge) };
    const result = await createSkillTool([skill], fs).execute!({ name: "renewal-research" }, options);
    expect(result).toMatchObject({ truncated: true });
    expect((result as { hint: string }).hint).toContain(PATH);
  });

  it("throws rather than reporting an error object when the file is unreadable", async () => {
    const skill = parseSkillFrontmatter(PATH, DOCUMENT);
    const fs = { readFile: vi.fn(async () => { throw new Error("ENOENT"); }) };
    await expect(
      createSkillTool([skill], fs).execute!({ name: "renewal-research" }, options),
    ).rejects.toThrow("ENOENT");
  });

  it("refuses to exist with nothing to offer", () => {
    expect(() => createSkillTool([], { readFile: vi.fn() })).toThrow("at least one skill");
  });
});
