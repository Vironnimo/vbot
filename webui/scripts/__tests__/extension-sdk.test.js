import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  symlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { build } from 'vite';
import { expect, it } from 'vitest';
import { buildExtensionSdk, packExtensionSdk } from '../extension-sdk.mjs';

const webui = resolve(import.meta.dirname, '../..');

// Packaging must exercise npm's file selection and a real Svelte build. This
// smoke deliberately runs outside the checkout and has no product-source alias.
it('builds an external Extension from the shipped SDK tarball', async () => {
  const temporary = mkdtempSync(join(tmpdir(), 'vbot-extension-sdk-'));
  try {
    const prepared = buildExtensionSdk({ output: join(temporary, 'sdk') });
    const archive = packExtensionSdk({
      directory: prepared.directory,
      destination: temporary,
    });
    const unpacked = join(temporary, 'unpacked');
    execFileSync(
      'python',
      [
        '-c',
        'import sys, tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter="data")',
        archive,
        unpacked,
      ],
      { windowsHide: true },
    );
    const consumer = join(temporary, 'extension');
    const installed = join(consumer, 'node_modules/@vbot/extension-sdk');
    mkdirSync(join(consumer, 'node_modules/@vbot'), { recursive: true });
    renameSync(join(unpacked, 'package'), installed);
    cpSync(join(installed, 'example'), consumer, { recursive: true });
    symlinkSync(
      join(webui, 'node_modules/@sveltejs'),
      join(consumer, 'node_modules/@sveltejs'),
      'junction',
    );
    const { extensionPageConfig } = await import(
      /* @vite-ignore */ pathToFileURL(join(installed, 'build.mjs')).href
    );
    expect(existsSync(join(installed, 'dist/fonts/OFL.txt'))).toBe(true);
    expect(existsSync(join(installed, 'LICENSE'))).toBe(true);
    expect(existsSync(join(installed, 'dist/lib/api.js'))).toBe(false);
    expect(existsSync(join(installed, 'dist/lib/api'))).toBe(false);
    const manifest = JSON.parse(
      readFileSync(join(consumer, 'extension.json'), 'utf8'),
    );
    expect(manifest.api_version).toBe(30);
    expect(manifest.contributions[0].surface).toBe('chat.composer.actions');

    expect(() =>
      extensionPageConfig({
        entry: join(consumer, 'ui/page.html'),
        outDir: consumer,
      }),
    ).toThrow();

    const config = extensionPageConfig({
      entry: join(consumer, 'ui/page.html'),
      outDir: join(consumer, 'web'),
    });
    await build({
      ...config,
      configFile: false,
      logLevel: 'silent',
      // Reuse only the installed third-party build toolchain, not vBot sources.
      resolve: {
        alias: [
          ['svelte/internal/client', 'svelte/src/internal/client/index.js'],
          [
            'svelte/internal/disclose-version',
            'svelte/src/internal/disclose-version.js',
          ],
          ['svelte/reactivity', 'svelte/src/reactivity/index-client.js'],
          ['svelte', 'svelte/src/index-client.js'],
        ].map(([find, path]) => ({
          find,
          replacement: join(webui, 'node_modules', path),
        })),
      },
      plugins: [
        ...config.plugins,
        {
          name: 'reject-checkout-source',
          load(id) {
            if (
              id
                .replaceAll('\\', '/')
                .startsWith(`${webui.replaceAll('\\', '/')}/src/`)
            )
              throw new Error(
                `External SDK build imported checkout source: ${id}`,
              );
          },
        },
      ],
    });
    const html = readFileSync(join(consumer, 'web/page.html'), 'utf8');
    expect(html).toMatch(/src="\.\/assets\/[^"/]+\.js"/);
    expect(html).toMatch(/href="\.\/assets\/[^"/]+\.css"/);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}, 30_000);
