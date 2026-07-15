import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const API_FILE = join(SRC_DIR, 'lib', 'api.js');
const METHOD_LITERAL_EXCLUSIONS = new Set(['lib/i18n.js']);

function collectProductionSources(directory) {
  const files = [];
  for (const entry of readdirSync(directory)) {
    const fullPath = join(directory, entry);
    if (statSync(fullPath).isDirectory()) {
      if (entry !== '__tests__') {
        files.push(...collectProductionSources(fullPath));
      }
    } else if (entry.endsWith('.js') || entry.endsWith('.svelte')) {
      files.push(fullPath);
    }
  }
  return files.filter((filePath) => filePath !== API_FILE);
}

function sourceLabel(filePath) {
  return relative(SRC_DIR, filePath).split(sep).join('/');
}

describe('RPC ownership guard', () => {
  it('keeps the raw rpc transport inside lib/api.js', () => {
    const violations = [];
    for (const filePath of collectProductionSources(SRC_DIR)) {
      const source = readFileSync(filePath, 'utf8');
      if (/\brpc\s*\(/.test(source)) {
        violations.push(sourceLabel(filePath));
      }
    }

    expect(violations).toEqual([]);
  });

  it('keeps known server method names inside lib/api.js', () => {
    const apiSource = readFileSync(API_FILE, 'utf8');
    const serverMethods = new Set(
      [...apiSource.matchAll(/\brpc\(\s*['"]([^'"]+)['"]/g)].map(
        (match) => match[1],
      ),
    );
    const violations = [];

    for (const filePath of collectProductionSources(SRC_DIR)) {
      if (METHOD_LITERAL_EXCLUSIONS.has(sourceLabel(filePath))) {
        continue;
      }
      const source = readFileSync(filePath, 'utf8');
      for (const literal of source.matchAll(/['"]([^'"\r\n]+)['"]/g)) {
        if (serverMethods.has(literal[1])) {
          violations.push(`${sourceLabel(filePath)}: ${literal[1]}`);
        }
      }
    }

    expect(violations).toEqual([]);
  });
});
