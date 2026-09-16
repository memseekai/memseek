// Fails if any var(--x) in the source has no definition. The token sweep from
// the 2026-09 redesign silently broke several diagrams this way: an undefined
// custom property does not error, it just resolves to the initial value, so a
// stroke turns black and a line disappears with nothing in the console.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC = new URL('../src/', import.meta.url).pathname;

/** Properties written by scripts at runtime, or injected by Shiki. */
const RUNTIME = [
  /^--ambient-[xy]$/, /^--fold-/, /^--org-/, /^--story-/, /^--shine-/,
  /^--reading-progress$/, /^--return-overhang$/, /^--shiki-/, /^--hue$/,
];

const walk = (dir) => readdirSync(dir).flatMap((f) => {
  const p = join(dir, f);
  return statSync(p).isDirectory() ? walk(p) : [p];
});

const files = walk(SRC).filter((f) => /\.(css|astro|ts)$/.test(f));
const defined = new Set();
const used = new Map();

for (const file of files) {
  const text = readFileSync(file, 'utf8');
  for (const [, name] of text.matchAll(/(--[a-z0-9-]+)\s*:/g)) defined.add(name);
  for (const [, name] of text.matchAll(/var\((--[a-z0-9-]+)/g)) {
    if (!used.has(name)) used.set(name, file.replace(SRC, 'src/'));
  }
}

const missing = [...used].filter(([name]) => !defined.has(name) && !RUNTIME.some((r) => r.test(name)));
for (const [name, file] of missing) console.log(`undefined ${name}  (first used in ${file})`);
console.log(missing.length ? `\n${missing.length} undefined custom propert${missing.length === 1 ? 'y' : 'ies'}` : 'every custom property resolves');
process.exit(missing.length ? 1 : 0);
