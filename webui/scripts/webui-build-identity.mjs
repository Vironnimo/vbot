// Vite plugin that stamps a production build with its WebUI build identity
// (contract: src/lib/webuiBuild.js). The id hashes the names of every emitted
// file; Vite puts a content hash into each name, so the id changes exactly
// when the output does, and an unchanged rebuild keeps it.
import { createHash } from 'node:crypto';

import {
  WEBUI_BUILD_FILE,
  WEBUI_BUILD_META_NAME,
} from '../src/lib/webuiBuild.js';

const PAGE = 'index.html';

export function webuiBuildIdentity() {
  return {
    name: 'vbot-webui-build-identity',
    apply: 'build',
    generateBundle: {
      // After Vite has emitted index.html and every chunk and asset.
      order: 'post',
      handler(_options, bundle) {
        const page = bundle[PAGE];
        if (page?.type !== 'asset') return;
        const hash = createHash('sha256');
        for (const fileName of Object.keys(bundle).sort()) {
          if (fileName !== PAGE) hash.update(`${fileName}\n`);
        }
        const id = hash.digest('hex').slice(0, 16);
        const html =
          typeof page.source === 'string'
            ? page.source
            : new TextDecoder().decode(page.source);
        page.source = html.replace(
          '</head>',
          `  <meta name="${WEBUI_BUILD_META_NAME}" content="${id}" />\n  </head>`,
        );
        this.emitFile({
          type: 'asset',
          fileName: WEBUI_BUILD_FILE,
          source: `${JSON.stringify({ build_id: id })}\n`,
        });
      },
    },
  };
}
