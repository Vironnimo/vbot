import path from 'path';

import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

import { webuiBuildIdentity } from './scripts/webui-build-identity.mjs';

export default defineConfig({
  plugins: [svelte(), webuiBuildIdentity()],
  resolve: {
    alias: {
      $lib: path.resolve(import.meta.dirname, 'src/lib'),
    },
  },
  test: {
    alias: [
      {
        // Vitest runs in Node, but our Svelte state models are browser code.
        find: /^svelte\/reactivity$/,
        replacement: path.resolve(
          import.meta.dirname,
          'node_modules/svelte/src/reactivity/index-client.js',
        ),
      },
    ],
    setupFiles: ['src/__tests__/environment.support.js'],
  },
  build: {
    chunkSizeWarningLimit: 800,
    minify: 'terser',
    rolldownOptions: {
      output: {
        // Third-party code lives in its own chunks so app releases no longer
        // invalidate the browser cache for unchanged libraries. markdown-it
        // gets a dedicated chunk because it is the largest library and only
        // grows with markdown features, never with app code. The @xterm and
        // tldts packages match no group on purpose: they are loaded through
        // dynamic import() and must remain separate on-demand chunks. Other
        // libraries are grouped by their actual consumers so a lazy view's
        // dependency (such as Cron's cronstrue) stays out of startup imports.
        codeSplitting: {
          groups: [
            {
              name: 'markdown',
              test: /[\\/]node_modules[\\/](markdown-it|linkify-it|mdurl|uc\.micro|entities)[\\/]/,
              minSize: 0,
              entriesAware: true,
              priority: 20,
            },
            {
              name: 'vendor',
              test: /[\\/]node_modules[\\/](?!@xterm[\\/]|tldts(-core)?[\\/])/,
              minSize: 0,
              entriesAware: true,
              priority: 10,
            },
          ],
        },
      },
    },
  },
});
