import path from 'path';

import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [svelte()],
  resolve: {
    alias: {
      $lib: path.resolve(import.meta.dirname, 'src/lib'),
    },
  },
  build: {
    chunkSizeWarningLimit: 800,
    minify: 'terser',
    rolldownOptions: {
      output: {
        // Third-party code lives in its own chunks so app releases no longer
        // invalidate the browser cache for unchanged libraries. markdown-it
        // gets a dedicated chunk because it is the largest library and only
        // grows with markdown features, never with app code. The @xterm
        // packages match no group on purpose: they are loaded through
        // dynamic import() and must remain separate on-demand chunks.
        codeSplitting: {
          groups: [
            {
              name: 'markdown',
              test: /[\\/]node_modules[\\/](markdown-it|linkify-it|mdurl|uc\.micro|entities)[\\/]/,
              minSize: 0,
              priority: 20,
            },
            {
              name: 'vendor',
              test: /[\\/]node_modules[\\/]/,
              minSize: 0,
              priority: 10,
            },
          ],
        },
      },
    },
  },
});
