import { tool, type Tool } from "ai";
import { z } from "zod";
import { limitRecallMatches, type ExecuteRequest } from "./protocol";
import { sha256 } from "./security";
import { recallPaths } from "./turn-policy";

type FoundEntry = { path: string; type: string };
type GrepMatch = { path: string; line: number; text: string; context?: unknown };

/** The narrow slice of the workspace recall needs, so tests can supply it. */
export type RecallWorkspace = {
  fs: {
    find(directory: string, pattern: string, options: { limit: number }): Promise<FoundEntry[]>;
    grep(
      pattern: string,
      path: string,
      options: { ignoreCase: boolean; regex: boolean; context: number; limit: number },
    ): Promise<GrepMatch[]>;
    mkdir(path: string, options: { recursive: boolean }): Promise<unknown>;
    writeFile(path: string, content: string): Promise<unknown>;
  };
};

export type RecallLimits = { hits?: number; exposed_bytes?: number };

export function boundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  return Number.isSafeInteger(value) && (value as number) >= minimum && (value as number) <= maximum
    ? (value as number)
    : fallback;
}

function parentPath(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

export function createRecallTool(
  workspace: RecallWorkspace,
  request: ExecuteRequest,
  limits: RecallLimits = {},
): Tool {
  const rawPolicy = request.executor.kind === "agent" ? request.executor.context_policy : {};
  const policyHits = boundedInteger(rawPolicy.max_recall_hits, 1, 50, 20);
  const policyBytes = boundedInteger(rawPolicy.max_exposed_bytes, 1024, 262_144, 64 * 1024);
  // A tool source may ask for less than the context policy allows and never
  // for more: the policy is the ceiling, the declaration narrows it.
  const hitLimit = Math.min(policyHits, boundedInteger(limits.hits, 1, 50, policyHits));
  const byteLimit = Math.min(
    policyBytes,
    boundedInteger(limits.exposed_bytes, 1024, 262_144, policyBytes),
  );
  return tool({
    description:
      "Search already-authorized immutable context and durable workspace files, then materialize a bounded receipt for exact opening.",
    inputSchema: z.object({
      query: z.string().min(1).max(512),
    }),
    execute: async ({ query }: { query: string }) => {
      const matches: Array<Record<string, unknown>> = [];
      let workspaceFiles: string[] = [];
      try {
        workspaceFiles = (await workspace.fs.find("/workspace", "**/*", { limit: 2000 }))
          .filter((entry) => entry.type === "file").map((entry) => entry.path);
      } catch (error) {
        if ((error as { code?: string }).code !== "ENOENT") throw error;
      }
      for (const root of recallPaths(request.context_files, workspaceFiles)) {
        let found;
        try {
          found = await workspace.fs.grep(query, root, {
            ignoreCase: true,
            regex: false,
            context: 1,
            limit: hitLimit - matches.length,
          });
        } catch (error) {
          if ((error as { code?: string }).code === "ENOENT") continue;
          throw error;
        }
        for (const match of found) {
          matches.push({
            path: match.path,
            line: match.line,
            text: match.text,
            context: match.context ?? [],
          });
          if (matches.length >= hitLimit) break;
        }
        if (matches.length >= hitLimit) break;
      }
      const exposed = limitRecallMatches(matches, byteLimit);
      const receipt = { query, matches: exposed, truncated: exposed.length < matches.length };
      const path = `/workspace/recalled/${await sha256(query)}.json`;
      await workspace.fs.mkdir(parentPath(path), { recursive: true });
      await workspace.fs.writeFile(path, JSON.stringify(receipt));
      return { ...receipt, materialized_path: path };
    },
  });
}
