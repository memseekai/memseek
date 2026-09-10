import { describe, expect, it, vi } from "vitest";
import { createRecallTool, type RecallWorkspace } from "../src/recall-tool";
import type { ExecuteRequest } from "../src/protocol";

const options = { toolCallId: "test", messages: [] } as never;

function setup(overrides: { policy?: Record<string, unknown>; matches?: unknown[] } = {}) {
  type Match = { path: string; line: number; text: string };
  const fs = {
    find: vi.fn(async (_directory: string, _pattern: string, _options: { limit: number }) => [
      { path: "/workspace/notes.md", type: "file" },
    ]),
    grep: vi.fn(
      async (
        _pattern: string,
        path: string,
        _options: { ignoreCase: boolean; regex: boolean; context: number; limit: number },
      ): Promise<Match[]> =>
        (overrides.matches as Match[] | undefined) ?? [{ path, line: 1, text: "hit" }],
    ),
    mkdir: vi.fn(async (_path: string, _options: { recursive: boolean }) => undefined),
    writeFile: vi.fn(async (_path: string, _content: string) => undefined),
  };
  const request = {
    context_files: { "/.memseek/instructions.md": "", "/.memseek/manifest.json": "" },
    executor: { kind: "agent", context_policy: overrides.policy ?? {} },
  } as unknown as ExecuteRequest;
  return { fs, request, workspace: { fs } as unknown as RecallWorkspace };
}

describe("createRecallTool", () => {
  it("materializes a receipt and returns where it landed", async () => {
    const { fs, request, workspace } = setup();
    const result = await createRecallTool(workspace, request).execute!({ query: "uplift" }, options);
    const path = (result as { materialized_path: string }).materialized_path;
    expect(path).toMatch(/^\/workspace\/recalled\/[0-9a-f]{64}\.json$/);
    expect(fs.mkdir.mock.calls[0][0]).toBe("/workspace/recalled");
    expect(fs.writeFile.mock.calls[0][0]).toBe(path);
  });

  it("searches context and workspace files but never the manifest", async () => {
    const { fs, request, workspace } = setup();
    await createRecallTool(workspace, request).execute!({ query: "uplift" }, options);
    const searched = fs.grep.mock.calls.map(([, path]) => path);
    expect(searched).toContain("/.memseek/instructions.md");
    expect(searched).toContain("/workspace/notes.md");
    expect(searched).not.toContain("/.memseek/manifest.json");
  });

  it("continues past a missing root and rethrows anything else", async () => {
    const { request, workspace, fs } = setup();
    fs.grep.mockImplementation(async (_pattern, path) => {
      if (path === "/.memseek/instructions.md") {
        throw Object.assign(new Error("gone"), { code: "ENOENT" });
      }
      return [{ path, line: 2, text: "found" }];
    });
    const result = await createRecallTool(workspace, request).execute!({ query: "q" }, options);
    expect((result as { matches: unknown[] }).matches).toHaveLength(1);

    fs.grep.mockRejectedValue(Object.assign(new Error("boom"), { code: "EIO" }));
    await expect(
      createRecallTool(workspace, request).execute!({ query: "q" }, options),
    ).rejects.toThrow("boom");
  });

  it("lets a source narrow the policy but never widen it", async () => {
    const { request, workspace, fs } = setup({ policy: { max_recall_hits: 5 } });
    await createRecallTool(workspace, request, { hits: 2 }).execute!({ query: "q" }, options);
    expect(fs.grep.mock.calls[0][2]).toMatchObject({ limit: 2 });

    fs.grep.mockClear();
    await createRecallTool(workspace, request, { hits: 50 }).execute!({ query: "q" }, options);
    expect(fs.grep.mock.calls[0][2]).toMatchObject({ limit: 5 });
  });
});
