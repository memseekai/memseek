// Reads the palettes straight out of src/styles/tokens.css and reports the
// contrast of every ink-on-surface pair that ships. Run it after touching a
// colour; the dark set is derived rather than designed, so it needs proving.
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8');
const block = (sel) => css.slice(css.indexOf(sel)).match(/\{([\s\S]*?)\n\}/)[1];
const vars = (sel) => Object.fromEntries(
  [...block(sel).matchAll(/--([\w-]+):\s*(#[0-9a-f]{3,8});/gi)].map(([, k, v]) => [k, v]));

const lum = (hex) => {
  const n = hex.length === 4 ? [...hex.slice(1)].map((c) => c + c) : hex.slice(1).match(/../g);
  const [r, g, b] = n.slice(0, 3).map((h) => {
    const c = parseInt(h, 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

// Text roles and the minimum each must clear. Body and metadata are read for
// minutes at a time, so they hold to 4.5; --blue is link text at body size and
// holds to the same bar. Large display ink gets 3.0.
const PAIRS = [
  ['ink', 'ambient', 4.5], ['ink', 'paper', 4.5], ['ink', 'silver', 4.5],
  ['copy', 'ambient', 4.5], ['copy', 'paper', 4.5], ['copy', 'silver', 4.5],
  ['muted', 'ambient', 4.5], ['muted', 'paper', 4.5],
  ['blue', 'ambient', 4.5], ['blue', 'paper', 4.5], ['blue', 'silver', 4.5],
  ['blue', 'blue-soft', 4.5],
  ['app-gmail', 'paper', 3], ['app-hubspot', 'paper', 3],
  ['app-slack', 'paper', 3], ['app-linear', 'paper', 3],
  ['faint', 'ambient', 4.5], ['faint', 'paper', 4.5],
  ['caution', 'ambient', 4.5], ['caution', 'paper', 4.5],
  ['ink', 'code-bg', 4.5], ['copy', 'code-bg', 4.5],
  /* Series lines carry direct labels at small type, so they hold to the
     text bar rather than the 3.0 graphical-object minimum. */
  ['series-1', 'ambient', 4.5], ['series-2', 'ambient', 4.5], ['series-3', 'ambient', 4.5],
  ['series-4', 'ambient', 4.5], ['series-5', 'ambient', 4.5],
  ['series-1', 'paper', 4.5], ['series-2', 'paper', 4.5], ['series-3', 'paper', 4.5],
  ['series-4', 'paper', 4.5], ['series-5', 'paper', 4.5],
];

let failed = 0;
for (const [theme, sel] of [['light', ':root {'], ['dark', ":root[data-theme='dark']"]]) {
  const t = vars(sel);
  console.log(`\n${theme}`);
  for (const [fg, bg, min] of PAIRS) {
    const r = ratio(t[fg], t[bg]);
    const ok = r >= min;
    if (!ok) failed++;
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${r.toFixed(2)} (min ${min})  --${fg} on --${bg}`);
  }
}
console.log(failed ? `\n${failed} failing pair(s)` : '\nall pairs clear');
process.exit(failed ? 1 : 0);
