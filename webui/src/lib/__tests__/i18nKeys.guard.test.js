import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { englishCatalog } from '../i18n.js';

// Guard scan: `t(key)` without an English fallback renders the raw key when
// the English catalog lacks it. A call with a literal fallback may rely on that
// fallback, but every literal key used without one must resolve in the catalog.

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function collectSourceFiles(directory) {
  const files = [];
  for (const entry of readdirSync(directory)) {
    if (entry === '__tests__' || entry === 'node_modules') {
      continue;
    }
    const fullPath = join(directory, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...collectSourceFiles(fullPath));
    } else if (/\.(js|svelte)$/.test(entry)) {
      files.push(fullPath);
    }
  }
  return files;
}

// A literal key followed by `)` or by an empty fallback (undefined, null, '').
const KEY_WITHOUT_FALLBACK =
  /\bt\(\s*(['"])([\w.-]+)\1\s*(?:\)|,\s*(?:undefined|null|''|"")\s*[,)])/g;

describe('i18n key guard', () => {
  it('resolves every literal key used without an English fallback', () => {
    const violations = [];
    for (const file of collectSourceFiles(SRC_DIR)) {
      const source = readFileSync(file, 'utf8');
      for (const [, , key] of source.matchAll(KEY_WITHOUT_FALLBACK)) {
        if (!Object.hasOwn(englishCatalog, key)) {
          violations.push(
            `${relative(SRC_DIR, file).split(sep).join('/')}: ${key}`,
          );
        }
      }
    }

    expect(violations).toEqual([]);
  });
});
