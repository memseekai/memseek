// @ts-check
import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import sitemap from '@astrojs/sitemap';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import rehypeSlug from 'rehype-slug';
import rehypeAutolinkHeadings from 'rehype-autolink-headings';

import cloudflare from "@astrojs/cloudflare";

// Set SITE_URL in the Cloudflare Pages build env once a custom domain is attached.
// It only affects absolute URLs (RSS, sitemap, og:url) — never the page markup.
const site = process.env.SITE_URL ?? 'https://memseek.ai';

/**
 * Wraps every <table> in <div class="table-scroll"> so wide tables scroll
 * inside themselves instead of making the whole page scroll sideways.
 * Hand-rolled to avoid pulling in unist-util-visit for six lines of work.
 */
function rehypeWrapTables() {
  /** @param {any} tree */
  return (tree) => {
    /** @param {any} node */
    const walk = (node) => {
      if (!node.children) return;
      node.children = node.children.map((/** @type {any} */ child) => {
        walk(child);
        if (child.type === 'element' && child.tagName === 'table') {
          return {
            type: 'element',
            tagName: 'div',
            properties: { className: ['table-scroll'] },
            children: [child],
          };
        }
        return child;
      });
    };
    walk(tree);
  };
}

export default defineConfig({
  site,

  integrations: [
    mdx(),
    sitemap({
      // The showcases live in public/, so Astro can't discover them.
      // /showcase/ itself IS a page, so it's found.
      customPages: [
        new URL('/showcase/gbrain/', site).href,
        new URL('/showcase/dreams/', site).href,
        new URL('/showcase/living-profile/', site).href,
        new URL('/showcase/skill/', site).href,
        new URL('/showcase/self-audit/', site).href,
        new URL('/showcase/generative-agents/', site).href,
        new URL('/showcase/mcp/', site).href,
        new URL('/showcase/workspace-explorer/', site).href,
        new URL('/showcase/agent-memory/', site).href,
      ],
    }),
  ],

  markdown: {
    remarkPlugins: [remarkMath],
    rehypePlugins: [
      rehypeKatex,
      rehypeSlug,
      [
        rehypeAutolinkHeadings,
        {
          behavior: 'append',
          properties: { className: ['head-anchor'], ariaHidden: 'true', tabIndex: -1 },
          content: { type: 'text', value: '#' },
          // Don't anchor the KaTeX-generated markup or the h1 (the page title).
          test: (/** @type {any} */ el) => ['h2', 'h3', 'h4'].includes(el.tagName),
        },
      ],
      rehypeWrapTables,
    ],
    shikiConfig: {
      // Dual theme: prose.css swaps the CSS vars when data-theme="light".
      themes: { light: 'github-light', dark: 'github-dark-default' },
      wrap: true,
    },
  },

  build: {
    // /blog/my-post/index.html — trailing-slash URLs, which is what CF Pages serves best.
    format: 'directory',
  },

  adapter: cloudflare()
});
