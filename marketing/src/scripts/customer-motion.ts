/**
 * Drives the four-frame hero demo.
 *
 * It advances on a timer and stops being a timer the moment the reader touches
 * it. It also stops while off screen, while the tab is hidden, and while focus
 * is inside it. Under reduced motion the timer never starts at all and the
 * playback control becomes a manual "Next day" button, which is why the label
 * is computed rather than written into the markup.
 */
import { play } from './action-motion';
import { dwellMs } from '../data/customer-week';

const reduced = matchMedia('(prefers-reduced-motion: reduce)');

for (const root of document.querySelectorAll<HTMLElement>('[data-customer-demo]')) {
  const frames = [...root.querySelectorAll<HTMLElement>('[data-demo-frame]')];
  const days = [...root.querySelectorAll<HTMLButtonElement>('[data-demo-day]')];
  const button = root.querySelector<HTMLButtonElement>('[data-demo-playback]')!;
  const stage = root.querySelector<HTMLElement>('.demo-stage')!;
  const progress = root.querySelector<HTMLElement>('[data-demo-progress]')!;
  const announcement = root.querySelector<HTMLElement>('[data-demo-announcement]')!;

  let current = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let visible = false;
  let started = false;
  let paused = reduced.matches;
  let motions: Animation[] = [];

  function playbackLabel() {
    const label = reduced.matches ? 'Next day' : paused ? 'Play' : 'Pause';
    button.setAttribute('aria-label', reduced.matches ? 'Next day in customer story' : `${label} customer story`);
    root.querySelector('[data-demo-play-label]')!.textContent = label;
    root.querySelector('[data-demo-play-symbol]')!.textContent = paused ? '▷' : 'Ⅱ';
    root.dataset.demoPaused = String(paused);
  }

  function schedule() {
    clearTimeout(timer);
    if (paused || !visible || document.hidden || reduced.matches) return;
    timer = setTimeout(() => show((current + 1) % frames.length), dwellMs[current]);
  }

  function show(index: number, manual = false) {
    for (const a of motions) a.cancel();
    motions = [];
    current = index;
    root.dataset.demoActive = String(index);
    frames.forEach((frame, i) => { frame.hidden = i !== index; });
    days.forEach((day, i) => {
      day.setAttribute('aria-pressed', String(i === index));
      day.classList.toggle('is-past', i < index);
    });
    progress.style.transform = `scaleX(${index / (frames.length - 1)})`;

    const frame = frames[index];
    if (!reduced.matches) {
      const motion = (selector: string, keyframes: Keyframe[], options: KeyframeAnimationOptions) => {
        const el = frame.querySelector(selector);
        if (el) motions.push(el.animate(keyframes, { fill: 'both', ...options }));
      };
      motion('.demo-incoming',
        [{ opacity: 0, transform: 'translate(18px,-8px) rotate(1deg)' }, { opacity: 1, transform: 'translate(0,0) rotate(0)' }],
        { duration: 750, easing: 'cubic-bezier(.16,1,.3,1)' });
      motion('.demo-connection i',
        [{ opacity: 0, transform: 'translateY(0)' }, { opacity: 1, offset: .2 }, { opacity: 1, offset: .8 }, { opacity: 0, transform: 'translateY(25px)' }],
        { duration: 1100, delay: 400, easing: 'ease-in-out' });
      motion('.demo-memory-content',
        [{ opacity: .15, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }],
        { duration: 800, delay: 650, easing: 'cubic-bezier(.16,1,.3,1)' });
      motion('.demo-next',
        [{ opacity: .2, transform: 'translateY(5px)' }, { opacity: 1, transform: 'translateY(0)' }],
        { duration: 700, delay: 1000, easing: 'ease-out' });
      frame.querySelectorAll('.demo-evidence-chip').forEach((chip, i) => motions.push(
        chip.animate([{ opacity: .2, transform: 'translateX(-5px)' }, { opacity: 1, transform: 'translateX(0)' }],
          { duration: 450, delay: 850 + i * 140, fill: 'both', easing: 'ease-out' })));
    }
    if (frame.matches('[data-action-flow]')) {
      motions.push(...play(frame, { delay: 1100, reduced: reduced.matches }));
    }

    if (manual) {
      /* Touching the control is a decision to read at your own pace. */
      paused = true;
      announcement.textContent =
        `${days[index].getAttribute('aria-label')!.replace('Show ', '')}. ${frame.querySelector('h2')!.textContent}`;
      playbackLabel();
    }
    schedule();
  }

  /**
   * Sizes the stage to its tallest frame by rendering every frame into an
   * inert clone. Without this the page jumps by whatever the frames differ by
   * each time one swaps, which on a timer is a moving target.
   */
  function measure() {
    if (!root.getClientRects().length) return;
    const probe = stage.cloneNode(true) as HTMLElement;
    probe.inert = true;
    probe.setAttribute('aria-hidden', 'true');
    Object.assign(probe.style, {
      position: 'absolute', visibility: 'hidden', pointerEvents: 'none',
      width: `${stage.getBoundingClientRect().width}px`, minHeight: '0', top: '0', left: '0',
    });
    root.append(probe);
    let height = 0;
    const copies = [...probe.querySelectorAll<HTMLElement>('[data-demo-frame]')];
    copies.forEach((_, i) => {
      copies.forEach((f, j) => { f.hidden = i !== j; });
      height = Math.max(height, probe.getBoundingClientRect().height);
    });
    probe.remove();
    stage.style.minHeight = `${Math.ceil(height)}px`;

    /* A dispatch frame that has never played still needs its static wire. */
    const action = frames[current];
    if (action.matches('[data-action-flow]') && !action.querySelector('.action-wires')) {
      play(action, { reduced: true });
    }
  }

  days.forEach((day, i) => day.addEventListener('click', () => show(i, true)));
  button.addEventListener('click', () => {
    if (reduced.matches) { show((current + 1) % frames.length, true); return; }
    paused = !paused;
    playbackLabel();
    schedule();
  });

  new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting;
    if (!visible) for (const a of motions) a.finish();
    if (visible && !started) { started = true; show(current); }
    measure();
    schedule();
  }, { threshold: .25 }).observe(root);

  root.addEventListener('focusin', () => clearTimeout(timer));
  root.addEventListener('focusout', (event) => {
    if (!root.contains(event.relatedTarget as Node)) schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  addEventListener('resize', measure);
  addEventListener('hashchange', () => requestAnimationFrame(measure));
  reduced.addEventListener('change', () => {
    paused = true;
    for (const a of motions) a.finish();
    playbackLabel();
    schedule();
  });
  /* Metrics change once the real face lands, so measure again then. */
  document.fonts.ready.then(measure);

  playbackLabel();
  measure();
  schedule();
}
