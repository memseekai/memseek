# memseek marketing site

The public site: marketing pages, blog, and the interactive showcases. Astro 5
with MDX, deployed to Cloudflare as a Worker with static assets.

| URL | Source | Notes |
| --- | --- | --- |
| `/` | [src/pages/index.astro](src/pages/index.astro) | Hero demo, the six-step renewal story, developer start, benchmark band. |
| `/how-it-works/` | [src/pages/how-it-works.astro](src/pages/how-it-works.astro) | Five stages, responsibilities, FAQ. Content in [src/data/how-it-works.ts](src/data/how-it-works.ts). |
| `/benchmarks/` | [src/pages/benchmarks.astro](src/pages/benchmarks.astro) | All eleven LongMemEval-S series, the cost chart, reproduction detail. |
| `/start/` | [src/pages/start.astro](src/pages/start.astro) | Local setup, then managed setup. |
| `/blog/`, `/blog/<slug>/` | [src/content/blog/](src/content/blog/) | One `.md`/`.mdx` file per post. |
| `/tags/<tag>/` | generated | One page per tag used by a published post. |
| `/showcase/` | [src/pages/showcase/index.astro](src/pages/showcase/index.astro) | Hub listing the showcases. |
| `/showcase/<slug>/` | [public/showcase/](public/showcase/) | Nine self-contained pages, copied verbatim, sharing one system stylesheet. |
| `/rss.xml` | [src/pages/rss.xml.js](src/pages/rss.xml.js) | Published posts only. |
| `/sitemap-index.xml` | generated | Includes the `public/` showcases via `customPages`. |
| `/404` | [src/pages/404.astro](src/pages/404.astro) | |

Retired routes redirect from [public/_redirects](public/_redirects): `/membukkit/`
and `/context-engine/` fold into the pages above, and `/claude-code/` goes to the
docs page it always mirrored.

## Where things live

Marketing pages are light-only. The blog, the showcases and the docs also carry a
dark theme, chosen with `data-theme` and persisted to `localStorage['ms-theme']`.

- [src/styles/tokens.css](src/styles/tokens.css) is the **only** place colours,
  spacing, radii and the type scale are declared. `scripts/sync-chrome.mjs`
  generates `public/chrome.css` from it plus `site.css`, so the standalone
  showcase pages under `public/` use the same values without a second copy.
- [src/data/](src/data/) holds the editorial content the pages render: the
  renewal story, the hero demo's four frames, the how-it-works stages and FAQ.
  Change the words there, not in a component.
- [src/lib/benchmark-data.ts](src/lib/benchmark-data.ts) is the single benchmark
  dataset. The ranked bars are **derived** from it rather than restated.
- [src/scripts/](src/scripts/) holds the behaviour. `sticky-story.ts` and
  `customer-motion.ts` are load-bearing; `atmosphere.ts` and `feel-polish.ts`
  are decorative and can be deleted without losing information.
- `npm run check` runs `astro check`, then `scripts/check-tokens.mjs` (fails on
  any `var(--x)` with no definition) and `scripts/check-contrast.mjs` (fails on
  any ink-on-surface pair below its bar, in both themes).
- `scripts/prepare-assets.sh` regenerates the DM Sans subsets and the decorative
  WebP from the redesign package. The outputs are committed; it only needs
  running when an original changes.

## Quick start

```bash
cd marketing
nvm use          # Node 22, pinned in .nvmrc
npm install
npm run dev      # http://localhost:4321
```

`npm run dev` shows drafts. Everything else hides them.

### Commands

| Command | What it does |
| --- | --- |
| `npm run dev` | Astro dev server, hot reload, **drafts visible**. |
| `npm run build` | Production build into `dist/`. Drafts excluded. |
| `npm run preview` | Builds, then serves through `wrangler dev` — the closest thing to production locally. |
| `npm run deploy` | Builds, then `wrangler deploy`. See [Publishing](#publishing). |
| `npm run cf-typegen` | Regenerates `worker-configuration.d.ts` from `wrangler.jsonc`. |
| `npm run check` | `astro check`, then the token and contrast checks. |

## Layout

```
marketing/
├─ astro.config.mjs        site URL, markdown pipeline, sitemap
├─ wrangler.jsonc          Cloudflare deploy config
├─ scripts/                asset prep, chrome sync, the two checks
├─ public/                 copied to dist/ verbatim, never parsed
│  ├─ chrome.css           GENERATED from src/styles; do not edit
│  ├─ fonts/               DM Sans subsets + the OFL licence
│  ├─ apps/                the four app marks used on the org graph
│  ├─ glass-fold.webp      the decorative fold behind page heads
│  ├─ showcase/
│  │  ├─ showcase.css      showcase-only layers; tokens come from chrome.css
│  │  ├─ showcase.js       persisted theme toggle + scroll reveals
│  │  └─ <slug>/index.html one page per showcase
│  ├─ _headers             security + cache headers
│  ├─ _redirects           retired routes
│  ├─ robots.txt
│  └─ og-default.png       fallback social card
└─ src/
   ├─ content.config.ts    the blog frontmatter schema — the source of truth
   ├─ content/blog/        posts + colocated images
   ├─ data/                editorial content for the marketing pages
   ├─ scripts/             page behaviour, imported per page
   ├─ layouts/             BaseLayout (head, chrome, theme), PostLayout
   ├─ components/          chrome, the homepage scene, the blog diagrams
   ├─ lib/                 posts.ts, benchmark-data.ts, links.ts
   ├─ pages/               routed pages
   └─ styles/              tokens.css, site.css, story.css, bench.css, prose.css
```

## Writing a blog post

Create one file. The filename becomes the URL slug:
`src/content/blog/my-post.mdx` → `/blog/my-post/`.

```yaml
---
title: "Post title — becomes the h1 and the <title>"
description: "One or two sentences. Used in listings, meta description, and RSS."
date: 2026-07-26           # publication date; also the sort key
updated: 2026-07-30        # optional; shown in the byline
author: Memseek           # optional; defaults to "Memseek"
tags: ["Agent memory"]     # optional; each tag gets a /tags/<slug>/ page
draft: true                # optional; dev-only, never built for production
cover: ./images/cover.png  # optional; relative path, used as hero + og:image
coverAlt: "Description."   # set whenever cover conveys meaning
---
```

`title`, `description` and `date` are required. A mistyped or unknown field
fails the build with a message naming the file and the field. The schema is
[src/content.config.ts](src/content.config.ts) — change it there, not in a layout.

**Drafts.** `draft: true` renders in `npm run dev` and is excluded from the
production build, the post list, RSS, and the sitemap. It is the safe way to
work in the open.

**`.md` vs `.mdx`.** Use `.md` unless you need components. `.mdx` lets you
`import` and use Astro components — notably `<Image>` for optimized images.

**Images.** Put them in `src/content/blog/images/` next to the post and
reference them relatively. Astro optimizes, resizes, and fingerprints them at
build time. In `.mdx`:

```mdx
import { Image } from 'astro:assets';
import diagram from './images/my-diagram.png';

<figure>
  <Image src={diagram} alt="What the diagram shows." widths={[700, 1100, 1400]}
         sizes="(max-width: 900px) 100vw, 820px" />
  <figcaption>Caption.</figcaption>
</figure>
```

**Math.** `remark-math` + KaTeX are wired up. Inline `$\tau$`, display `$$ … $$`.

**Code.** Fenced blocks are highlighted by Shiki with a dual light/dark theme
that follows the site theme toggle. Long lines wrap rather than scroll.

**Tables** are automatically wrapped in a scroll container, so a wide table
never makes the page scroll sideways.

**Headings.** `h2`–`h4` get `id`s and a hover anchor link automatically.

**Callouts.** `<aside class="note"><span class="label">Label</span>Text</aside>`,
or `class="warn"` for the amber variant. Styled by `prose.css`.

[src/content/blog/authoring-reference.mdx](src/content/blog/authoring-reference.mdx)
is a permanent `draft: true` page demonstrating every one of these. Run
`npm run dev` and open `/blog/authoring-reference/` to see them rendered.

**Before publishing:** flip `draft` off (or remove it), confirm `description`
reads well as a social preview, set `coverAlt` if there's a cover, and check
`npm run build` passes.

## Adding a showcase

A showcase is one hand-written HTML page in `public/`, with no build step. It
splits in two:

- **The system layer** — [public/showcase/showcase.css](public/showcase/showcase.css)
  and [showcase.js](public/showcase/showcase.js), linked by every showcase. Tokens,
  base, nav, footer, and the primitives DESIGN.md names: `.eyebrow`, `.region`,
  `.panel`, `.badge`, `.btn`, `.hero`, `.rv`. This is what makes the showcases
  read as one system, and it is the only place their shared values live.
- **The page layer** — an inline `<style>` holding only that page's own
  machinery (gbrain's readout and graph; dreams' diff ledger and demo).

To add one:

1. Drop the file at `public/showcase/<slug>/index.html`. The directory +
   `index.html` naming gives it a trailing-slash URL, matching the rest of the site.
2. Link the system layer and use its primitives:
   `<link rel="stylesheet" href="../showcase.css">` and
   `<script src="../showcase.js"></script>`. Copy the nav, footer, skip link and
   the pre-paint theme snippet from an existing showcase rather than rewriting them.
3. Put anything genuinely reusable in `showcase.css`, and everything else in the
   page's own `<style>`. If you find yourself copying a rule between two showcases,
   it belongs in the system layer.
4. Make every internal link **site-absolute** (`/#kit`, `/blog/`, `/showcase/`).
   Relative links like `../docs/foo.md` worked when these lived in `examples/`
   and will 404 in production.
5. Add `<link rel="canonical">` and `og:`/`twitter:` meta to the `<head>` —
   nothing else can inject them into a verbatim-copied file.
6. Register it in the `showcases` array in
   [src/pages/showcase/index.astro](src/pages/showcase/index.astro) so it appears
   on the hub.
7. Add its URL to `customPages` in [astro.config.mjs](astro.config.mjs). Astro
   can't discover pages in `public/`, so it won't reach the sitemap otherwise.

### House rules the showcases follow

- **Type.** JetBrains Mono for headings, labels, values, and code; Inter for
  prose. Anything below `--step--1` uses the four-step sub-body scale in
  `showcase.css` (`--micro`, `--label`, `--meta`, `--copy`) rather than a new
  arbitrary size.
- **Hue.** A region sets `--hue` once (`style="--hue: var(--cyan)"`) and its
  eyebrow, chips, and accents inherit it. A second hue inside a region has to
  carry a second *meaning*, not decoration.
- **Light theme is not an inversion.** Hue-coloured small text must go through
  the `.hue-ink` mix; the raw signal hues only clear ~3.4:1 on white. See the
  comment block in `showcase.css`.
- **No halos.** Depth comes from tonal layering and hairline rules. A
  zero-offset coloured glow is never elevation here.
- **The gradient is spent once.** Orange→pink belongs to the primary action and,
  at most, one headline word.

Files in `public/` are never parsed, bundled, or type-checked. A broken showcase
still builds — check it in `npm run dev`.

## Publishing

Deploys to Cloudflare Workers with static assets (`wrangler.jsonc`: worker name
`memseek`, assets served from `dist/`).

**One-time setup:**

```bash
npx wrangler login          # or export CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in CI
```

**Deploy:**

```bash
npm run deploy              # build + wrangler deploy
```

**Check it locally first** with `npm run preview` — that runs the real Worker
runtime against the real `dist/`, including `_headers`, which `npm run dev`
does not.

### Site URL

`astro.config.mjs` reads `SITE_URL`, defaulting to `https://memseek.ai`.
It affects absolute URLs only — RSS, sitemap, canonical, `og:url` — never the
page markup. When a custom domain is attached, set it at build time:

```bash
SITE_URL=https://memseek.ai npm run deploy
```

and update the three places that hardcode the host, which `SITE_URL` cannot reach:

- [public/robots.txt](public/robots.txt) — the `Sitemap:` line
- [public/showcase/gbrain/index.html](public/showcase/gbrain/index.html) — `canonical`, `og:url`, `og:image`
- [public/showcase/dreams/index.html](public/showcase/dreams/index.html) — same three

### Headers and caching

[public/_headers](public/_headers) is deployed with the assets: security headers
on everything, immutable year-long caching for fingerprinted `/_astro/*`, a day
for images, an hour for RSS. HTML is left on Cloudflare's default so a deploy is
visible immediately.

## Known rough edges

- The nine showcase pages hardcode `https://memseek.ai/...` in their `canonical`
  and `og:url`, and `public/robots.txt` hardcodes the sitemap host. `SITE_URL`
  cannot reach any of them.
- `public/_headers` sets no CSP. There is now one fewer external origin to allow
  (fonts are self-hosted), so this is easier than it was.
- The blog's cover images in [src/content/blog/images/](src/content/blog/images/)
  are raster art in the retired dark palette. The SVG diagrams were recoloured;
  these were not, because they need regenerating rather than remapping.
- [docs/stylesheets/extra.css](../docs/stylesheets/extra.css) mirrors
  `src/styles/tokens.css` by hand. The docs build from the repo root with no
  access to this directory, so a change here needs copying there.
