/**
 * Shared public benchmark facts for the Astro benchmark surfaces.
 *
 * Keep these values aligned with MemBukkit's current benchmark guide:
 * https://github.com/memseekai/membukkit/blob/main/docs/guide/benchmarks.md
 */
export const longMemEvalFieldRows = [
  {
    name: 'OMEGA + GPT-4.1',
    detail: 'same model answers + grades',
    judge: 'GPT-4.1 answers + grades',
    score: '95.4%',
    width: 88.7,
    tone: 'altg',
    protocol: 'alternate',
  },
  {
    name: 'Mem0 Cloud + GPT-5',
    detail: "author's GPT-5 judge",
    judge: "author's GPT-5 judge",
    score: '94.4%',
    width: 87.7,
    tone: 'altg',
    protocol: 'alternate',
  },
  {
    name: 'MemBukkit + gpt-5.4',
    detail: 'official gpt-4o judge',
    judge: 'official gpt-4o judge',
    score: '92.6%',
    width: 86,
    tone: 'us',
    protocol: 'official',
  },
  {
    name: 'Hindsight',
    detail: 'official prompts · judge model swapped',
    judge: 'GPT-OSS-120B judge',
    score: '91.4%',
    width: 84.9,
    tone: 'altg',
    protocol: 'alternate',
  },
  {
    name: 'Mem0 OSS',
    detail: "author's GPT-5 judge",
    judge: "author's GPT-5 judge",
    score: '91.0%',
    width: 84.6,
    tone: 'altg',
    protocol: 'alternate',
  },
  {
    name: 'Supermemory',
    detail: 'official gpt-4o judge',
    judge: 'official gpt-4o judge',
    score: '85.2%',
    width: 79.2,
    tone: 'other',
    protocol: 'official',
  },
  {
    name: 'Zep',
    detail: 'official gpt-4o judge',
    judge: 'official gpt-4o judge',
    score: '71.2%',
    width: 66.2,
    tone: 'other',
    protocol: 'official',
  },
  {
    name: 'Full-context reading',
    detail: 'no memory system · official judge',
    judge: 'official gpt-4o judge',
    score: '60.2%',
    width: 56,
    tone: 'full',
    protocol: 'official',
  },
] as const;

export const pairedFindings = [
  {
    value: '+25.6 pts',
    label: 'over paired full context',
    detail: '82.0% vs 56.4% with the same reader, official judge, and ingestion.',
  },
  {
    value: '~32× less',
    label: 'answer context',
    detail: '~3.2k tokens read per question instead of roughly 100k.',
  },
  {
    value: '1.3%',
    label: 'without receipt-named buckets',
    detail: 'Accuracy falls from 80.0%; excluding a matched random set leaves 82.3%.',
  },
] as const;
