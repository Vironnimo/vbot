import { existsSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { build } from 'vite';
import { extensionPageConfig } from '../extension-sdk/build.mjs';
import {
  buildExtensionSdk,
  extensionSdkSourceAliases,
  packExtensionSdk,
} from './extension-sdk.mjs';

const root = resolve(import.meta.dirname, '..', '..');
buildExtensionSdk();
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
    ...extensionPageConfig({ entry, outDir: resolve(ui, '..', 'web') }),
    resolve: {
      alias: [
        ...extensionSdkSourceAliases(),
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
  });
}

packExtensionSdk();
