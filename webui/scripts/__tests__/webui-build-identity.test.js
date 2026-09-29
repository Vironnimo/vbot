import { describe, expect, it } from 'vitest';

import { webuiBuildIdentity } from '../webui-build-identity.mjs';

// Runs the plugin over a fake Vite bundle and returns the stamped page and
// the emitted build.json payload.
function stamp(fileNames) {
  const bundle = {
    'index.html': {
      type: 'asset',
      source: '<html><head><title>vBot</title></head></html>',
    },
  };
  for (const fileName of fileNames) {
    bundle[fileName] = { type: 'chunk' };
  }
  const emitted = [];
  webuiBuildIdentity().generateBundle.handler.call(
    { emitFile: (file) => emitted.push(file) },
    {},
    bundle,
  );
  const [file] = emitted;
  expect(file.fileName).toBe('build.json');
  const buildId = JSON.parse(file.source).build_id;
  const html = bundle['index.html'].source;
  expect(html).toContain(
    `<meta name="vbot-webui-build" content="${buildId}" />`,
  );
  return buildId;
}

describe('webuiBuildIdentity', () => {
  it('stamps the page and build.json with one id that follows the output files', () => {
    const buildId = stamp(['assets/index-a1.js', 'assets/index-b2.css']);

    expect(stamp(['assets/index-b2.css', 'assets/index-a1.js'])).toBe(buildId);
    expect(stamp(['assets/index-c3.js', 'assets/index-b2.css'])).not.toBe(
      buildId,
    );
  });
});
