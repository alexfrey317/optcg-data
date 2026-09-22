// @ts-check
import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://optcg-data.pages.dev',
  output: 'static',
  build: { format: 'directory' },
  trailingSlash: 'ignore',
  redirects: { '/ladder/1': '/' },
});
