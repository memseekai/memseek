/* ============================================================
   The customer week — the four frames the hero demo plays.

   One account over Mon/Wed/Thu/Fri. customer-motion.ts advances
   them on a timer and hands control back to the reader on any
   interaction; under reduced motion the timer never starts and
   the day buttons are the only way through.

   `source` keys into renewal-story's `sources`, so the message on
   a frame and the record behind it cannot drift apart.

   The generator this came from branched on the frame index for
   the eyebrow, the evidence chips and Thursday's handoff. Those
   are fields here instead: a frame carries what it shows.
   ============================================================ */
import type { SourceApp } from './renewal-story';

/** A chip naming a record already in the account's history. */
export interface EvidenceChip {
  app: SourceApp;
  label: string;
}

/** Thursday alone hands the brief to a connected workflow. */
export interface Dispatch {
  app: SourceApp;
  channel: string;
  /** Initial shown on the recipient's avatar. */
  initial: string;
  lead: string;
  body: string;
  receipt: string;
  note: string;
}

export interface Moment {
  /** Short weekday label on the calendar control. */
  day: string;
  date: string;
  label: string;
  /** Key into renewal-story `sources`. */
  source: string;
  /** Two-digit counter shown on the memory card. */
  version: string;
  /** What the brief said before this message arrived. */
  before: string;
  /** Sentence-case label above the card's heading. */
  eyebrow: string;
  /** The card's own header text; it changes once the brief starts updating. */
  summaryLabel: string;
  title: string;
  detail: string;
  evidence: EvidenceChip[];
  count: string;
  /** The retained context and the move it enables. Absent on the frame that
      dispatches instead, so the component never branches on an index. */
  next?: string;
  action?: string;
  dispatch?: Dispatch;
  /** Line under the connector. Friday receives rather than reports. */
  connection: string;
}

const REPORT: EvidenceChip = { app: 'Gmail', label: 'Report' };
const CALL_NOTE: EvidenceChip = { app: 'HubSpot', label: 'Call note' };
const FOLLOW_UP: EvidenceChip = { app: 'Gmail', label: 'Follow-up' };

export const moments: Moment[] = [
  {
    day: 'Mon',
    date: '14 Sep',
    label: 'The first report',
    source: 'first-report',
    version: '01',
    before: 'Renewal call on Friday.',
    eyebrow: 'Problem + deadline',
    summaryLabel: 'Account summary',
    title: 'Maya can’t export her board report.',
    detail: 'Her renewal is Friday. Memseek keeps the problem and the deadline together.',
    evidence: [REPORT],
    count: '1 report connected',
    next: 'Account history retained',
    action: 'Friday renewal · prefers a short email',
    connection: 'Message added to account history',
  },
  {
    day: 'Wed',
    date: '16 Sep',
    label: 'The problem persists',
    source: 'second-report',
    version: '02',
    before: 'Maya can’t export her board report.',
    eyebrow: 'The impact grows',
    summaryLabel: 'Account summary',
    title: 'Her team is now rebuilding it by hand.',
    detail: 'The call note adds the cost of the problem. Memseek keeps it with Maya’s first report.',
    evidence: [REPORT, CALL_NOTE],
    count: '2 reports connected',
    next: 'Second report this week',
    action: 'Failed export → hours of manual work.',
    connection: 'Message added to account history',
  },
  {
    day: 'Thu',
    date: '17 Sep',
    label: 'Now the renewal is blocked',
    source: 'third-report',
    version: '03',
    before: 'Her team is now rebuilding it by hand.',
    eyebrow: 'Third report this week',
    summaryLabel: 'Account summary updated',
    title: 'The export issue now blocks Friday’s renewal.',
    detail: 'Memseek prepares a warning for her account manager, Leo. Your Slack workflow sends it.',
    evidence: [REPORT, CALL_NOTE, FOLLOW_UP],
    count: '3 reports connected',
    dispatch: {
      app: 'Slack',
      channel: '#renewals',
      initial: 'L',
      lead: 'Leo, Maya’s renewal is blocked.',
      body: 'Send her an update before Friday.',
      receipt: 'Delivered',
      note: 'Via your connected Slack workflow',
    },
    connection: 'Message added to account history',
  },
  {
    day: 'Fri',
    date: '18 Sep',
    label: 'The answer changes again',
    source: 'customer-confirmation',
    version: '04',
    before: 'Friday’s renewal is on the line.',
    eyebrow: 'Customer confirmed',
    summaryLabel: 'Account summary updated',
    title: 'Maya confirms the fix. Her account is updated.',
    detail: 'Memseek clears her blocker and keeps the earlier reports and preferences for the next conversation.',
    evidence: [REPORT, CALL_NOTE, FOLLOW_UP],
    count: 'Confirmation remembered',
    next: 'Memseek clears Maya’s blocker',
    action: 'Her renewal call can go ahead.',
    connection: 'Confirmation added to account history',
  },
];

/** Per-frame dwell in ms. Thursday is longest: it is the frame where the
    brief changes and the handoff to Slack is drawn. */
export const dwellMs = [6500, 7500, 9500, 8500];
