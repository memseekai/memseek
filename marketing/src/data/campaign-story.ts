/** Illustrative workflow. Outcomes are feedback, not a claimed performance lift. */
export const creativeRule = 'Use lifestyle imagery that features the product.';
export const learnedRule = 'Use the campaign’s audience brief to choose settings, styling, and language.';
export const campaignSteps = [
  { label: 'Run', title: 'Three agents. One campaign.', body: 'The catalog agent supplies product facts. The creative agent retrieves its skill from Memseek and makes the ad. The performance agent watches the results.', actor: 'Creative agent', status: 'Using creative skill v1' },
  { label: 'Observe', title: 'The work comes back with feedback.', body: 'The performance agent flags weaker engagement in Colombia. A marketer adds context. Together, they give the learning agent a reason to revisit the creative skill.', actor: 'Performance agent', status: 'Feedback recorded' },
  { label: 'Improve', title: 'One agent improves how the others work.', body: 'The learning agent reads the current skill, campaign report, and marketer’s feedback. It proposes a cited revision for review. In this example, a marketer approves it.', actor: 'Learning agent', status: 'Proposed v2 · review required' },
  { label: 'Run again', title: 'The next campaign starts with the lesson.', body: 'After approval, Memseek serves skill v2. The creative agent retrieves it for the next campaign and follows the audience brief. New results feed the next learning cycle.', actor: 'Creative agent', status: 'Using approved creative skill v2' },
];
