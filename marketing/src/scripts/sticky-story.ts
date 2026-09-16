/**
 * Drives the six-step renewal story.
 *
 * The shell pins while the runway scrolls past it, and scroll position picks
 * a stage. The scene never pans or translates: one composition stays at the
 * same screen coordinates for all six steps, and only its contents change.
 *
 * Three things here are load-bearing rather than decorative:
 *
 *   measure() renders all six panels into an inert clone to find the tallest,
 *   then pins that height. Without it the board resizes under the reader
 *   every time a stage changes.
 *
 *   `fits` is the escape hatch. If the shell cannot fit the viewport — heavy
 *   text enlargement, a short landscape window — the scene stops sticking and
 *   the six stage buttons become plain controls. Nothing is clipped or shrunk.
 *
 *   Everything renders at stage one with no JavaScript at all.
 */
import { graph, play } from './action-motion';
import { stages } from '../data/renewal-story';

const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const clamp = (n: number, min: number, max: number) => Math.max(min, Math.min(max, n));

/** Which source tile is the focus at each stage, by index into the four. */
const FOCUS_SOURCE = [0, 0, 2, 2, 3, 0];
/** The result node's glyph per stage: pending, added, sent, escalated, acted, returned. */
const RESULT_SYMBOL = ['…', '+', '→', '↗', '↗', '↻'];

for (const root of document.querySelectorAll<HTMLElement>('[data-org-story]')) {
  const shell = root.querySelector<HTMLElement>('[data-org-shell]')!;
  const content = root.querySelector<HTMLElement>('[data-org-content]')!;
  const panelWrap = root.querySelector<HTMLElement>('[data-org-panels]')!;
  const panels = [...root.querySelectorAll<HTMLElement>('[data-org-panel]')];
  const buttons = [...root.querySelectorAll<HTMLButtonElement>('[data-org-step]')];

  let current = -1;
  let frame = 0;
  let resizeTimer: ReturnType<typeof setTimeout>;
  let top = 12;
  let travel = 0;
  let step = 600;
  let transition: Animation[] = [];
  let graphMotions: Animation[] = [];
  let measured = false;
  let fits = true;

  /** Writes one stage into a subtree. Used on the live scene and on the
      offscreen probe, which is why it takes its target. */
  function render(target: ParentNode, index: number) {
    const state = stages[index];
    target.querySelectorAll<HTMLElement>('[data-org-panel]').forEach((p, i) => { p.hidden = i !== index; });
    target.querySelector('[data-org-engine]')!.textContent = state.engine;
    target.querySelector('[data-org-result]')!.textContent = state.outcome;
    for (const el of target.querySelectorAll<HTMLElement>('[data-source-status]')) {
      el.textContent = state.statuses[Number(el.dataset.sourceStatus)];
    }
    for (const el of target.querySelectorAll<HTMLElement>('[data-case-person]')) {
      el.textContent = state.people[Number(el.dataset.casePerson)];
    }
    target.querySelector('[data-org-focus-source]')!.textContent = state.focus[0];
    target.querySelector('[data-org-focus-text]')!.textContent = state.focus[1];
    for (const el of target.querySelectorAll<HTMLElement>('[data-case-source]')) {
      const n = Number(el.dataset.caseSource);
      /* Nina joins at stage 2 and Sam at stage 4. Before that their tiles are
         present but muted: unlinked evidence, not missing evidence. */
      el.dataset.sourcePending = String((n === 2 && index < 2) || (n === 3 && index < 4));
      el.dataset.sourceFocus = String(n === FOCUS_SOURCE[index]);
    }
    target.querySelector('[data-result-symbol]')!.textContent = RESULT_SYMBOL[index];
  }

  function show(index: number) {
    if (index === current) return;
    const previous = current;
    current = index;
    for (const a of transition) a.cancel();
    for (const a of graphMotions) a.cancel();
    transition = [];
    graphMotions = [];

    render(content, index);
    shell.dataset.activeStage = String(index);
    shell.dataset.loopPhase = stages[index].phase;
    buttons.forEach((b, i) => b.setAttribute('aria-pressed', String(i === index)));
    shell.querySelector('[data-org-counter]')!.textContent =
      `${String(index + 1).padStart(2, '0')} / ${String(stages.length).padStart(2, '0')}`;
    shell.querySelector<HTMLElement>('[data-org-meter]')!.style.transform =
      `scaleX(${(index + 1) / stages.length})`;
    graphMotions = graph(shell, stages[index].phase, reduced.matches);

    if (previous < 0 || reduced.matches) return;
    /* The panel and the focused quote enter from the direction of travel, so
       scrolling back up reads as going back rather than as a new arrival. */
    const direction = index > previous ? 1 : -1;
    for (const el of [panels[index], content.querySelector('.learn-focus-message')!]) {
      transition.push(el.animate([
        { opacity: .1, transform: `translateY(${direction * 6}px)` },
        { opacity: 1, transform: 'translateY(0)' },
      ], { duration: 330, easing: 'cubic-bezier(.22,.8,.22,1)' }));
    }
    const arrow = panels[index].querySelector('.learn-receipt-arrow');
    if (arrow) {
      transition.push(arrow.animate([
        { opacity: .2, transform: 'translateX(-6px)' },
        { opacity: 1, transform: 'translateX(0)' },
      ], { duration: 650, easing: 'ease-out' }));
    }
  }

  /** The return path is drawn in CSS, so its ends have to be told where the
      engine and the result actually are at this width. */
  function anchorFeedback() {
    const engine = content.querySelector<HTMLElement>('.learn-engine')!;
    const result = content.querySelector<HTMLElement>('.learn-result')!;
    const feedback = content.querySelector<HTMLElement>('.learn-feedback')!;
    const compact = innerWidth <= 1000;
    feedback.style.marginLeft = compact ? '' : `${engine.offsetWidth / 2}px`;
    feedback.style.marginRight = compact ? '' : `${result.offsetWidth / 2}px`;
    feedback.style.setProperty('--return-overhang', compact
      ? '0px'
      : `${Math.max(0, engine.getBoundingClientRect().bottom - result.getBoundingClientRect().bottom)}px`);
  }

  function measure() {
    if (!root.getClientRects().length) return;
    const rect = root.getBoundingClientRect();
    const wasPinned = measured && fits && rect.top <= top && rect.bottom > innerHeight;
    const oldProgress = measured ? clamp((top - rect.top) / step, 0, stages.length) : 0;

    panelWrap.style.minHeight = '';
    const probe = content.cloneNode(true) as HTMLElement;
    probe.setAttribute('aria-hidden', 'true');
    probe.inert = true;
    /* The clone would otherwise duplicate every id in the scene. */
    for (const el of probe.querySelectorAll('[id]')) el.removeAttribute('id');
    Object.assign(probe.style, {
      position: 'absolute', visibility: 'hidden', pointerEvents: 'none',
      width: `${content.clientWidth}px`, left: '0', top: '0',
    });
    shell.append(probe);
    let maxPanel = 0;
    for (let i = 0; i < stages.length; i++) {
      render(probe, i);
      maxPanel = Math.max(maxPanel, probe.querySelector('[data-org-panels]')!.getBoundingClientRect().height);
    }
    probe.remove();
    panelWrap.style.minHeight = `${Math.ceil(maxPanel)}px`;

    root.classList.add('is-enhanced', 'is-staged');
    const shellHeight = Math.ceil(shell.getBoundingClientRect().height);
    fits = shellHeight <= innerHeight - 16;
    root.classList.toggle('is-readable', !fits);

    top = Math.max(8, (innerHeight - shellHeight) / 2);
    step = clamp(innerHeight * .82, 500, 740);
    travel = step * stages.length;
    root.style.setProperty('--org-height', `${shellHeight}px`);
    root.style.setProperty('--org-travel', `${travel}px`);
    root.style.setProperty('--org-top', `${top}px`);

    /* Proximity snapping settles on the six stage positions, so a flick of
       the wheel lands on a stage instead of between two. */
    for (const el of root.querySelectorAll('.learn-snap-anchor')) el.remove();
    if (fits) {
      stages.forEach((_, i) => {
        const anchor = document.createElement('span');
        anchor.className = 'learn-snap-anchor';
        anchor.setAttribute('aria-hidden', 'true');
        anchor.style.top = `${i * step + 24}px`;
        anchor.style.scrollMarginTop = `${top}px`;
        root.append(anchor);
      });
    }

    anchorFeedback();
    for (const a of graphMotions) a.cancel();
    graphMotions = graph(shell, stages[current].phase, true);
    measured = true;
    /* Remeasuring must not move the reader to a different stage. */
    if (wasPinned && fits) {
      scrollTo({ top: scrollY + root.getBoundingClientRect().top - top + oldProgress * step, behavior: 'instant' });
    }
    schedule();
  }

  function update() {
    frame = 0;
    if (!measured) return;
    const visible = root.getClientRects().length > 0;
    const bounds = root.getBoundingClientRect();
    const pinned = visible && fits && bounds.top <= top + 1 && bounds.bottom > innerHeight - top;
    document.documentElement.classList.toggle('memseek-story-snap', !reduced.matches && pinned);
    if (!visible || !fits) return;
    const progress = clamp(top - bounds.top, 0, travel);
    show(Math.min(stages.length - 1, Math.floor(progress / step)));
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(update);
  }

  buttons.forEach((button, index) => button.addEventListener('click', () => {
    /* When the scene does not stick, the buttons are the only way through. */
    if (!fits) { show(index); return; }
    const target = scrollY + root.getBoundingClientRect().top - top + index * step + 24;
    scrollTo({ top: Math.max(0, target), behavior: reduced.matches ? 'instant' : 'smooth' });
  }));

  for (const button of root.querySelectorAll<HTMLButtonElement>('[data-org-evidence]')) {
    button.addEventListener('click', () => {
      const dialog = root.querySelector<HTMLDialogElement>(`[data-org-dialog="${button.dataset.orgEvidence}"]`)!;
      dialog.showModal();
      const action = dialog.querySelector<HTMLElement>('[data-action-flow]');
      if (action) play(action, { reduced: reduced.matches });
    });
  }

  for (const dialog of root.querySelectorAll<HTMLDialogElement>('[data-org-dialog]')) {
    dialog.querySelector('[data-org-close]')!.addEventListener('click', () => dialog.close());
    /* A click on the backdrop targets the dialog itself, so compare against
       its box to tell a backdrop click from a click on its own padding. */
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const r = dialog.getBoundingClientRect();
      if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) {
        dialog.close();
      }
    });
  }

  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(measure, 80); });
  addEventListener('hashchange', () => requestAnimationFrame(measure));
  reduced.addEventListener('change', () => {
    for (const a of transition) a.finish();
    for (const a of graphMotions) a.finish();
    schedule();
  });

  show(0);
  measure();
  document.fonts.ready.then(measure);
}
