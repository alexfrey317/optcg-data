// @ts-check
import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://optcg-ladder.pages.dev',
  output: 'static',
  build: { format: 'directory' },
  trailingSlash: 'ignore',
});
