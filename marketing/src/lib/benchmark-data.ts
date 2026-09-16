/* ============================================================
   LongMemEval-S benchmark facts. One dataset, two renderings.

   Every number here comes from the benchmark package reviewed on
   2026-09-11; the guide it tracks is
   https://github.com/memseekai/membukkit/blob/main/docs/guide/benchmarks.md

   The bar rows the page draws are DERIVED from `series` below, not
   restated. The previous site kept a second hand-maintained copy and
   the two drifted: it still carried Zep at 71.2% under the official
   judge, months after Zep republished 90.2% under a GPT-5.4 judge.

   Scope, which the page must keep legible:
     92.6% is 463 of 500 questions, GPT-5.4 reader and distiller,
     official GPT-4o judge. It is a benchmark score on long
     conversations, never a production success rate.
     Competitor entries are historical published configurations, not
     a current leaderboard. Entries judged by something other than
     the official GPT-4o judge are marked `alt` and cannot be read
     against the official ones.
   ============================================================ */

/** How a result was judged. `alt` results are not comparable to the rest. */
export type SeriesKind = 'mem' | 'other' | 'alt';

/** Where the cost figure came from. `chart-estimate` is read off a
    published chart, so it locates a point rather than pricing a run. */
export type CostBasis = 'published' | 'chart-estimate' | 'unreported';

export interface Series {
  key: string;
  name: string;
  /** Chart label; the full name is too wide for a data point. */
  short: string;
  accuracy: number;
  /** USD per answer. null where the configuration never reported one. */
  cost: number | null;
  kind: SeriesKind;
  basis: CostBasis;
  judge: string;
  source?: string;
  reviewed?: string;
}

export const reviewed = "2026-09-11";

export const series: Series[] = [
  {
    key: "mb-gemma",
    name: "MemBukkit · Gemma 4 26B",
    short: "MemBukkit · Gemma 4",
    accuracy: 88.8,
    cost: 0.0004,
    kind: "mem",
    basis: "published",
    judge: "Official GPT-4o",
  },
  {
    key: "mb-mini",
    name: "MemBukkit · GPT-4o-mini",
    short: "MemBukkit · GPT-4o-mini",
    accuracy: 82,
    cost: 0.0005,
    kind: "mem",
    basis: "published",
    judge: "Official GPT-4o",
  },
  {
    key: "mb-gpt54",
    name: "MemBukkit · GPT-5.4",
    short: "MemBukkit · GPT-5.4",
    accuracy: 92.6,
    cost: 0.015,
    kind: "mem",
    basis: "published",
    judge: "Official GPT-4o",
  },
  {
    key: "gemini",
    name: "Gemini 2.5 Pro · full context",
    short: "Gemini 2.5 Pro",
    accuracy: 89.2,
    cost: 0.16,
    kind: "other",
    basis: "published",
    judge: "Independent full-context result",
  },
  {
    key: "full-gpt4o",
    name: "GPT-4o · full context",
    short: "Full-context GPT-4o",
    accuracy: 60.2,
    cost: 0.29,
    kind: "other",
    basis: "published",
    judge: "Official GPT-4o",
  },
  {
    key: "supermemory",
    name: "Supermemory · historical result",
    short: "Supermemory",
    accuracy: 85.2,
    cost: 0.005011872336272725,
    kind: "other",
    basis: "chart-estimate",
    judge: "Official GPT-4o · historical configuration",
  },
  {
    key: "zep",
    name: "Zep · GPT-5.4",
    short: "Zep",
    accuracy: 90.2,
    cost: null,
    kind: "alt",
    basis: "unreported",
    judge: "Alternate: GPT-5.4",
    source: "https://www.getzep.com/research/",
    reviewed: "2026-09-11",
  },
  {
    key: "mem0-cloud",
    name: "Mem0 Cloud · GPT-5",
    short: "Mem0 Cloud",
    accuracy: 94.4,
    cost: 0.011628327142469017,
    kind: "alt",
    basis: "chart-estimate",
    judge: "Alternate: author’s GPT-5",
  },
  {
    key: "hindsight",
    name: "Hindsight",
    short: "Hindsight",
    accuracy: 91.4,
    cost: 0.021092979522563674,
    kind: "alt",
    basis: "chart-estimate",
    judge: "Alternate: GPT-OSS-120B",
  },
  {
    key: "omega",
    name: "OMEGA · GPT-4.1",
    short: "OMEGA",
    accuracy: 95.4,
    cost: null,
    kind: "alt",
    basis: "unreported",
    judge: "Alternate: GPT-4.1 answers and grades",
  },
  {
    key: "mem0-oss",
    name: "Mem0 OSS",
    short: "Mem0 OSS",
    accuracy: 91,
    cost: null,
    kind: "alt",
    basis: "unreported",
    judge: "Alternate: author’s GPT-5",
  },
];

/** The controlled pair: one reader, one ingestion, memory on and off.
    Separate from the 92.6% run and not derived from it. */
export const pairedRun = {
  membukkitAccuracy: 82,
  fullContextAccuracy: 56.4,
  /** 82.0 vs 56.4 is a 58.7% relative fall in errors. Rounded to 59% on the page. */
  relativeErrorReduction: 58.71559633027523,
  contextReductionPercent: 96.8,
  membukkitContextTokens: 3200,
  fullContextTokens: 100000,
} as const;

/** Bar width as a percentage of the track. The scale tops out above the
    highest score so the leading bar does not touch the edge. */
const BAR_SCALE = 0.93;

/** The ranked bar list, derived so it cannot drift from `series`. */
export const rankedRows = [...series]
  .sort((a, b) => b.accuracy - a.accuracy)
  .map((s) => ({
    ...s,
    score: `${s.accuracy.toFixed(1)}%`,
    width: Number((s.accuracy * BAR_SCALE).toFixed(1)),
    /** Official-judge results are the only ones that rank against each other. */
    protocol: s.kind === 'alt' ? ('alternate' as const) : ('official' as const),
  }));

/** Ablations from the benchmark guide. No counterpart in the chart data. */
export const ablations = [
  {
    value: '1.3%',
    label: 'without receipt-named buckets',
    detail: 'Accuracy falls from 80.0%; excluding a matched random set leaves 82.3%.',
  },
] as const;
