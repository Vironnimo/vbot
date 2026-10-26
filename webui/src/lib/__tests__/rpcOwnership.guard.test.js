import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const API_FILE = join(SRC_DIR, 'lib', 'api.js');
const API_DIR = join(SRC_DIR, 'lib', 'api');

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
  return files;
}

function isApiSource(filePath) {
  return filePath === API_FILE || filePath.startsWith(`${API_DIR}${sep}`);
}

function sourceLabel(filePath) {
  return relative(SRC_DIR, filePath).split(sep).join('/');
}

describe('RPC ownership guard', () => {
  it('keeps the raw rpc transport inside the API owner', () => {
    const violations = [];
    for (const filePath of collectProductionSources(SRC_DIR)) {
      if (isApiSource(filePath)) continue;
      const source = readFileSync(filePath, 'utf8');
      if (/\brpc\s*\(/.test(source)) {
        violations.push(sourceLabel(filePath));
      }
    }

    expect(violations).toEqual([]);
  });

  it('keeps known server method names inside the API owner', () => {
    const apiSource = [API_FILE, ...collectProductionSources(API_DIR)]
      .map((filePath) => readFileSync(filePath, 'utf8'))
      .join('\n');
    const serverMethods = new Set(
      [...apiSource.matchAll(/\brpc\(\s*['"]([^'"]+)['"]/g)].map(
        (match) => match[1],
      ),
    );
    const violations = [];

    for (const filePath of collectProductionSources(SRC_DIR)) {
      if (
        isApiSource(filePath) ||
        sourceLabel(filePath) === 'lib/i18n.js' ||
        sourceLabel(filePath).startsWith('lib/i18n/')
      ) {
        continue;
      }
      const source = readFileSync(filePath, 'utf8');
      for (const literal of source.matchAll(/['"]([^'"\r\n]+)['"]/g)) {
        // Translation keys may legitimately share an RPC's spelling. They
        // resolve product text; the raw-transport guard above still applies.
        if (/\bt(?:Or)?\(\s*$/.test(source.slice(0, literal.index))) continue;
        if (serverMethods.has(literal[1])) {
          violations.push(`${sourceLabel(filePath)}: ${literal[1]}`);
        }
      }
    }

    expect(violations).toEqual([]);
  });
});
