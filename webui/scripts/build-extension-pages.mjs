import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { build } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const root = resolve(import.meta.dirname, '..', '..');
const roots = [
  resolve(root, 'resources', 'extensions'),
  resolve(root, 'tests', 'fixtures', 'extension-pages'),
];

const pages = roots.flatMap((parent) =>
  existsSync(parent)
    ? readdirSync(parent, { withFileTypes: true })
        .filter((item) => item.isDirectory())
        .map((item) => resolve(parent, item.name, 'ui', 'page.html'))
        .filter(existsSync)
    : [],
);

for (const entry of pages) {
  const ui = resolve(entry, '..');
  await build({
    root: ui,
    base: './',
    resolve: {
      alias: [
        { find: '$lib', replacement: resolve(root, 'webui', 'src', 'lib') },
        {
          find: 'svelte/internal/client',
          replacement: resolve(
            root,
            'webui',
            'node_modules',
            'svelte',
            'src',
            'internal',
            'client',
            'index.js',
          ),
        },
        {
          find: 'svelte/internal/disclose-version',
          replacement: resolve(
            root,
            'webui',
            'node_modules',
            'svelte',
            'src',
            'internal',
            'disclose-version.js',
          ),
        },
        {
          find: 'svelte/reactivity',
          replacement: resolve(
            root,
            'webui',
            'node_modules',
            'svelte',
            'src',
            'reactivity',
            'index-client.js',
          ),
        },
        {
          find: 'svelte',
          replacement: resolve(
            root,
            'webui',
            'node_modules',
            'svelte',
            'src',
            'index-client.js',
          ),
        },
      ],
    },
    plugins: [
      svelte(),
      {
        name: 'bundled-font-license',
        generateBundle() {
          this.emitFile({
            type: 'asset',
            fileName: 'fonts/OFL.txt',
            source: readFileSync(resolve(root, 'webui/public/fonts/OFL.txt')),
          });
        },
      },
    ],
    publicDir: false,
    build: {
      outDir: resolve(ui, '..', 'web'),
      emptyOutDir: true,
      copyPublicDir: false,
      minify: 'terser',
      rolldownOptions: { input: entry },
    },
  });
}
