import { expect, it, vi } from "vitest";
import { z } from "zod";
import { tool, type ToolSet } from "ai";
import { installWritebackTools } from "../src/writeback-tools";
import type { ExecuteRequest } from "../src/protocol";

const citation = "00000000-0000-4000-8000-000000000001";
function setup() {
  const fs = { mkdir: vi.fn().mockResolvedValue(undefined), writeFile: vi.fn().mockResolvedValue(undefined) };
  const generic = vi.fn().mockResolvedValue({ bytesWritten: 1 });
  const tools: ToolSet = { write: tool({ inputSchema: z.object({ path: z.string(), content: z.string() }), execute: generic }) };
  const schema = { type: "object", required: ["text"], properties: { text: { type: "string" }, kind: { const: "observation" } }, additionalProperties: false };
  const request = {
    mode: "invocation", citation_ids: [citation],
    context_files: { "/.memseek/writeback-schemas.json": JSON.stringify([
      { path: "/outbox/observations.jsonl", collection: "observations@1", mode: "event", schema },
      { path: "/outbox/proposals", collection: "proposals@1", mode: "event", schema },
    ]) },
    computer: { writeback: [
      { type: "observations", path: "/outbox/observations.jsonl", collection: "observations@1", review: false },
      { type: "maintained_state", path: "/outbox/proposals", collection: "proposals@1", review: true },
    ] },
  } as unknown as ExecuteRequest;
  installWritebackTools(tools, request, fs);
  return { tools, fs, generic };
}
const options = { toolCallId: "test", messages: [], context: {} };

it("rejects invented collection fields through the tool input schema", () => {
  const { tools } = setup();
  const schema = tools.write_observations_0.inputSchema as { validate(value: unknown): { success: boolean } };
  expect(schema.validate({ records: [{ record: { text: "fact", uptime: 99.91 }, citations: [citation] }] }).success).toBe(false);
  expect(schema.validate({ records: [{ record: { text: "fact", kind: "observation" }, citations: [citation] }] }).success).toBe(true);
  expect(schema.validate({ records: JSON.stringify([{ record: { text: "fact" }, citations: [citation] }]) }).success).toBe(true);
  expect(schema.validate({ records: JSON.stringify([{ record: { text: "fact", uptime: 99.91 }, citations: [citation] }]) }).success).toBe(false);
  expect(schema.validate({ records: [{ record: { text: "fact" }, citations: [citation], key: "forbidden-event-key" }] }).success).toBe(false);
});

it("writes a valid candidate and reports its exact citations for the final envelope", async () => {
  const { tools, fs } = setup();
  const result = await tools.write_observations_0.execute!({ records: [{ record: { text: "fact", kind: "observation" }, citations: [citation] }] }, options);
  expect(JSON.parse(fs.writeFile.mock.calls[0][1])).toEqual({ text: "fact", content: { kind: "observation" }, citations: [citation] });
  expect(result).toEqual({ path: "/outbox/observations.jsonl", records_written: 1, citation_ids: [citation] });
});

it("rejects widened citations and proposal traversal before writing", async () => {
  const { tools, fs } = setup();
  await expect(tools.write_observations_0.execute!({ records: [{ record: { text: "fact" }, citations: ["unknown"] }] }, options)).rejects.toThrow("authorized");
  await expect(tools.write_proposal_1.execute!({ filename: "../elsewhere.json", records: [{ record: { text: "proposal" }, citations: [citation] }] }, options)).rejects.toThrow("filename");
  expect(fs.writeFile).not.toHaveBeenCalled();
});

it("directs generic writeback to typed tools while preserving scratch writes", async () => {
  const { tools, generic } = setup();
  expect(await tools.write.execute!({ path: "/outbox/observations.jsonl", content: "bad" }, options)).toHaveProperty("error");
  expect(generic).not.toHaveBeenCalled();
  await tools.write.execute!({ path: "/workspace/notes", content: "notes" }, options);
  expect(generic).toHaveBeenCalledOnce();
});
