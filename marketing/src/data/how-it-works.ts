/* ============================================================
   The five stages, the responsibilities table and the FAQ.

   Editorial content, kept out of the page so the copy can be
   revised without touching layout. The `visual` on a stage is the
   small diagram beside it; each kind renders differently, which is
   why it is a tagged union rather than a list of rows.
   ============================================================ */

/**
 * A labelled pair. Normally the left half names the input and the right half
 * carries the emphasis, but a closing line inverts that to state its result
 * first, so which half is emphasized is part of the content.
 */
export interface FlowLine {
  left?: string;
  right: string;
  strong?: 'left' | 'right';
}

export type StageVisual =
  | { kind: 'sources'; caption?: string; types: { title: string; detail: string }[]; lines: FlowLine[] }
  | { kind: 'lines'; caption?: string; lines: FlowLine[]; note?: string }
  | { kind: 'brief'; label: string; heading: string; body: string[]; lines: FlowLine[] };

export interface Stage {
  n: string;
  id: string;
  /** Short label for the index at the top of the page. */
  short: string;
  title: string;
  body: string[];
  visual: StageVisual;
}

export const stages: Stage[] = [
  {
    n: '01', id: 'step-1', short: 'Connect',
    title: 'Connect your sources.',
    body: ['Send documents, messages and business records through the API, or work with us to set up the connections your agents need.'],
    visual: {
      kind: 'sources',
      types: [
        { title: 'Documents', detail: 'Policies, contracts, guides' },
        { title: 'Conversations', detail: 'Chats, tickets, handovers' },
        { title: 'Business systems', detail: 'Customers, projects, orders' },
        { title: 'Databases', detail: 'Records and changing state' },
      ],
      lines: [{ left: 'Your sources', right: 'Shared company knowledge ↓' }],
    },
  },
  {
    n: '02', id: 'step-2', short: 'Organise',
    title: 'Connect related facts.',
    body: ['Memseek links information about the same customer, issue or project. Each finding keeps its sources, so you can check where it came from.'],
    visual: {
      kind: 'lines',
      caption: 'Connected knowledge',
      lines: [
        { left: 'Conversations', right: 'Decisions and experience' },
        { left: 'Documents', right: 'Instructions and requirements' },
        { left: 'Business records', right: 'Current facts and status' },
        { left: 'A shared finding', right: 'Sources stay attached', strong: 'left' },
      ],
    },
  },
  {
    n: '03', id: 'step-3', short: 'Maintain',
    title: 'Keep facts current.',
    body: [
      'Repeated complaints, a changed deadline or a resolved issue can trigger a fresh look. You configure when.',
      'Memseek reads the new information alongside what it already knows, then updates the relevant facts.',
    ],
    visual: {
      kind: 'lines',
      caption: 'What can start a refresh?',
      lines: [
        { left: 'Significance', right: 'Enough important evidence' },
        { left: 'Repetition', right: '3 new complaints' },
        { left: 'Timing', right: 'A deadline or quiet moment' },
        { left: 'Change', right: 'An issue is resolved' },
      ],
      note: 'Example conditions. Thresholds and scopes are configurable.',
    },
  },
  {
    n: '04', id: 'step-4', short: 'Serve',
    title: 'Prepare your agent’s next task.',
    body: [
      'Memseek produces a brief: the relevant facts, history and sources for a task.',
      'Your agent uses it to answer a customer, prepare a call or flag an issue. Your workflow controls which actions need review.',
    ],
    visual: {
      kind: 'brief',
      label: 'A brief ready for your agent',
      heading: 'What matters for this task, with sources.',
      body: [
        'Current facts and open commitments.',
        'Relevant history and what changed.',
        'Source messages your agent can inspect.',
      ],
      lines: [{ left: 'Integration', right: 'API · MCP', strong: 'left' }],
    },
  },
  {
    n: '05', id: 'step-5', short: 'Learn',
    title: 'Learn from the result.',
    body: [
      'New information updates the memory. A customer’s confirmation can close an issue; a changed deadline can update the next brief.',
      'Feedback can also lead to proposed instruction changes. Your team reviews them before they affect future tasks.',
    ],
    visual: {
      kind: 'lines',
      caption: 'From a correction to a reviewed improvement',
      lines: [
        { left: 'Feedback', right: 'A missing customer deadline' },
        { left: 'Proposed change', right: 'Include deadlines in future briefs' },
        { left: 'Your team reviews', right: 'Approve the instruction update' },
        { left: 'Next task', right: 'The agent checks the deadline' },
      ],
    },
  },
];

/** Who does what in a managed pilot. Three columns, five rows. */
export const responsibilities = [
  ['Getting started', 'Choose the workflow and authorised sources.', 'Set up the agreed source connections and knowledge updates.'],
  ['Keeping up', 'Maintain your source systems and provide file revisions.', 'Update the useful facts and preserve their sources and history.'],
  ['Helping your agents', 'Connect your AI tool or agree the integration with us.', 'Prepare the facts, history and evidence for each task.'],
  ['Checking results', 'Provide real questions and tell us what a good answer looks like.', 'Check the retrieved information and refine the setup against those questions.'],
  ['Closing the loop', 'Return useful outcomes and approve changes that need review.', 'Investigate recurring gaps and propose improvements to the maintained knowledge and instructions. Check the result after review.'],
] as const;

export const faq = [
  ['What would we use Memseek for?', 'Start with a task that requires gathering information from several places: preparing for an account call, understanding a recurring problem or keeping a project summary current. Memseek maintains the knowledge your AI needs to help with that work.'],
  ['Is Memseek another chatbot?', 'Memseek is the layer that supplies information to AI. It can support a customer-facing agent, an internal assistant, or another AI workflow. Start with what you want that AI to do.'],
  ['What happens when information changes?', 'New records can trigger an update to the relevant knowledge. An old commitment can be replaced, a resolved issue can be closed and useful history can remain available for future tasks.'],
  ['Which sources can we connect?', 'Tell us which systems and file types matter to your use case. We confirm available integrations and any extra setup before agreeing the pilot.'],
  ['What does our team control?', 'You choose the sources and workflow, configure when information should be reviewed, and decide which proposed changes or actions require approval. We agree who may receive the information and the hosting, retention and deletion arrangements before connecting your data.'],
  ['How does Memseek work with our agents?', 'Memseek maintains knowledge and prepares findings for agents your team already runs. Your connected workflows carry out external actions under the permissions and review rules you choose. For a managed setup, we agree which parts we operate with you.'],
  ['What actually changes when Memseek learns?', 'New records update maintained facts, profiles and briefs. Feedback you return from a task can also produce a proposed improvement to the instructions used next time. Your team reviews those changes. The underlying language model is not retrained, and the original evidence stays available.'],
  ['What if the information does not exist in our sources?', 'A missing fact, such as an unconfirmed fix date, stays unknown. Your workflow can flag it or ask the responsible person for an answer. Adding a field to a profile does not supply the missing evidence.'],
  ['Can I try Memseek without a call?', 'Yes. The open-source repository includes a working memory setup. Start it locally, connect your agent through MCP or the API, and follow the included examples. A managed pilot is available if you want us to handle setup and operation.'],
  ['Why does the benchmark mention MemBukkit?', 'Memseek uses an open-source memory engine called MemBukkit. The published results measure that memory configuration, with its code, settings and reproduction recipe. Start with Memseek for the context engine; developers can also use MemBukkit independently.'],
] as const;
