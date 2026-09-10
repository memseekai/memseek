import { tool, type Tool } from "ai";
import { z } from "zod";

export type Skill = { name: string; description: string; path: string; body: string };

type SkillFilesystem = {
  readFile(path: string, encoding: "utf8"): Promise<string>;
};

const SKILL_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;
// Deliberately closed. The Agent SDK's SKILL.md also carries `allowed-tools`,
// which *grants capability*; silently ignoring a key that grants capability is
// a privilege bug, so an unknown key fails loudly here instead.
const RECOGNIZED_KEYS = new Set(["name", "description"]);
const MAX_DESCRIPTION_CHARS = 500;
// The same ceiling the read tool applies, for the same reason.
const MAX_SKILL_BYTES = 32 * 1024;

/** Parse one SKILL.md into its offer (name, description) and its body. */
export function parseSkillFrontmatter(path: string, content: string): Skill {
  if (!content.startsWith("---\n")) {
    throw new Error(`skill ${path} does not start with frontmatter`);
  }
  const rest = content.slice(4);
  const end = rest.search(/^---[ \t]*$/m);
  if (end === -1) throw new Error(`skill ${path} has unterminated frontmatter`);
  const header = rest.slice(0, end);
  // Drop the closing delimiter and the blank line convention puts after it.
  // Only newlines: indentation inside the body is content.
  const body = rest.slice(end).replace(/^---[ \t]*\r?\n?/, "").replace(/^(?:\r?\n)+/, "");
  const fields = new Map<string, string>();
  for (const raw of header.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf(":");
    if (separator === -1) throw new Error(`skill ${path} has a malformed frontmatter line`);
    const key = line.slice(0, separator).trim();
    if (!RECOGNIZED_KEYS.has(key)) {
      throw new Error(`skill ${path} declares unsupported frontmatter key: ${key}`);
    }
    if (fields.has(key)) throw new Error(`skill ${path} declares ${key} twice`);
    fields.set(key, line.slice(separator + 1).trim());
  }
  const name = fields.get("name");
  const description = fields.get("description");
  if (!name) throw new Error(`skill ${path} declares no name`);
  if (!description) throw new Error(`skill ${path} declares no description`);
  if (!SKILL_NAME.test(name)) throw new Error(`skill ${path} declares invalid name: ${name}`);
  if (description.length > MAX_DESCRIPTION_CHARS) {
    throw new Error(`skill ${path} description exceeds ${MAX_DESCRIPTION_CHARS} characters`);
  }
  // The path is the authority: it is what `immutable_hashes` keys on. A
  // frontmatter name that disagreed would let one skill be offered under
  // another's identity.
  const segments = path.split("/");
  const directory = segments[segments.length - 2];
  if (directory !== name) {
    throw new Error(`skill ${path} declares name ${name}, which is not its directory`);
  }
  return { name, description, path, body };
}

/** What the model sees about skills it has not loaded. */
export function skillIndex(skills: Skill[]): string {
  if (!skills.length) return "";
  return [
    "## Skills",
    "These procedures are available but are NOT in your context. Call the `skill` tool with "
      + "the exact name to read one. Do not claim to have followed a skill you did not load.",
    ...skills.map((skill) => `- ${skill.name} — ${skill.description} (${skill.path})`),
  ].join("\n");
}

export function createSkillTool(skills: Skill[], fs: SkillFilesystem): Tool {
  if (!skills.length) throw new Error("skill tool requires at least one skill");
  const names = skills.map((skill) => skill.name);
  const declared = new Map(skills.map((skill) => [skill.name, skill]));
  return tool({
    description:
      "Load the full procedure for one declared skill. The skill index in your instructions "
      + "lists each skill's name and when it applies; the procedures themselves are not in "
      + "your context until you load them. Load a skill before following it.",
    inputSchema: z.object({
      name: z.enum(names).describe("Exact skill name from the skill index."),
    }),
    execute: async ({ name }: { name: string }) => {
      const skill = declared.get(name);
      if (!skill) throw new Error(`unknown skill: ${name}`);
      // Read through the filesystem rather than serving the copy parsed at
      // assembly time, so the bytes the model receives are the bytes that
      // `restoreAndFindTampering` protects.
      const parsed = parseSkillFrontmatter(skill.path, await fs.readFile(skill.path, "utf8"));
      const bytes = new TextEncoder().encode(parsed.body).length;
      const truncated = bytes > MAX_SKILL_BYTES;
      return {
        name,
        path: skill.path,
        bytes,
        content: truncated ? parsed.body.slice(0, MAX_SKILL_BYTES) : parsed.body,
        ...(truncated
          ? { truncated: true, hint: `Use read on ${skill.path} for the remainder.` }
          : {}),
      };
    },
  });
}
