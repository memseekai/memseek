// A pi extension that offers the run's writeback tools as native tools.
//
// The tool specs and the command that validates a call both come from
// .harness/input.json, so this file knows nothing about any collection. A
// rejected call throws, and pi hands the error text back to the model, which
// is what lets the agent fix a record and call again within the run.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const OUTPUT_LIMIT = 16_000;

function runCommand(command, name, params, signal) {
  return new Promise((resolve, reject) => {
    const child = spawn(command[0], [...command.slice(1), name], {
      stdio: ["pipe", "pipe", "pipe"],
      signal,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, text: (stdout || stderr).slice(0, OUTPUT_LIMIT) }));
    child.stdin.end(JSON.stringify(params ?? {}));
  });
}

export default function (pi) {
  const root = process.env.MEMSEEK_HARNESS_ROOT;
  if (!root) return;
  const input = JSON.parse(readFileSync(join(root, ".harness", "input.json"), "utf8"));
  const command = input.writeback_command ?? [];
  if (!command.length) return;
  for (const tool of input.writeback_tools ?? []) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.input_schema,
      async execute(_toolCallId, params, signal) {
        const { code, text } = await runCommand(command, tool.name, params, signal);
        if (code !== 0) throw new Error(text.trim() || `${tool.name} failed`);
        return { content: [{ type: "text", text: text.trim() }], details: undefined };
      },
    });
  }
}
