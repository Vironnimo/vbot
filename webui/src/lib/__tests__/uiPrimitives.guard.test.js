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
 * Finds raw HTML elements (lowercase tags — never a capitalized Svelte
 * component) whose class attribute or `class:` directive uses one of the
 * forbidden primitive classes, anywhere except the primitive's own component.
 * `tagPattern` is a regex fragment: a literal tag name (e.g. `button`) or a
 * wildcard (`[a-z][\\w-]*`) to scan every element.
 */
function findRawClassViolations(
  tagPattern,
  forbiddenClasses,
  ownerRelativePath,
) {
  const openingTagPattern = new RegExp(`<(?:${tagPattern})\\b[^>]*>`, 'g');
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
          violations.push(
            `${relativePath}: <${tagMatch[0].slice(1).match(/^[\w-]+/)?.[0]} class="…${token}…">`,
          );
        }
      }
    }
  }

  return violations;
}

const ANY_ELEMENT = '[a-z][\\w-]*';

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

  it('routes every modal through components/ui/Modal.svelte', () => {
    // The shell owns the overlay, header, title, and close button; callers only
    // supply body/footer content (modal-body/modal-footer stay caller-side).
    const forbidden = new Set([
      'modal-overlay',
      'modal-header',
      'modal-title',
      'modal-close',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/Modal.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('routes every switch toggle through components/ui/Toggle.svelte', () => {
    // The two switch sizes; other "toggle"-named controls (stats-toggle,
    // voice-toggle, chat-sessions-toggle) are distinct tokens and unaffected.
    const forbidden = new Set(['toggle', 'tl-toggle']);

    const violations = findRawClassViolations(
      'button',
      forbidden,
      'components/ui/Toggle.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('routes every status chip through components/ui/StatusChip.svelte', () => {
    // The canonical `chip` base plus the retired color aliases; scoped chips
    // named differently (sp-scope-chip, …) are distinct.
    const forbidden = new Set([
      'chip',
      'chip-green',
      'chip-amber',
      'chip-orange',
      'chip-red',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/StatusChip.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('routes every metadata badge through components/ui/Badge.svelte', () => {
    // The canonical `badge` base plus each `badge--<variant>` modifier. A raw
    // element carrying any of these bypasses the Badge primitive that owns the
    // metadata-tag pill (the counterpart to StatusChip's status `chip`).
    const forbidden = new Set([
      'badge',
      'badge--neutral',
      'badge--info',
      'badge--success',
      'badge--warn',
      'badge--error',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/Badge.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('keeps the retired bespoke pill classes from returning', () => {
    // Every hand-built metadata pill was folded into the Badge primitive. These
    // base tokens must never reappear on an element — reintroducing one is a
    // regression back to a one-off pill instead of the shared Badge.
    const retired = new Set([
      'sp-badge',
      'session-row__badge',
      'stats-badge',
      'stats-skill-badge',
      'stats-origin',
      'logs-view__stream-chip',
      'debug-view__status-chip',
      's-ext-version',
    ]);

    const violations = findRawClassViolations(ANY_ELEMENT, retired, '');

    expect(violations).toEqual([]);
  });

  it('bans native confirm() in components — use ConfirmDialog instead', () => {
    // The shared ConfirmDialog replaces every native browser confirm. This scan
    // fails the build if `window.confirm(`, `globalThis.confirm(`, or a bare
    // `confirm(` call reappears in a component. The lookbehind requires the
    // token to start on a non-word boundary and be immediately followed by `(`,
    // so identifiers that merely contain the word — `confirmDelete`,
    // `onConfirm`, `ConfirmDialog`, and i18n keys like `delete_confirm` or
    // `deleteConfirm` — never trip it.
    const NATIVE_CONFIRM_CALL = /(?<![A-Za-z0-9_])confirm\s*\(/g;
    const violations = [];

    for (const filePath of SVELTE_FILES) {
      const source = readFileSync(filePath, 'utf8');
      for (const match of source.matchAll(NATIVE_CONFIRM_CALL)) {
        const relativePath = relative(SRC_DIR, filePath);
        violations.push(`${relativePath}: ${match[0]}`);
      }
    }

    expect(violations).toEqual([]);
  });

  it('bans raw checkbox inputs — boolean toggles use Toggle.svelte', () => {
    // Every boolean on/off control is the shared Toggle (role="switch") button.
    // A raw `<input type="checkbox">` would bypass the primitive, so this scan
    // fails the build if one reappears.
    const RAW_CHECKBOX = /type\s*=\s*"checkbox"/;
    const violations = [];

    for (const filePath of SVELTE_FILES) {
      const relativePath = relative(SRC_DIR, filePath);
      if (RAW_CHECKBOX.test(readFileSync(filePath, 'utf8'))) {
        violations.push(`${relativePath}: <input type="checkbox">`);
      }
    }

    expect(violations).toEqual([]);
  });

  it('bans native title tooltips — use the shared tooltip action or InfoHint', () => {
    // The quick tooltip (`use:tooltip` from lib/tooltip.js, or the Button
    // `tooltip` prop) replaced every native `title` attribute: styled,
    // multi-line, immediate, and consistent. A raw `title=` on an HTML element
    // would bring back the unstyled, delayed, touch-less browser tooltip.
    // Capitalized Svelte components are unaffected — their `title` props are
    // real headings (Modal, ConfirmDialog), not native tooltips.
    const NATIVE_TITLE = /<[a-z][\w-]*\b[^>]*\stitle\s*=/g;
    const violations = [];

    for (const filePath of SVELTE_FILES) {
      const source = readFileSync(filePath, 'utf8');
      for (const match of source.matchAll(NATIVE_TITLE)) {
        const relativePath = relative(SRC_DIR, filePath);
        violations.push(
          `${relativePath}: ${match[0].replaceAll('\n', ' ').slice(0, 80)}`,
        );
      }
    }

    expect(violations).toEqual([]);
  });

  it('routes every text field through components/ui/TextField.svelte', () => {
    // Editable inputs: scoped to <input> so textareas (which legitimately reuse
    // s-input for styling) are unaffected. The read-only value-box may live on
    // any element, so it is scanned everywhere.
    const inputViolations = findRawClassViolations(
      'input',
      new Set(['s-input', 'modal-input']),
      'components/ui/TextField.svelte',
    );
    const valueBoxViolations = findRawClassViolations(
      ANY_ELEMENT,
      new Set(['s-value-box']),
      'components/ui/TextField.svelte',
    );

    expect([...inputViolations, ...valueBoxViolations]).toEqual([]);
  });
});
