import { readFileSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/** Build a portable page with the same Svelte and asset policy as bundled pages. */
export function extensionPageConfig({
  entry = 'ui/page.html',
  outDir = 'web',
} = {}) {
  const absoluteEntry = resolve(entry);
  const absoluteOutput = resolve(outDir);
  const entryFromOutput = relative(absoluteOutput, absoluteEntry);
  if (
    !isAbsolute(entryFromOutput) &&
    entryFromOutput !== '..' &&
    !entryFromOutput.startsWith(`..${sep}`)
  )
    throw new Error('Extension build output must not contain its source entry');
  return {
    root: dirname(absoluteEntry),
    base: './',
    plugins: [
      svelte(),
      {
        name: 'vbot-extension-font-license',
        generateBundle() {
          this.emitFile({
            type: 'asset',
            fileName: 'fonts/OFL.txt',
            source: readFileSync(
              fileURLToPath(new URL('./dist/fonts/OFL.txt', import.meta.url)),
            ),
          });
        },
      },
    ],
    publicDir: false,
    build: {
      outDir: absoluteOutput,
      emptyOutDir: true,
      copyPublicDir: false,
      minify: 'terser',
      rolldownOptions: { input: absoluteEntry },
    },
  };
}
