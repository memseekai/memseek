/**
 * Chrome behaviours shared by every page: the narrow-width nav toggle, the
 * theme toggle on reading surfaces, copy buttons on code blocks, and the
 * in-page jumps. Nothing here is required for a page to be readable.
 */

const root = document.documentElement;

/* ---------- nav ---------- */

const header = document.querySelector<HTMLElement>('.header');
const menu = header?.querySelector<HTMLButtonElement>('.menu');

menu?.addEventListener('click', () => {
  const open = header!.toggleAttribute('data-open');
  menu.setAttribute('aria-expanded', String(open));
});

/* A destination is the end of the menu's job. */
header?.addEventListener('click', (e) => {
  if ((e.target as Element).closest('.navlinks a')) {
    header.removeAttribute('data-open');
    menu?.setAttribute('aria-expanded', 'false');
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !header?.hasAttribute('data-open')) return;
  header.removeAttribute('data-open');
  menu?.setAttribute('aria-expanded', 'false');
  menu?.focus();
});

/* ---------- theme ---------- */

/* Only reading surfaces render the toggle. The key is shared with the
   showcases and the docs so a reader's choice follows them across the site. */
const themeBtn = document.getElementById('themeBtn');
themeBtn?.addEventListener('click', () => {
  const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  themeBtn.setAttribute('aria-pressed', String(next === 'dark'));
  try { localStorage.setItem('ms-theme', next); } catch { /* private mode */ }
});
if (themeBtn) themeBtn.setAttribute('aria-pressed', String(root.getAttribute('data-theme') === 'dark'));

/* ---------- copy buttons ---------- */

for (const btn of document.querySelectorAll<HTMLButtonElement>('[data-copy-code]')) {
  btn.addEventListener('click', async () => {
    const code = btn.closest('[data-code-block]')?.querySelector('code')?.textContent;
    if (!code) return;
    const label = btn.textContent;
    try {
      await navigator.clipboard.writeText(code);
      btn.textContent = 'Copied';
    } catch {
      /* Clipboard is blocked over http and in some embeds. Selecting the
         block still lets the reader press the shortcut themselves. */
      const block = btn.closest('[data-code-block]')?.querySelector('code');
      if (block) {
        const range = document.createRange();
        range.selectNodeContents(block);
        const sel = getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      btn.textContent = 'Select and copy';
    }
    setTimeout(() => { btn.textContent = label; }, 2000);
  });
}

/* ---------- in-page jumps ---------- */

for (const link of document.querySelectorAll<HTMLAnchorElement>('[data-jump]')) {
  link.addEventListener('click', (e) => {
    const target = document.querySelector(link.getAttribute('href')!);
    if (!target) return;
    e.preventDefault();
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    /* scrollIntoView does not move focus, so a keyboard reader would land
       back at the top of the document on the next Tab. */
    (target as HTMLElement).setAttribute('tabindex', '-1');
    (target as HTMLElement).focus({ preventScroll: true });
  });
}
