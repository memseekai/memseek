/* ============================================================
   The renewal story — the illustrative case the homepage plays.

   Six stages, scrolled through one pinned board. `phase` is the
   state machine the whole scene is a projection of: the graph, the
   counter, the meter and the four source tiles all read from the
   active stage rather than holding state of their own.

   Every person here is fictional and no company is named. Maya is
   the customer, Leo her account owner, Nina support, Sam product.
   The scores are example scheduling inputs, not risk probabilities.
   Editorial copy lives here on purpose: change the words in this
   file, not in a component.
   ============================================================ */

/** The four apps the org graph names, in the order it draws them. */
export type SourceApp = 'Gmail' | 'HubSpot' | 'Slack' | 'Linear';

/** The six stages, in order. sticky-story.ts drives the scene off this. */
export type StagePhase = 'notice' | 'connect' | 'prepare' | 'escalate' | 'act' | 'learn';

/** One record in the evidence dialogs. Only the messages carry an app. */
export interface SourceRecord {
  label: string;
  group: string;
  /** ISO timestamp, the sort key within a dialog. */
  at: string;
  text: string;
  app?: SourceApp;
  author?: string;
  role?: string;
  channel?: string;
  displayTime?: string;
  account?: string;
  /** Example scheduling weight. Not a probability. */
  importance?: number;
}

export interface Stage {
  label: string;
  when: string;
  title: string;
  body: string;
  /** What the Memseek node reads at this stage. */
  engine: string;
  /** What the result node reads at this stage. */
  outcome: string;
  /** One line per source tile: Gmail, HubSpot, Slack, Linear. */
  statuses: [string, string, string, string];
  /** Keys into `sources`, opened by the stage's evidence dialog. */
  sources: string[];
  phase: StagePhase;
  /** Recipient chips as [human, humanNote, agent, agentNote]. */
  people: [string, string, string, string];
  /** The highlighted message as [origin, excerpt]. */
  focus: [string, string];
}

/** The panel under the graph: what changed, and the receipt for it. */
export interface Scene {
  title: string;
  body: string;
  focus: [string, string];
  from: string;
  to: string;
  note: string;
}

/** Scope note shown with the case. Keep it visible; the case is illustrative. */
export const scope = "Illustrative scores and configured thresholds. Customer briefs stay scoped to their account; the internal product brief uses authorized feedback grouped under report exports. Importance schedules a refresh; it is not a risk probability.";

/** Importance at which a refresh is scheduled. An example input. */
export const threshold = 9;

export const accountReports = ["first-report","second-report","third-report"];

export const productReports = ["first-report","second-report","third-report","other-report-02","other-report-03","other-report-04"];

export const sources: Record<string, SourceRecord> = {
  "account-record": {
    label: "Existing account context",
    group: "Business systems",
    at: "2026-09-11T15:00:00Z",
    text: "Account owner: Leo. Renewal call on Friday, September 18. The contact prefers concise written updates before calls. No export issue recorded yet.",
  },
  "first-report": {
    label: "Gmail · Maya’s first report",
    app: "Gmail",
    author: "Maya",
    role: "Customer",
    channel: "Report exports",
    displayTime: "Mon · 09:00",
    group: "Customers",
    account: "account-01",
    importance: 2,
    at: "2026-09-14T09:00:00Z",
    text: "The board report export keeps timing out. Can you take a look?",
  },
  "second-report": {
    label: "HubSpot · Leo’s call note",
    app: "HubSpot",
    author: "Leo",
    role: "Account owner",
    channel: "Call note",
    displayTime: "Wed · 10:00",
    group: "Your team",
    account: "account-01",
    importance: 3,
    at: "2026-09-16T10:00:00Z",
    text: "Still failing. Maya’s team has two people rebuilding the board report by hand.",
  },
  "third-report": {
    label: "Gmail · Maya’s third report",
    app: "Gmail",
    author: "Maya",
    role: "Customer",
    channel: "Re: Report exports",
    displayTime: "Thu · 09:00",
    group: "Customers",
    account: "account-01",
    importance: 4,
    at: "2026-09-17T09:00:00Z",
    text: "I’ve raised this three times. We can’t renew on Friday if exports still fail.",
  },
  "account-brief": {
    label: "Memseek · updated account brief",
    group: "Memseek",
    at: "2026-09-17T09:20:00Z",
    text: "Renewal is blocked by unresolved export failures: three reports this week. Send a concise written update before Friday’s renewal call. Retain the contact’s preference for written updates.",
  },
  "team-chat": {
    label: "Slack · Nina in #customer-feedback",
    app: "Slack",
    author: "Nina",
    role: "Support lead",
    channel: "#customer-feedback",
    displayTime: "Thu · 09:15",
    group: "Your team",
    at: "2026-09-17T09:15:00Z",
    text: "Same export timeout in three other accounts. I’ve linked their reports. Could this be one issue?",
  },
  "other-report-02": {
    label: "Related feedback · customer 2",
    group: "Customers",
    account: "account-02",
    at: "2026-09-15T09:00:00Z",
    text: "Our report download fails before it finishes.",
  },
  "other-report-03": {
    label: "Related feedback · customer 3",
    group: "Customers",
    account: "account-03",
    at: "2026-09-15T10:00:00Z",
    text: "We cannot export the report for our weekly review.",
  },
  "other-report-04": {
    label: "Related feedback · customer 4",
    group: "Customers",
    account: "account-04",
    at: "2026-09-16T14:00:00Z",
    text: "The export fails. We are copying the figures manually.",
  },
  "agent-followup": {
    label: "Gmail · your support agent’s follow-up",
    app: "Gmail",
    author: "Your support agent",
    role: "To Maya · sent",
    channel: "Re: Report exports",
    displayTime: "Thu · 10:15",
    group: "Your agents",
    at: "2026-09-17T10:15:00Z",
    text: "Hi Maya — I’ve linked all three reports and flagged this with the exports team. I’ll send an update before tomorrow’s renewal call.",
  },
  "product-brief": {
    label: "Memseek · internal product brief",
    group: "Memseek",
    at: "2026-09-17T09:35:00Z",
    text: "Report exports: six distinct reports across four customers. One customer explicitly says the issue blocks renewal. Workarounds require manual spreadsheet work. Root cause unconfirmed. Links to the six reports are attached.",
  },
  "escalation-brief": {
    label: "Memseek · escalation prepared automatically",
    group: "Memseek",
    at: "2026-09-17T09:36:00Z",
    text: "Renewal blocker flagged. Owner: Leo, from the account record. Next step: send Maya a concise written update before Friday’s renewal call. Attach the three account reports and the authorized internal product brief. This committed escalation record is ready for the connected Slack workflow.",
  },
  "renewal-alert": {
    label: "Slack · Memseek escalation to #renewals",
    app: "Slack",
    author: "Memseek",
    role: "Via connected workflow",
    channel: "#renewals",
    displayTime: "Thu · 09:40",
    group: "Connected Slack workflow",
    at: "2026-09-17T09:40:00Z",
    text: "Leo — Maya’s renewal is blocked by export failures. Please send her a short update before Friday’s call. Product brief attached: 4 customers, 6 reports.",
  },
  "team-decision": {
    label: "Linear · Sam approves EXP-247",
    group: "Your team",
    at: "2026-09-17T10:00:00Z",
    text: "Prioritize EXP-247, report export timeouts, for investigation before Friday. The exports team owns the fix.",
  },
  "release": {
    label: "Linear · EXP-247 fix shipped",
    group: "Documents",
    at: "2026-09-18T09:00:00Z",
    text: "An export fix has shipped. Customer verification is still required.",
  },
  "customer-confirmation": {
    label: "Gmail · Maya confirms the fix",
    app: "Gmail",
    author: "Maya",
    role: "Customer",
    channel: "Re: Report exports",
    displayTime: "Fri · 10:00",
    group: "Customers",
    at: "2026-09-18T10:00:00Z",
    text: "Export worked this morning. Thanks for staying on it — let’s keep the renewal call.",
  },
  "latest-state": {
    label: "Memseek · current state after the fix",
    group: "Memseek",
    at: "2026-09-18T10:05:00Z",
    text: "Export blocker resolved for the first customer; renewal is not yet signed. Product fix shipped, with one of four customers confirming. Ask the other three to verify. Preserve existing contact preferences and the original evidence.",
  },
};

export const stages: Stage[] = [
  {
    label: "The complaint",
    when: "Monday",
    title: "Her board report is broken. Her renewal is on Friday.",
    body: "Maya needs the export for her board report. Leo has her renewal call on Friday. The complaint is in an email; the deadline is in the CRM. Memseek connects both to the same account.",
    engine: "Export issue + Friday renewal",
    outcome: "Account summary ready",
    statuses: [
      "“The board report export keeps timing out.”",
      "Renewal: Fri, 18 Sept. Prefers short emails.",
      "Other reports not connected yet.",
      "No product issue assigned yet.",
    ],
    sources: ["first-report","account-record"],
    phase: "notice",
    people: [
      "Leo · sales",
      "Renewal this Friday.",
      "Support agent",
      "Written updates first.",
    ],
    focus: [
      "Gmail · Maya",
      "“The board report export keeps timing out.”",
    ],
  },
  {
    label: "Renewal at risk",
    when: "Wednesday → Thursday",
    title: "Three reports later, Maya says she can’t renew.",
    body: "Leo’s call note adds the cost: two people are rebuilding the report by hand. Then Maya makes the stakes explicit. Memseek carries the earlier reports forward and updates the account: this is now blocking renewal.",
    engine: "3 reports. Renewal blocked.",
    outcome: "Maya’s renewal marked as blocked",
    statuses: [
      "“We can’t renew on Friday if exports still fail.”",
      "“two people rebuilding the board report by hand.”",
      "Other reports not connected yet.",
      "No product issue assigned yet.",
    ],
    sources: ["first-report","second-report","third-report"],
    phase: "connect",
    people: [
      "Leo · sales",
      "Renewal blocked.",
      "Support agent",
      "3 reports attached.",
    ],
    focus: [
      "Gmail · Maya",
      "“We can’t renew on Friday if exports still fail.”",
    ],
  },
  {
    label: "Wider impact",
    when: "Thursday · 09:15",
    title: "Four customers. One product decision.",
    body: "Nina has seen the same timeout in three other accounts. Memseek connects six reports into a product brief. Sam can see the affected customers and the renewal deadline before deciding what to prioritize.",
    engine: "6 reports across 4 accounts",
    outcome: "Product summary ready",
    statuses: [
      "“We can’t renew on Friday if exports still fail.”",
      "“two people rebuilding the board report by hand.”",
      "“Same export timeout in three other accounts.”",
      "No product issue assigned yet.",
    ],
    sources: ["account-record","first-report","second-report","third-report","team-chat","other-report-02","other-report-03","other-report-04","account-brief","product-brief"],
    phase: "prepare",
    people: [
      "Sam · product",
      "4 accounts affected.",
      "Support agent",
      "Maya cannot renew.",
    ],
    focus: [
      "Slack · Nina",
      "“Same export timeout in three other accounts.”",
    ],
  },
  {
    label: "Leo alerted",
    when: "Thursday · 09:40",
    title: "Leo gets the warning before the call.",
    body: "Memseek flags Maya’s renewal as blocked, sets Leo’s follow-up deadline and prepares the evidence. His connected Slack workflow delivers the escalation: update Maya before Friday, with the product brief attached.",
    engine: "Escalation + evidence prepared",
    outcome: "Renewal warning sent in Slack",
    statuses: [
      "“We can’t renew on Friday if exports still fail.”",
      "“two people rebuilding the board report by hand.”",
      "“Same export timeout in three other accounts.”",
      "No product issue assigned yet.",
    ],
    sources: ["account-record","first-report","second-report","third-report","team-chat","account-brief","product-brief","escalation-brief","renewal-alert"],
    phase: "escalate",
    people: [
      "Leo · Slack",
      "Update before Friday.",
      "Support agent",
      "Reply deadline set.",
    ],
    focus: [
      "Slack · Nina",
      "“Same export timeout in three other accounts.”",
    ],
  },
  {
    label: "Fix prioritized",
    when: "Thursday · 10:00–10:15",
    title: "Sam prioritizes the fix. Maya gets an answer.",
    body: "With six reports and a blocked renewal in the brief, Sam assigns the issue to the exports team. The support agent writes to Maya, using her preference for a short email. The account response and the product decision move together.",
    engine: "Customer update + product summary",
    outcome: "Fix assigned. Reply sent.",
    statuses: [
      "“We can’t renew on Friday if exports still fail.”",
      "“two people rebuilding the board report by hand.”",
      "“Same export timeout in three other accounts.”",
      "“The exports team owns the fix.”",
    ],
    sources: ["third-report","second-report","team-chat","escalation-brief","renewal-alert","agent-followup","product-brief","team-decision"],
    phase: "act",
    people: [
      "Sam · Linear",
      "Exports team assigned.",
      "Agent · Gmail",
      "Update sent to Maya.",
    ],
    focus: [
      "Linear · Sam",
      "“The exports team owns the fix.”",
    ],
  },
  {
    label: "Customer confirms",
    when: "Friday · 10:00",
    title: "Maya confirms the fix. Three customers still need a check.",
    body: "The fix has shipped, but that doesn’t resolve every account by itself. Memseek clears Maya’s blocker after her confirmation, keeps the other three follow-ups open and retains the history for the next conversation.",
    engine: "Maya confirmed. 3 customers still to check.",
    outcome: "Maya cleared. 3 follow-ups open.",
    statuses: [
      "“Export worked this morning.”",
      "Renewal: Fri, 18 Sept. Prefers short emails.",
      "“Same export timeout in three other accounts.”",
      "“An export fix has shipped.”",
    ],
    sources: ["release","customer-confirmation","account-record","team-chat","latest-state"],
    phase: "learn",
    people: [
      "Leo · sales",
      "Renewal call goes ahead.",
      "Support agent",
      "3 follow-ups stay open.",
    ],
    focus: [
      "Gmail · Maya",
      "“Export worked this morning.”",
    ],
  },
];

export const scenes: Scene[] = [
  {
    title: "Maya reports a broken export.",
    body: "Leo, her account manager, keeps the renewal date in HubSpot. Memseek links it to Maya’s complaint.",
    focus: [
      "Gmail · Maya",
      "“The board report export keeps timing out.”",
    ],
    from: "Customer email + CRM note",
    to: "Account summary ready",
    note: "The export issue and Friday’s renewal are linked.",
  },
  {
    title: "Three complaints. Renewal is blocked.",
    body: "Three reports show the cost: manual work and a blocked renewal. Memseek updates Maya’s account.",
    focus: [
      "Gmail · Maya",
      "“We can’t renew on Friday if exports still fail.”",
    ],
    from: "3 reports this week",
    to: "Maya’s renewal marked as blocked",
    note: "Failed export → manual work → blocked renewal. Each report adds weight.",
  },
  {
    title: "Three more customers report the problem.",
    body: "Nina in support links three more affected customers. Memseek prepares a summary of the shared problem.",
    focus: [
      "Slack · Nina",
      "“Same export timeout in three other accounts.”",
    ],
    from: "6 reports · 4 accounts",
    to: "Product summary ready",
    note: "Affected customers, renewal deadline and source links. Root cause still unconfirmed.",
  },
  {
    title: "Leo gets the warning in Slack.",
    body: "Memseek prepares a warning with the issue and deadline. Your connected Slack workflow sends it to Leo.",
    focus: [
      "Slack · Nina",
      "“Same export timeout in three other accounts.”",
    ],
    from: "Memseek escalation",
    to: "Renewal warning sent in Slack",
    note: "“Leo — Maya’s renewal is blocked by export failures.”",
  },
  {
    title: "The team assigns the fix. Maya gets an update.",
    body: "Sam, the product lead, assigns the fix in Linear. Your support agent emails Maya an update.",
    focus: [
      "Linear · Sam",
      "“The exports team owns the fix.”",
    ],
    from: "EXP-247 assigned",
    to: "Maya updated in Gmail",
    note: "“I’ll send an update before tomorrow’s renewal call.”",
  },
  {
    title: "Maya confirms the fix. Three follow-ups remain.",
    body: "Memseek clears Maya’s blocker, keeps the history and leaves three follow-ups open until customers confirm.",
    focus: [
      "Gmail · Maya",
      "“Export worked this morning.”",
    ],
    from: "Customer confirmation",
    to: "Maya’s blocker cleared",
    note: "Three follow-ups stay open. Her preferences and earlier reports stay in memory.",
  },
];

/**
 * The four source tiles on the org graph, in draw order. Each pairs an app
 * with the person who works in it, which is the point of the graph: the
 * sources are people, not integrations.
 *
 * `slot` is the historical class name the stage styling keys off; it names
 * the kind of source rather than the app.
 */
export const graphSources = [
  { slot: 'customers', app: 'Gmail', person: 'Maya', role: 'customer' },
  { slot: 'systems', app: 'HubSpot', person: 'Leo', role: 'account owner' },
  { slot: 'team', app: 'Slack', person: 'Nina', role: 'support lead' },
  { slot: 'docs', app: 'Linear', person: 'Sam', role: 'product lead' },
] as const satisfies readonly { slot: string; app: SourceApp; person: string; role: string }[];
