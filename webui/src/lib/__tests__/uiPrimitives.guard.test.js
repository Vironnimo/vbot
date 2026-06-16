import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

// Guard scan for the shared UI primitives. Each design-system control is owned
// by exactly one component under `components/ui/`; every other view must go
// through that component instead of re-applying the global CSS classes by hand.
// This test fails the build if a raw element reintroduces a primitive's class,
// so a bypassed primitive cannot drift back in. Each phase adds its rule below.

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function collectSvelteFiles(directory) {
  const files = [];
  for (const entry of readdirSync(directory)) {
    const fullPath = join(directory, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...collectSvelteFiles(fullPath));
    } else if (entry.endsWith('.svelte')) {
      files.push(fullPath);
    }
  }
  return files;
}

const SVELTE_FILES = collectSvelteFiles(SRC_DIR);

function classTokensInTag(openingTag) {
  const tokens = [];

  const staticClass = openingTag.match(/\sclass\s*=\s*"([^"]*)"/);
  if (staticClass) {
    tokens.push(...staticClass[1].split(/\s+/).filter(Boolean));
  }

  const directiveMatches = openingTag.matchAll(/\sclass:([A-Za-z0-9_-]+)/g);
  for (const match of directiveMatches) {
    tokens.push(match[1]);
  }

  return tokens;
}

/**
 * Finds raw `<tagName ...>` elements (the lowercase HTML element, not a Svelte
 * component) whose class attribute or `class:` directive uses one of the
 * forbidden primitive classes, anywhere except the primitive's own component.
 */
function findRawClassViolations(tagName, forbiddenClasses, ownerRelativePath) {
  const openingTagPattern = new RegExp(`<${tagName}\\b[^>]*>`, 'g');
  const violations = [];

  for (const filePath of SVELTE_FILES) {
    const relativePath = relative(SRC_DIR, filePath);
    if (relativePath.split(sep).join('/') === ownerRelativePath) {
      continue;
    }

    const source = readFileSync(filePath, 'utf8');
    for (const tagMatch of source.matchAll(openingTagPattern)) {
      for (const token of classTokensInTag(tagMatch[0])) {
        if (forbiddenClasses.has(token)) {
          violations.push(`${relativePath}: <${tagName} class="…${token}…">`);
        }
      }
    }
  }

  return violations;
}

describe('UI primitive guard', () => {
  it('routes every button through components/ui/Button.svelte', () => {
    const forbidden = new Set([
      // canonical variant + footprint classes the Button component owns
      'btn-primary',
      'btn-secondary',
      'btn-danger',
      'btn-tertiary',
      'btn-icon',
      // retired aliases — reintroducing any of these is also a regression
      'btn-new',
      'btn-outline',
      'btn-dang',
      'modal-btn-confirm',
      'modal-btn-cancel',
      'send-btn',
      'icon-btn',
      'tl-btn',
      'pane-action',
    ]);

    const violations = findRawClassViolations(
      'button',
      forbidden,
      'components/ui/Button.svelte',
    );

    expect(violations).toEqual([]);
  });
});
