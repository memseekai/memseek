/**
 * The benchmark chart's interactivity: selecting a point, toggling series, and
 * switching cost between per-answer and per-million.
 *
 * The chart renders complete and correct without this. What it adds is the
 * ability to isolate a series and to read a cost at a scale you can picture.
 */

const currency = (v: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumSignificantDigits: 3 }).format(v);

const labOf = (el: Element) => el.closest<HTMLElement>('.bench-lab')!;

/** Writes the readout under the chart from whichever point is selected. */
function renderSelection(lab: HTMLElement) {
  const point = lab.querySelector<SVGGElement>('[data-bench-point][aria-pressed="true"]:not(.is-off)');
  const output = lab.querySelector<HTMLElement>('.bench-selection')!;
  output.hidden = !point;
  if (!point) return;

  const multiplier = Number(lab.dataset.multiplier || 1);
  const d = point.dataset;
  /* An empty cost is a configuration that never published one. Saying "not
     reported" is the honest readout; zero would be a claim. */
  const missing = d.cost === '';
  const approx = d.basis === 'chart-estimate';

  lab.querySelector('[data-selected-name]')!.textContent = d.name!;
  lab.querySelector('[data-selected-score]')!.textContent = `${d.accuracy}% accuracy · ${d.judge}`;
  lab.querySelector('[data-selected-cost]')!.textContent =
    missing ? 'Not reported' : (approx ? '≈' : '') + currency(Number(d.cost) * multiplier);
  lab.querySelector('[data-selected-unit]')!.textContent =
    missing
      ? 'cost unavailable'
      : `${approx ? 'original chart position' : 'estimated model cost'}${multiplier === 1 ? ' / answer' : ' / 1M answers'}`;
}

function choose(point: SVGGElement) {
  const lab = labOf(point);
  for (const p of lab.querySelectorAll('[data-bench-point]')) {
    p.setAttribute('aria-pressed', String(p === point));
  }
  renderSelection(lab);
}

function updateVisibility(lab: HTMLElement) {
  const inputs = [...lab.querySelectorAll<HTMLInputElement>('[data-series-toggle]')];
  for (const input of inputs) {
    for (const el of lab.querySelectorAll(`[data-series="${input.dataset.seriesToggle}"]`)) {
      el.classList.toggle('is-off', !input.checked);
      /* A hidden point must also leave the tab order, or a keyboard reader
         walks through data that is not on screen. */
      if (el.hasAttribute('data-bench-point')) {
        el.setAttribute('tabindex', input.checked ? '0' : '-1');
        el.setAttribute('aria-hidden', String(!input.checked));
      }
    }
  }

  const visible = [...lab.querySelectorAll('[data-bench-point]')].filter((p) => !p.classList.contains('is-off'));
  /* Hiding the selected series moves the selection rather than emptying it. */
  if (!visible.some((p) => p.getAttribute('aria-pressed') === 'true')) {
    for (const p of lab.querySelectorAll('[data-bench-point]')) p.setAttribute('aria-pressed', 'false');
    visible[0]?.setAttribute('aria-pressed', 'true');
  }

  lab.querySelector('[data-visible-count]')!.textContent = `${visible.length} of ${inputs.length} series shown`;
  lab.querySelector<HTMLElement>('[data-bench-empty]')!.hidden = visible.length > 0;
  renderSelection(lab);
}

for (const point of document.querySelectorAll<SVGGElement>('[data-bench-point]')) {
  point.addEventListener('click', () => choose(point));
  point.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    choose(point);
  });
}

for (const input of document.querySelectorAll<HTMLInputElement>('[data-series-toggle]')) {
  input.addEventListener('change', () => updateVisibility(labOf(input)));
}

for (const button of document.querySelectorAll<HTMLButtonElement>('[data-bench-unit]')) {
  button.addEventListener('click', () => {
    const lab = labOf(button);
    const multiplier = Number(button.dataset.benchUnit);
    lab.dataset.multiplier = String(multiplier);
    for (const b of lab.querySelectorAll('[data-bench-unit]')) b.setAttribute('aria-pressed', String(b === button));

    for (const el of lab.querySelectorAll<HTMLElement>('[data-dollar]')) {
      const v = Number(el.dataset.dollar) * multiplier;
      /* Axis ticks abbreviate at the million scale; the readouts stay exact. */
      const value = el.classList.contains('axis-text') && v >= 1000
        ? `$${v >= 1e6 ? `${v / 1e6}M` : `${v / 1000}k`}`
        : currency(v);
      el.textContent = (el.hasAttribute('data-approx') ? '≈' : '') + value;
    }
    lab.querySelector('[data-cost-axis]')!.textContent = multiplier === 1
      ? 'Estimated model cost per answer · USD · log scale →'
      : 'Estimated model cost per 1M answers · USD · log scale →';
    renderSelection(lab);
  });
}

for (const lab of document.querySelectorAll<HTMLElement>('.bench-lab')) updateVisibility(lab);
