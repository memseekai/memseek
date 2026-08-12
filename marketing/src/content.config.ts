import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

/**
 * One post = one .md/.mdx file in src/content/blog/.
 * The filename (minus extension) is the URL slug: my-post.mdx -> /blog/my-post/
 * Colocate images next to the post and reference them relatively; Astro
 * optimizes and fingerprints them at build time.
 */
const blog = defineCollection({
  loader: glob({ base: './src/content/blog', pattern: '**/*.{md,mdx}' }),
  schema: ({ image }) =>
    z.object({
      title: z.string(),
      description: z.string(),
      date: z.coerce.date(),
      updated: z.coerce.date().optional(),
      author: z.string().default('Memseek'),
      tags: z.array(z.string()).default([]),
      /** Optional art direction for essays with a dedicated visual treatment. */
      presentation: z.enum(['loop-essay']).optional(),
      /** Hidden from listings, RSS and sitemap; still built in `astro dev`. */
      draft: z.boolean().default(false),
      /** Relative path to a colocated image, e.g. ./images/cover.png */
      cover: image().optional(),
      coverAlt: z.string().optional(),
    }),
});

export const collections = { blog };
