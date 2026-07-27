import rss from '@astrojs/rss';
import { getPosts } from '../lib/posts';

export async function GET(context) {
  const posts = await getPosts();
  return rss({
    title: 'Memseek — blog',
    description:
      'Notes on agent memory: why similarity retrieval serves stale answers, and how to serve current, dated, source-linked context.',
    site: context.site,
    // Descriptions only, not full bodies — post HTML references optimized,
    // fingerprinted image URLs that would rot in a feed reader's cache.
    items: posts
      .filter((post) => !post.data.draft)
      .map((post) => ({
        title: post.data.title,
        description: post.data.description,
        pubDate: post.data.date,
        link: `/blog/${post.id}/`,
        categories: post.data.tags,
        author: post.data.author,
      })),
    customData: '<language>en-us</language>',
  });
}
