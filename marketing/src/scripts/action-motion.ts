/**
 * Draws the wires between surfaces: the handoff from the Memseek card to a
 * Slack channel, and the edges of the org graph.
 *
 * Paths are computed from live getBoundingClientRect() rather than authored,
 * because both scenes reflow: the graph changes shape between breakpoints and
 * the dispatch card moves when its text wraps. A drawn wire would be wrong in
 * every layout but the one it was drawn for.
 *
 * Nothing here is required for the scenes to be readable. Under reduced motion
 * the same paths are emitted as static lines with their arrowheads shown.
 */

const NS = 'http://www.w3.org/2000/svg';

/** One run's animations, keyed by the element that owns them, so replaying a
    scene cancels the previous run instead of layering a second one over it. */
const previous = new WeakMap<Element, Animation[]>();

type Direction = 'up' | 'down' | 'left' | 'right';
export type Phase = 'notice' | 'connect' | 'prepare' | 'escalate' | 'act' | 'learn';

function element<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
  parent: Element,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  parent.append(el);
  return el;
}

function canvas(root: HTMLElement, className: string) {
  root.querySelector(`:scope > .${className}`)?.remove();
  const box = root.getBoundingClientRect();
  return element('svg', { class: className, viewBox: `0 0 ${box.width} ${box.height}`, 'aria-hidden': 'true' }, root);
}

function animate(
  el: Element | null,
  frames: Keyframe[],
  options: KeyframeAnimationOptions,
  motions: Animation[],
  reduced: boolean,
) {
  if (!el || reduced) return;
  motions.push(el.animate(frames, { fill: 'both', easing: 'cubic-bezier(.2,.7,.2,1)', ...options }));
}

function arrowhead(end: { x: number; y: number }, direction: Direction) {
  const { x, y } = end;
  if (direction === 'down') return `M${x - 4} ${y - 5}l4 5 4-5`;
  if (direction === 'up') return `M${x - 4} ${y + 5}l4-5 4 5`;
  if (direction === 'left') return `M${x + 5} ${y - 4}l-5 4 5 4`;
  return `M${x - 5} ${y - 4}l5 4-5 4`;
}

function wire(
  svg: SVGSVGElement,
  d: string,
  end: { x: number; y: number },
  direction: Direction,
  delay: number,
  duration: number,
  motions: Animation[],
  reduced: boolean,
) {
  element('path', { d, class: 'action-wire-base' }, svg);
  const line = element('path', { d, class: 'action-wire-active', pathLength: 1 }, svg);
  const tip = element('path', { d: arrowhead(end, direction), class: 'action-wire-tip' }, svg);

  animate(line, [{ strokeDasharray: '1', strokeDashoffset: 1 }, { strokeDasharray: '1', strokeDashoffset: 0 }],
    { duration, delay }, motions, reduced);
  animate(tip, [{ opacity: 0 }, { opacity: 1 }], { duration: 180, delay: delay + duration - 150 }, motions, reduced);

  if (reduced) return;
  /* The travelling dot is sampled off the real path, so it follows any curve
     without the curve having to be expressed twice. */
  const packet = element('circle', { r: 4.5, class: 'action-wire-packet', opacity: 0 }, svg);
  const length = line.getTotalLength();
  const frames = Array.from({ length: 41 }, (_, i) => {
    const p = line.getPointAtLength((length * i) / 40);
    return { transform: `translate(${p.x}px,${p.y}px)`, opacity: i === 0 || i === 40 ? 0 : 1, offset: i / 40 };
  });
  animate(packet, frames, { duration, delay, easing: 'linear' }, motions, false);
}

function glow(el: Element | null, delay: number, motions: Animation[], reduced: boolean) {
  animate(el, [
    { boxShadow: '0 0 0 0 #7460db00' },
    { boxShadow: '0 0 0 4px #7460db30', offset: .35 },
    { boxShadow: '0 0 0 0 #7460db00' },
  ], { duration: 850, delay }, motions, reduced);
}

function reset(root: Element) {
  for (const a of previous.get(root) ?? []) a.cancel();
  const motions: Animation[] = [];
  previous.set(root, motions);
  return motions;
}

/** Plays one origin-to-target handoff inside a frame or an evidence dialog. */
export function play(root: HTMLElement, { delay = 0, reduced = false } = {}): Animation[] {
  const motions = reset(root);
  delete root.dataset.actionWaiting;
  if (!root.getClientRects().length) return motions;

  const origin = root.querySelector<HTMLElement>('[data-action-origin]');
  const targets = [...root.querySelectorAll<HTMLElement>('[data-action-target]')];
  if (!origin || !targets.length) return motions;

  const svg = canvas(root, 'action-wires');
  root.dataset.actionEnhanced = 'true';
  const box = root.getBoundingClientRect();
  const from = origin.getBoundingClientRect();
  const branch = root.dataset.actionFlow === 'branch';
  const sy = from.bottom - box.top + 1;

  /* Each before/after row resolves in turn, so the reader sees which value
     changed rather than a card that silently differs. */
  const rows = [...root.querySelectorAll('.case-run-row')];
  rows.forEach((row, i) => {
    const at = delay + i * 480;
    animate(row.querySelector('s'), [{ opacity: 1 }, { opacity: .45 }], { duration: 500, delay: at }, motions, reduced);
    animate(row.querySelector('.action-change-arrow'),
      [{ opacity: 0, transform: 'translateX(-7px)' }, { opacity: 1, transform: 'translateX(0)' }],
      { duration: 420, delay: at + 100 }, motions, reduced);
    animate(row.querySelector('strong'),
      [{ opacity: 0, transform: 'translateX(-8px)' }, { opacity: 1, transform: 'translateX(0)' }],
      { duration: 500, delay: at + 240 }, motions, reduced);
    glow(row.querySelector('strong'), at + 450, motions, reduced);
  });

  const handoff = delay + (rows.length ? 1050 : 500);
  animate(root.querySelector('.case-run-published'), [{ opacity: .15 }, { opacity: 1 }],
    { duration: 400, delay: handoff - 200 }, motions, reduced);
  glow(origin, delay, motions, reduced);

  targets.forEach((target, i) => {
    const to = target.getBoundingClientRect();
    let d: string;
    let end: { x: number; y: number };
    let direction: Direction;

    if (branch) {
      /* Two destinations from one origin: drop to a shared rail, then turn
         in, so the pair reads as one decision with two consequences. */
      const rail = 12;
      const ty = to.top - box.top + Math.min(39, to.height / 3);
      const tx = to.left - box.left - 2;
      d = `M${rail} ${sy}V${ty - 7}Q${rail} ${ty} ${rail + 7} ${ty}H${tx}`;
      end = { x: tx, y: ty };
      direction = 'right';
    } else {
      const sx = from.left - box.left + 37;
      const tx = to.left - box.left + 23;
      const ty = to.top - box.top - 2;
      const middle = sy + (ty - sy) / 2;
      d = `M${sx} ${sy}C${sx} ${middle} ${tx} ${middle} ${tx} ${ty}`;
      end = { x: tx, y: ty };
      direction = 'down';
    }

    const at = handoff + i * 1000;
    const duration = 900;
    const arrival = at + duration - 120;
    wire(svg, d, end, direction, at, duration, motions, reduced);
    animate(target, [{ opacity: .16, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: 500, delay: arrival }, motions, reduced);
    glow(target, arrival + 200, motions, reduced);
    animate(target.querySelector('.action-receipt'), [{ opacity: 0 }, { opacity: 1 }],
      { duration: 350, delay: arrival + 250 }, motions, reduced);
    animate(target.querySelector('.action-check'), [
      { transform: 'scale(.3)', opacity: 0 },
      { transform: 'scale(1.2)', opacity: 1, offset: .7 },
      { transform: 'scale(1)', opacity: 1 },
    ], { duration: 450, delay: arrival + 300 }, motions, reduced);
    animate(target.querySelector('.case-priority'),
      [{ opacity: .2, transform: 'scale(.94)' }, { opacity: 1, transform: 'scale(1)' }],
      { duration: 400, delay: arrival + 200 }, motions, reduced);
  });

  return motions;
}

/** Redraws the org graph's edges for one stage of the renewal story. */
export function graph(shell: HTMLElement, phase: Phase, reduced = false): Animation[] {
  const root = shell.querySelector<HTMLElement>('.learn-topology');
  if (!root) return [];
  const motions = reset(root);
  root.querySelector(':scope > .learn-traffic')?.remove();
  delete root.dataset.traffic;
  if (!root.getClientRects().length) return motions;

  const box = root.getBoundingClientRect();
  const engine = root.querySelector<HTMLElement>('.learn-engine')!;
  const human = root.querySelector<HTMLElement>('.learn-human')!;
  const agent = root.querySelector<HTMLElement>('.learn-agent')!;
  const result = root.querySelector<HTMLElement>('.learn-result')!;
  const svg = canvas(root, 'learn-traffic');

  const link = (a: Element, b: Element, delay: number) => {
    const from = a.getBoundingClientRect();
    const to = b.getBoundingClientRect();
    if (to.left - from.right >= 8) {
      const sx = from.right - box.left + 2;
      const sy = from.top - box.top + from.height / 2;
      const tx = to.left - box.left - 3;
      const ty = to.top - box.top + to.height / 2;
      const mx = (sx + tx) / 2;
      wire(svg, `M${sx} ${sy}C${mx} ${sy} ${mx} ${ty} ${tx} ${ty}`, { x: tx, y: ty }, 'right', delay, 850, motions, reduced);
    } else if (to.top - from.bottom >= 8) {
      const sx = from.left - box.left + from.width / 2;
      const sy = from.bottom - box.top + 1;
      const tx = to.left - box.left + to.width / 2;
      const ty = to.top - box.top - 3;
      const my = (sy + ty) / 2;
      wire(svg, `M${sx} ${sy}C${sx} ${my} ${tx} ${my} ${tx} ${ty}`, { x: tx, y: ty }, 'down', delay, 850, motions, reduced);
    } else return;
    glow(b, delay + 650, motions, reduced);
  };

  const inputs = root.querySelector<HTMLElement>('.learn-inputs')!;
  const horizontal = engine.getBoundingClientRect().left > inputs.getBoundingClientRect().right;

  if (phase !== 'learn') {
    if (horizontal) {
      /* Evidence rows join the engine through a shared inlet, and the paths
         start outside the source group, so no line crosses a message card. */
      const from = inputs.getBoundingClientRect();
      const to = engine.getBoundingClientRect();
      const sx = from.right - box.left + 2;
      const tx = to.left - box.left - 3;
      const ty = to.top - box.top + to.height / 2;
      const mx = (sx + tx) / 2;
      [0, 1].forEach((row, i) => {
        /* The second row has nothing connected yet in the opening stages. */
        if (row === 1 && (phase === 'notice' || phase === 'connect')) return;
        const source = inputs.children[row * 2].getBoundingClientRect();
        const sy = source.top - box.top + source.height / 2;
        wire(svg, `M${sx} ${sy}C${mx} ${sy} ${mx} ${ty} ${tx} ${ty}`, { x: tx, y: ty }, 'right', i * 160, 950, motions, reduced);
      });
    } else link(inputs, engine, 0);
  }

  if (phase === 'learn' && horizontal) {
    /* The outcome returns to memory around the bottom of the board. */
    const from = result.getBoundingClientRect();
    const to = engine.getBoundingClientRect();
    const sx = from.left - box.left + from.width / 2;
    const sy = from.bottom - box.top + 1;
    const tx = to.left - box.left + to.width / 2;
    const ty = to.bottom - box.top + 3;
    const bottom = box.height - 5;
    const d = `M${sx} ${sy}V${bottom - 12}Q${sx} ${bottom} ${sx - 12} ${bottom}H${tx + 12}Q${tx} ${bottom} ${tx} ${bottom - 12}V${ty}`;
    wire(svg, d, { x: tx, y: ty }, 'up', 250, 1700, motions, reduced);
    glow(engine, 1750, motions, reduced);
  } else if (phase === 'learn') {
    const from = result.getBoundingClientRect();
    const to = engine.getBoundingClientRect();
    const sx = from.right - box.left + 1;
    const sy = from.top - box.top + from.height / 2;
    const tx = to.right - box.left + 3;
    const ty = to.top - box.top + to.height / 2;
    const rail = box.width + 8;
    const d = `M${sx} ${sy}H${rail - 4}Q${rail} ${sy} ${rail} ${sy - 4}V${ty + 4}Q${rail} ${ty} ${rail - 4} ${ty}H${tx}`;
    wire(svg, d, { x: tx, y: ty }, 'left', 250, 1500, motions, reduced);
    glow(engine, 1500, motions, reduced);
  } else if (phase === 'escalate' || phase === 'act') {
    link(engine, human, 180);
    if (phase === 'act') link(engine, agent, 500);
    link(human, result, 1100);
    if (phase === 'act') link(agent, result, 1400);
    glow(engine, 0, motions, reduced);
  }

  if (svg.childElementCount) root.dataset.traffic = phase;
  return motions;
}

document.addEventListener('click', (event) => {
  const button = (event.target as Element).closest('[data-action-replay]');
  if (!button) return;
  const root = button.closest<HTMLElement>('[data-action-flow]');
  if (root) play(root, { reduced: matchMedia('(prefers-reduced-motion: reduce)').matches });
});
