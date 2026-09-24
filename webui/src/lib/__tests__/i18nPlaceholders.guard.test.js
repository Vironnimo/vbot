import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { englishCatalog } from '../i18n.js';

// Guard scan: `t(key, fallback, values)` prefers the English catalog entry over
// the fallback, so a placeholder that exists only in the call-site fallback is
// silently dropped from the rendered text. Every placeholder a literal
// fallback uses must therefore also appear in the key's English catalog entry.

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

const TRANSLATE_CALL =
  /\bt\(\s*(['"`])([\w.-]+)\1\s*,\s*(['"`])((?:\\.|(?!\3)[\s\S])*?)\3/g;

function placeholders(text) {
  return new Set([...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]));
}

describe('i18n placeholder guard', () => {
  it('keeps every fallback placeholder in the English catalog entry', () => {
    const violations = [];
    for (const file of collectSourceFiles(SRC_DIR)) {
      const source = readFileSync(file, 'utf8');
      for (const match of source.matchAll(TRANSLATE_CALL)) {
        const [, , key, , fallback] = match;
        if (!Object.hasOwn(englishCatalog, key)) {
          continue;
        }
        const available = placeholders(englishCatalog[key]);
        const missing = [...placeholders(fallback)].filter(
          (name) => !available.has(name),
        );
        if (missing.length > 0) {
          violations.push(
            `${relative(SRC_DIR, file)}: ${key} lacks {${missing.join('}, {')}}`,
          );
        }
      }
    }

    expect(violations).toEqual([]);
  });
});
