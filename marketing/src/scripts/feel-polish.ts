/**
 * Three pieces of finish. All of them are decorative: remove this module and
 * the page still says everything it says.
 *
 *   The headline word resolves once, over an aria-hidden overlay, so the
 *   accessible text never changes mid-scramble.
 *   The active source tile springs when a stage changes, which is what makes
 *   the graph read as responding rather than re-rendering.
 *   The closing panel draws a finite flow field for 4.5 seconds on first
 *   entry. Deterministic trigonometry, no noise library, no new dependency.
 */
import { graph } from './action-motion';
import { stages, type StagePhase } from '../data/renewal-story';

const reduced = matchMedia('(prefers-reduced-motion: reduce)');

/* ---------- the headline word ---------- */

const LETTERS = 'abcdefghijklmnopqrstuvwxyz';

for (const word of document.querySelectorAll<HTMLElement>('[data-word-reveal]')) {
  if (reduced.matches) continue;
  const text = word.textContent ?? '';
  const overlay = document.createElement('span');
  overlay.setAttribute('aria-hidden', 'true');
  Object.assign(overlay.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
  word.style.position = 'relative';
  word.append(overlay);
  /* The real word stays in the DOM and is simply masked, so a screen reader
     and a copy-paste both get "compound." throughout. */
  const real = document.createElement('span');
  real.textContent = text;
  real.style.visibility = 'hidden';
  word.replaceChildren(real, overlay);
  overlay.textContent = text;

  const start = performance.now();
  const DURATION = 650;
  const tick = (now: number) => {
    const t = Math.min(1, (now - start) / DURATION);
    const settled = Math.floor(t * text.length);
    overlay.textContent = [...text]
      .map((c, i) => (i < settled || c === ' ' || c === '.' ? c : LETTERS[(Math.random() * 26) | 0]))
      .join('');
    if (t < 1) requestAnimationFrame(tick);
    else { overlay.remove(); real.style.visibility = ''; }
  };
  requestAnimationFrame(tick);
}

/* ---------- the graph responding to a stage change ---------- */

for (const shell of document.querySelectorAll<HTMLElement>('[data-org-shell]')) {
  let seen = shell.dataset.activeStage;
  /* Wires are drawn from live geometry, so they have to be redrawn when the
     board first scrolls into view, not when it is built offscreen. */
  new IntersectionObserver((entries) => {
    if (!entries[0].isIntersecting) return;
    graph(shell, (shell.dataset.loopPhase ?? stages[0].phase) as StagePhase, reduced.matches);
  }, { threshold: .3 }).observe(shell);

  new MutationObserver(() => {
    if (shell.dataset.activeStage === seen) return;
    seen = shell.dataset.activeStage;
    if (reduced.matches) return;
    const icon = shell.querySelector('[data-source-focus="true"] .graph-app-icon');
    icon?.animate([
      { transform: 'scale(1)', boxShadow: '0 0 0 0 #3f66eb00' },
      { transform: 'scale(1.09)', boxShadow: '0 0 0 6px #3f66eb1f', offset: .45 },
      { transform: 'scale(1)', boxShadow: '0 0 0 0 #3f66eb00' },
    ], { duration: 720, easing: 'cubic-bezier(.22,.8,.22,1)' });
  }).observe(shell, { attributes: true, attributeFilter: ['data-active-stage'] });
}

/* ---------- the closing flow field ---------- */

for (const panel of document.querySelectorAll<HTMLElement>('[data-flow-field]')) {
  if (reduced.matches) continue;
  const canvas = document.createElement('canvas');
  canvas.className = 'hq-field';
  panel.prepend(canvas);

  const observer = new IntersectionObserver((entries) => {
    if (!entries[0].isIntersecting) return;
    observer.disconnect();

    const dpr = Math.min(2, devicePixelRatio || 1);
    const box = panel.getBoundingClientRect();
    canvas.width = box.width * dpr;
    canvas.height = box.height * dpr;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.strokeStyle = '#3263df';
    ctx.lineWidth = 1;

    /* Seeded from position alone, so two viewers see the same field and a
       re-entry cannot produce a different one. */
    const field = (x: number, y: number) =>
      Math.sin(x * 0.0065) * 1.6 + Math.cos(y * 0.0085) * 1.4 + Math.sin((x + y) * 0.0032) * 0.9;

    const strands = Array.from({ length: 90 }, (_, i) => ({
      x: ((i * 61) % Math.max(1, box.width)),
      y: ((i * 137) % Math.max(1, box.height)),
    }));

    const start = performance.now();
    const DURATION = 4500;
    const draw = (now: number) => {
      if (now - start > DURATION) return;
      for (const s of strands) {
        const a = field(s.x, s.y);
        const nx = s.x + Math.cos(a) * 21;
        const ny = s.y + Math.sin(a) * 21;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(nx, ny);
        ctx.stroke();
        s.x = nx;
        s.y = ny;
      }
      requestAnimationFrame(draw);
    };
    requestAnimationFrame(draw);
  }, { threshold: .25 });
  observer.observe(panel);
}
