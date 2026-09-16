/**
 * Off-site destinations, named once.
 *
 * These appear in the header, the footer, the showcases and half the body
 * copy. When the docs move off GitHub Pages, this is the file that changes.
 */
export const DOCS_URL = 'https://memseekai.github.io/memseek/';
export const REPO_URL = 'https://github.com/memseekai/memseek';
export const MEMBUKKIT_REPO_URL = 'https://github.com/memseekai/membukkit';

/** Deep links into the docs, used from body copy on the marketing pages. */
export const docsPage = (slug: string) => `${DOCS_URL}${slug}/`;

/** The benchmark guide MemBukkit publishes; the source for every number
    on /benchmarks/. */
export const BENCHMARK_GUIDE_URL = `${MEMBUKKIT_REPO_URL}/blob/main/docs/guide/benchmarks.md`;

/** Intake form. The site runs no backend of its own. */
export const CONTACT_URL = 'https://tally.so/r/EkrGDN';
