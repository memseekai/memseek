import type { ExecuteRequest } from "./protocol";

/** Describe the effective contract for this run, after reusable Agent instructions. */
export function executionInstructions(request: ExecuteRequest): string {
  const common = [
    "## Effective execution contract for this turn",
    "The following run-mode rules take precedence over reusable instructions about writeback or asking an operator.",
    `Execution mode: ${request.mode}.`,
    "Return the final MemSeek JSON envelope as your final answer, not as a tool call or a file.",
    `The runtime writes that answer to ${request.output_path}; do not write that file yourself.`,
    "Use tools only while they advance the task. Once you have the evidence and have completed any required writes, stop calling tools and return the answer.",
    "The context shown above is already available to you; do not repeatedly read or recall the same evidence without a specific missing fact.",
  ];
  if (request.mode === "derivation") {
    return [...common,
      "This is a batch derivation, not an interactive invocation.",
      "Return the requested result in value, matching the supplied output schema, with awaiting_input false.",
      "The pipeline's emit step publishes the records from your returned value.",
      "Do not write any files under /outbox, including observations or proposals, even if reusable Agent instructions describe those paths.",
      "You may use /workspace for scratch work. Do not ask the operator a question; report only conclusions supported by the available evidence.",
    ].join("\n");
  }
  const writeback = request.computer.writeback.filter((item) => item.type !== "final_result");
  return [...common,
    "This is a durable invocation. Ask one concrete question with awaiting_input true if an operator decision is required.",
    "Determine whether to pause before writing any writeback files. A paused turn must not leave observations or proposals in /outbox.",
    writeback.length
      ? `On completion, the only permitted writeback declarations are: ${JSON.stringify(writeback)}.`
      : "No observation or proposal writeback is permitted for this invocation.",
    "When instructed to write observations or proposals, use objects with text (string), content (object), and citations (a nonempty array of authorized UUIDs). Observations are JSONL; each proposal is a .json file under its declared directory.",
    "The final envelope's citation_ids must include EVERY citation used in ALL observation and proposal files you write, as well as any citations used in the answer. A writeback citation missing from the final citation_ids is rejected.",
    "Follow /.memseek/writeback-schemas.json when supplied. Each destination schema validates content merged with the candidate's top-level text. Do not add fields the schema forbids. Put narrative facts and proposed commercial terms in text; content may be empty when the schema permits it.",
    "When write_observations_* or write_proposal_* tools are available, use them for writeback. Supply each record with text and only its declared collection fields. Copy the citation_ids returned by these successful tools into your final envelope.",
    "Do not claim a file was written unless its write tool succeeded. Review-required proposals remain drafts.",
  ].join("\n");
}

/** Reserve the last existing model step for final output; never add a step. */
type ToolStep = {
  toolCalls: Array<{ toolName: string; input: unknown }>;
  toolResults: Array<{ toolName: string; output: unknown }>;
  content?: Array<{ type: string; toolName?: string; error?: unknown }>;
};

export function repeatedToolStep(steps: ToolStep[]): boolean {
  if (steps.length < 2) return false;
  const signature = (step: ToolStep) => JSON.stringify({
    calls: step.toolCalls.map(({ toolName, input }) => [toolName, input]),
    results: step.toolResults.map(({ toolName, output }) => [toolName, output]),
    errors: step.content?.filter((part) => part.type === "tool-error")
      .map((part) => [part.toolName, String(part.error)]),
  });
  for (let period = 1; period * 2 <= steps.length; period++) {
    const previous = steps.slice(-period * 2, -period);
    const latest = steps.slice(-period);
    if (latest.every((step, index) => step.toolCalls.length > 0
      && step.toolResults.length + (step.content?.filter((part) => part.type === "tool-error").length ?? 0) === step.toolCalls.length
      && signature(step) === signature(previous[index]))) return true;
  }
  return false;
}

export function finalAnswerStep(stepNumber: number, maxSteps: number, system: string, steps: ToolStep[] = []) {
  const repeated = repeatedToolStep(steps);
  if (stepNumber < maxSteps - 1 && !repeated) return undefined;
  return {
    toolChoice: "none" as const,
    activeTools: [] as string[],
    system: `${system}\n\n${repeated ? "Your tool steps repeated an identical cycle of calls and results without progress." : "This is your last allowed model step."} Tools are now disabled. Return only the final MemSeek JSON envelope using the evidence and successful tool results already available. Do not invent tool results, file writes, citations, operator answers, or skill contents.\nThe entire response must have exactly this outer structure: {"value": {"your_result_fields": "your result"}, "citation_ids": ["an authorized evidence UUID"], "awaiting_input": false}. Replace the example value with your result object and the example UUID with actual authorized citations. If pausing, put the question inside value and set awaiting_input to true. Never return the value object alone, a tool-call object, or a filesystem write object.`,
  };
}
