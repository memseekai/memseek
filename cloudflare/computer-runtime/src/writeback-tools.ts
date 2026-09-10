import { jsonSchema, tool, type ToolSet } from "ai";
import { z } from "zod";
import type { JSONSchema7 } from "json-schema";
import type { ExecuteRequest } from "./protocol";
import { safeRelativePath } from "./security";

type Filesystem = {
  mkdir(path: string, options: { recursive: boolean }): Promise<unknown>;
  writeFile(path: string, content: string): Promise<unknown>;
};

/** Schema-specific helpers use only the Computer's already-declared writeback paths. */
export function installWritebackTools(tools: ToolSet, request: ExecuteRequest, fs: Filesystem): void {
  const encoded = request.context_files["/.memseek/writeback-schemas.json"];
  if (request.mode !== "invocation" || !encoded) return;
  const schemas = JSON.parse(encoded) as Array<{ path: string; collection: string; mode?: string; schema: Record<string, unknown> }>;
  const paths: string[] = [];
  const names: string[] = [];
  for (const [index, declaration] of request.computer.writeback.entries()) {
    const supplied = schemas.find((item) => item.path === declaration.path && item.collection === declaration.collection);
    if (declaration.type === "final_result" || !supplied) continue;
    let recordSchema;
    try {
      recordSchema = z.fromJSONSchema(supplied.schema);
    } catch {
      // Unsupported schema constructs keep the existing generic write path;
      // canonical ingestion always validates the original destination schema.
      continue;
    }
    const candidate = z.strictObject({
      record: recordSchema.describe("The destination record, including its text. Do not add undeclared fields."),
      citations: z.array(z.string()).min(1).describe("Authorized evidence UUIDs. Include all of these in the final envelope's citation_ids."),
      ...(supplied.mode === "event" ? {} : { key: supplied.mode === "keyed" ? z.string() : z.string().optional() }),
    });
    const observations = declaration.type === "observations";
    const name = observations ? `write_observations_${index}` : `write_proposal_${index}`;
    paths.push(declaration.path);
    names.push(name);
    const input = z.object({
      records: z.array(candidate).min(1).max(observations ? 100 : 1),
      ...(observations ? {} : { filename: z.string().describe("A .json filename, without directories.") }),
    });
    tools[name] = tool({
      description: `Write ${observations ? "observations" : "a proposal"} to ${declaration.path} for ${declaration.collection}. ${declaration.review ? "Held as a draft for review." : "Published on successful completion."} Use this tool instead of generic file writes.`,
      inputSchema: jsonSchema<z.infer<typeof input>>(z.toJSONSchema(input, { target: "draft-7" }) as JSONSchema7, {
        validate: (value) => {
          // Some tool-calling models encode nested arrays as JSON strings.
          // Decode once, then apply the identical strict destination schema.
          if (value && typeof value === "object" && "records" in value && typeof value.records === "string") {
            try { value = { ...value, records: JSON.parse(value.records) }; } catch { /* validation reports the invalid value */ }
          }
          const parsed = input.safeParse(value);
          return parsed.success ? { success: true, value: parsed.data } : { success: false, error: parsed.error };
        },
      }),
      execute: async ({ records, filename }) => {
        const allowed = new Set(request.citation_ids);
        const documents = records.map(({ record, citations, key }) => {
          if (citations.some((id) => !allowed.has(id))) throw new Error("writeback citations must be authorized evidence UUIDs");
          if (!record || typeof record !== "object" || !("text" in record) || typeof record.text !== "string") throw new Error("writeback record must include text");
          const { text, ...content } = record;
          return { text, content, citations: [...new Set(citations)], ...(key === undefined ? {} : { key }) };
        });
        if (!observations && (typeof filename !== "string" || !safeRelativePath(filename) || filename.includes("/") || !filename.endsWith(".json"))) throw new Error("proposal filename must be a single .json filename");
        const path = observations ? declaration.path : `${declaration.path}/${filename}`;
        const content = observations ? documents.map((doc) => JSON.stringify(doc)).join("\n") + "\n" : JSON.stringify(documents[0]);
        if (new TextEncoder().encode(content).length > 1024 * 1024) throw new Error("writeback file exceeds 1 MiB");
        await fs.mkdir(path.slice(0, path.lastIndexOf("/")), { recursive: true });
        await fs.writeFile(path, content);
        return { path, records_written: documents.length, citation_ids: [...new Set(documents.flatMap((doc) => doc.citations))] };
      },
    });
  }
  const generic = tools.write;
  if (names.length && generic?.execute) {
    const execute = generic.execute;
    tools.write = {
      ...generic,
      execute: async (input, options) => {
        const path = input && typeof input === "object" && "path" in input ? String(input.path) : "";
        if (paths.some((root) => path === root || path.startsWith(`${root}/`))) {
          return { error: `Use the schema-specific tools ${names.join(", ")} for writeback. Generic write remains available for workspace notes.` };
        }
        return execute(input, options);
      },
    };
  }
}
