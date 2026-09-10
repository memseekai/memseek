import { parseSkillFrontmatter, skillIndex, type Skill } from "./skills";
import { executionInstructions } from "./execution-instructions";
import type {
  ExecuteRequest,
  MaterializedFile,
  MaterializedRole,
} from "./protocol";
import { safeAbsolutePath } from "./security";

export const WRITEBACK_SCHEMAS_PATH = "/.memseek/writeback-schemas.json";
const MANIFEST_PATH = "/.memseek/manifest.json";
const INSTRUCTIONS_PATH = "/.memseek/instructions.md";
const SKILL_PATH = /^\/\.memseek\/skills\/[a-z0-9][a-z0-9-]{0,62}\/SKILL\.md$/;
const ROLES: ReadonlySet<string> = new Set<MaterializedRole>([
  "instructions",
  "skill",
  "reference",
  "view",
  "manifest",
  "writeback_schemas",
]);

export type ClassifiedFile = {
  path: string;
  role: MaterializedRole;
  prompt: boolean;
  declared?: { name: string; description: string };
  tool?: string;
};
export type Classification = { files: ClassifiedFile[]; descriptor: boolean };

export type ContextView = {
  instructions: Array<{ path: string; content: string }>;
  references: Array<{ path: string; content: string }>;
  skills: Skill[];
  /** Materialized, deliberately not in the prompt. */
  hidden: string[];
};

/** Whether a role's bytes belong in the system prompt when nothing says otherwise. */
function defaultPrompt(role: MaterializedRole): boolean {
  // `manifest` has always been skipped and skill bodies are the point of this
  // change; `view` files are searched by their tool, not inlined. Everything
  // else keeps today's behavior, which is to be inlined — including
  // writeback-schemas.json. Changing that is a separate decision.
  return role !== "manifest" && role !== "skill" && role !== "view";
}

/**
 * Decide what each materialized file is for. Pure: no I/O, so the fallback and
 * the descriptor path can both be pinned by tests.
 */
export function classifyContext(request: ExecuteRequest): Classification {
  const paths = Object.keys(request.context_files);
  const descriptor = request.materialization;
  if (!descriptor) {
    return {
      descriptor: false,
      files: paths.sort().map((path) => {
        const role = fallbackRole(path);
        return { path, role, prompt: defaultPrompt(role) };
      }),
    };
  }
  if (descriptor.version !== 1) {
    throw new Error(`unsupported materialization version: ${descriptor.version}`);
  }
  const known = new Set(paths);
  const seen = new Set<string>();
  const files = descriptor.files.map((file) => classifyDeclared(file, known, seen));
  files.sort((left, right) => left.path.localeCompare(right.path));
  return { descriptor: true, files };
}

function classifyDeclared(
  file: MaterializedFile,
  known: Set<string>,
  seen: Set<string>,
): ClassifiedFile {
  if (!ROLES.has(file.role)) throw new Error(`unknown materialization role: ${file.role}`);
  if (!known.has(file.path)) {
    throw new Error(`materialization declares an unmaterialized path: ${file.path}`);
  }
  if (seen.has(file.path)) throw new Error(`materialization declares ${file.path} twice`);
  seen.add(file.path);
  // `safeAbsolutePath` tolerates empty segments, which the workspace's own
  // listing normalizes away while the immutable map does not. Two views of one
  // file that disagree is worth closing here rather than in shared security
  // code other callers depend on.
  if (file.path.includes("//") || !safeAbsolutePath(file.path, ["/.memseek"])) {
    throw new Error(`invalid materialization path: ${file.path}`);
  }
  if (file.role === "skill" && !SKILL_PATH.test(file.path)) {
    throw new Error(`skill must be materialized as <name>/SKILL.md: ${file.path}`);
  }
  if (file.role === "writeback_schemas" && file.path !== WRITEBACK_SCHEMAS_PATH) {
    throw new Error(`writeback schemas must live at ${WRITEBACK_SCHEMAS_PATH}`);
  }
  return {
    path: file.path,
    role: file.role,
    prompt: file.prompt ?? defaultPrompt(file.role),
    ...(file.skill ? { declared: file.skill } : {}),
    ...(file.tool ? { tool: file.tool } : {}),
  };
}

function fallbackRole(path: string): MaterializedRole {
  if (path === MANIFEST_PATH) return "manifest";
  if (path === WRITEBACK_SCHEMAS_PATH) return "writeback_schemas";
  if (path === INSTRUCTIONS_PATH) return "instructions";
  if (SKILL_PATH.test(path)) return "skill";
  // Flat `/.memseek/skills/NN.md` carries no frontmatter and has always been
  // inlined. Do not promote it.
  return "reference";
}

/**
 * Read every classified file exactly once, and hand the result to both the
 * prompt and the skill tool.
 */
export async function buildContextView(
  classified: Classification,
  readFile: (path: string) => Promise<string>,
): Promise<ContextView> {
  const view: ContextView = { instructions: [], references: [], skills: [], hidden: [] };
  for (const file of classified.files) {
    // A file that reaches neither the prompt nor the skill tool is never read:
    // its bytes are already hashed into the receipt, and nothing here needs them.
    if (file.role !== "skill" && !file.prompt) {
      view.hidden.push(file.path);
      continue;
    }
    const content = await readFile(file.path);
    if (file.role === "skill") {
      const skill = parseSkillFrontmatter(file.path, content);
      // The descriptor is signed but is not hashed into `immutable_hashes`, so
      // it may describe a file only in agreement with the file itself.
      if (file.declared && (file.declared.name !== skill.name
        || file.declared.description !== skill.description)) {
        throw new Error(`materialization disagrees with the frontmatter of ${file.path}`);
      }
      view.skills.push(skill);
      continue;
    }
    (file.role === "instructions" ? view.instructions : view.references).push({
      path: file.path,
      content,
    });
  }
  return view;
}

/** The prompt sections, in role order: instructions, the skill index, references. */
export function renderContextSections(view: ContextView): string[] {
  return [
    ...view.instructions.map((file) => `## ${file.path}\n\n${file.content}`),
    ...(view.skills.length ? [skillIndex(view.skills)] : []),
    ...view.references.map((file) => `## ${file.path}\n\n${file.content}`),
  ];
}

function schemaSection(request: ExecuteRequest): string[] {
  if (!request.output_schema) return [];
  const encoded = JSON.stringify(request.output_schema);
  if (encoded.length > 8 * 1024) {
    return ["The \"value\" object is validated against a JSON Schema too large to inline."];
  }
  return [
    `The "value" object MUST validate against this JSON Schema, or the run is rejected:\n${encoded}`,
  ];
}

/**
 * The whole system prompt. Synchronous and pure: the reads happened once, in
 * `buildContextView`, and both the prompt and the skill tool consume that.
 */
export function buildSystemPrompt(view: ContextView, request: ExecuteRequest): string {
  return [
    "You are the exact versioned MemSeek Agent named in the manifest.",
    "Use the Computer filesystem as working memory. Read immutable context before acting.",
    "Use recall to recover buried authorized evidence; open its materialized receipt before citing it.",
    ...(view.skills.length
      ? ["Load a skill with the `skill` tool before following its procedure."]
      : []),
    "You may write only below the writable roots named in the manifest.",
    "Network access is denied unless the manifest explicitly enables it.",
    "End with one JSON object: {\"value\": <object>, \"citation_ids\": [<visible UUIDs>], \"awaiting_input\": <boolean> }.",
    "Set awaiting_input true only when work cannot continue without one concrete user answer.",
    "Never cite an ID absent from the manifest.",
    ...schemaSection(request),
    ...renderContextSections(view),
    // Last, deliberately: this is the override layer, and the final-word
    // position is load-bearing.
    executionInstructions(request),
  ].join("\n\n");
}
