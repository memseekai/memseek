// Emits public/chrome.css so the standalone showcase pages under public/ can
// use exactly the site's tokens, header, footer and buttons. They are served
// verbatim and cannot import from src/, and the previous site's answer was a
// second hand-maintained copy that drifted. One source, one generated file.
import { readFileSync, writeFileSync } from 'node:fs';

const read = (name) => readFileSync(new URL(`../src/styles/${name}`, import.meta.url), 'utf8');

const banner = `/* Generated from src/styles/tokens.css and src/styles/site.css by
   scripts/sync-chrome.mjs. Do not edit: change the source and rebuild. */\n\n`;

// site.css imports tokens.css; here they are concatenated instead, because an
// @import from public/ would cost a second blocking request on every showcase.
const site = read('site.css').replace(/@import\s+'\.\/tokens\.css';\s*/, '');

writeFileSync(new URL('../public/chrome.css', import.meta.url), banner + read('tokens.css') + '\n' + site);
console.log('public/chrome.css written');
