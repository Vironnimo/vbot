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
    rollupOptions: {
      output: {
        // Third-party code lives in its own chunks so app releases no longer
        // invalidate the browser cache for unchanged libraries. markdown-it
        // gets a dedicated chunk because it is the largest library and only
        // grows with markdown features, never with app code. The @xterm
        // packages stay out of manual chunking: they are loaded through
        // dynamic import() and must remain separate on-demand chunks.
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined;
          }
          if (/[\\/]node_modules[\\/]@xterm[\\/]/.test(id)) {
            return undefined;
          }
          if (
            /[\\/]node_modules[\\/](markdown-it|linkify-it|mdurl|uc\.micro|entities)[\\/]/.test(
              id,
            )
          ) {
            return 'markdown';
          }
          return 'vendor';
        },
      },
    },
  },
});
