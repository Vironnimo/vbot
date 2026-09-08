import { existsSync, readFileSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

import { afterEach, describe, expect, it } from 'vitest';

const webuiRoot = resolve(import.meta.dirname, '..', '..');
const projectRoot = resolve(webuiRoot, '..');
const fixtureRoot = resolve(
  projectRoot,
  'tests',
  'fixtures',
  'extension-pages',
);
const fixtureOwners = ['alpha', 'beta'];

function pageBuild(owner) {
  return resolve(fixtureRoot, owner, 'web');
}

afterEach(() => {
  for (const owner of fixtureOwners)
    rmSync(pageBuild(owner), { recursive: true, force: true });
});

describe('build-extension-pages', () => {
  it('emits each fixture page with relative hashed JavaScript and CSS assets', () => {
    execFileSync(process.execPath, ['scripts/build-extension-pages.mjs'], {
      cwd: webuiRoot,
      stdio: 'pipe',
    });

    for (const owner of fixtureOwners) {
      const build = pageBuild(owner);
      const html = readFileSync(resolve(build, 'page.html'), 'utf8');
      const imports = Array.from(
        html.matchAll(/(?:src|href)="\.\/assets\/([^"]+\.(?:js|css))"/g),
        (match) => match[1],
      );

      expect(imports).toEqual(
        expect.arrayContaining([
          expect.stringMatching(/\.js$/),
          expect.stringMatching(/\.css$/),
        ]),
      );
      expect(imports).toHaveLength(2);
      for (const asset of imports)
        expect(existsSync(resolve(build, 'assets', asset))).toBe(true);
      expect(html).not.toMatch(/(?:src|href)="\/assets\//);
      const script = imports.find((asset) => asset.endsWith('.js'));
      expect(readFileSync(resolve(build, 'assets', script), 'utf8')).toContain(
        'vbot.extension.init',
      );
    }
    // Check the production page too: shared CSS must not import fonts that its
    // opaque-origin CSP blocks, and every bundled font must actually ship.
    const swarmBuild = resolve(projectRoot, 'resources/extensions/swarm/web');
    const swarmHtml = readFileSync(resolve(swarmBuild, 'page.html'), 'utf8');
    const stylesheet = swarmHtml.match(/href="(\.\/assets\/[^"]+\.css)"/)[1];
    const css = readFileSync(resolve(swarmBuild, stylesheet), 'utf8');
    expect(css).not.toMatch(/fonts\.(googleapis|gstatic)\.com/);
    expect(
      readFileSync(resolve(swarmBuild, 'fonts/OFL.txt'), 'utf8'),
    ).toContain('SIL OPEN FONT LICENSE');
    const fonts = [
      ...css.matchAll(/url\((?:["']?)([^)"']+\.ttf)(?:["']?)\)/g),
    ].map((match) => match[1]);
    expect(fonts).toHaveLength(6);
    for (const font of fonts)
      expect(existsSync(resolve(swarmBuild, 'assets', font))).toBe(true);
  }, 30_000);
});
