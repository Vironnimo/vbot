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
const APP_CSS = readFileSync(join(SRC_DIR, 'styles', 'app.css'), 'utf8');
const INDEX_HTML = readFileSync(join(SRC_DIR, '..', 'index.html'), 'utf8');
const SYSTEM_PROMPT_SOURCE = readFileSync(
  join(SRC_DIR, 'components', 'SystemPromptView.svelte'),
  'utf8',
);

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
  it('paints the app background before the Svelte bundle loads', () => {
    expect(INDEX_HTML).toMatch(
      /html,\s*body\s*\{\s*background:\s*#221a12;\s*\}/,
    );
  });

  it('keeps secondary-list content equally inset on every edge', () => {
    expect(APP_CSS).toMatch(/\.secondary-list\s*\{\s*padding:\s*12px;/);
  });

  it('keeps form, Chat, Prompt block, and preview surfaces independently themed', () => {
    expect(APP_CSS).toMatch(/--field-surface:\s*var\(--surface-2\);/);
    expect(APP_CSS).toMatch(/--composer-surface:\s*var\(--surface\);/);
    expect(APP_CSS).toMatch(/--prompt-header-surface:\s*var\(--surface-2\);/);
    expect(APP_CSS).toMatch(/--prompt-content-surface:\s*var\(--surface\);/);
    expect(APP_CSS).toMatch(/--preview-surface:\s*var\(--surface\);/);
    expect(APP_CSS).toMatch(
      /\.text-area--inset\s*\{[^}]*background:\s*var\(--prompt-content-surface\);/s,
    );
    expect(APP_CSS).toMatch(
      /\.input-wrap\s*\{[^}]*background:\s*var\(--composer-surface\);/s,
    );
    expect(SYSTEM_PROMPT_SOURCE).toMatch(
      /\.sp-block\s*\{[^}]*background:\s*var\(--prompt-content-surface\);/s,
    );
    expect(SYSTEM_PROMPT_SOURCE).toMatch(
      /\.sp-block-row\s*\{[^}]*background:\s*var\(--prompt-header-surface\);/s,
    );
    expect(SYSTEM_PROMPT_SOURCE).not.toMatch(/\.sp-block--data \.sp-block-row/);
    expect(SYSTEM_PROMPT_SOURCE).toContain(
      'class:sp-block--off={!block.enabled}',
    );
    // Disabled and inherited state must not dim readable instructions.
    expect(SYSTEM_PROMPT_SOURCE).not.toMatch(
      /\.sp-block--(?:off|inherited)[^{]*\{[^}]*opacity:/s,
    );
    expect(SYSTEM_PROMPT_SOURCE).toMatch(
      /\.sp-document\s*\{[^}]*background:\s*var\(--preview-surface\);/s,
    );
    expect(SYSTEM_PROMPT_SOURCE).toMatch(
      /\.sp-preview-pre\s*\{[^}]*color:\s*var\(--text-hi\);/s,
    );
  });

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

  it('routes every inline banner through components/ui/Banner.svelte', () => {
    const forbidden = new Set([
      'banner',
      'banner--neutral',
      'banner--info',
      'banner--success',
      'banner--warn',
      'banner--error',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/Banner.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('keeps the retired bespoke banner classes from returning', () => {
    const retired = new Set([
      's-feedback',
      's-feedback--neutral',
      's-feedback--error',
      's-feedback--compact',
      'agents-view__notice',
      'agents-view__notice--error',
      'projects-notice',
      'projects-notice--error',
      'projects-notice--warn',
      'cron-notice',
      'cron-notice--error',
      'logs-view__feedback',
      'logs-view__feedback--error',
      'logs-view__feedback--warn',
      'debug-view__feedback',
      'debug-view__feedback--error',
      'stats-view__feedback',
      'stats-view__feedback--error',
      'sp-feedback',
      'sp-feedback--neutral',
      'onboarding-notice',
      'onboarding-error',
      'model-fallback-notice',
      'interrupted-notice',
      'chat-view__subagent-session-notice',
      'chat-view__no-model-notice',
    ]);

    const violations = findRawClassViolations(ANY_ELEMENT, retired, '');

    expect(violations).toEqual([]);
  });

  it('routes every empty-content surface through components/ui/EmptyState.svelte', () => {
    const forbidden = new Set([
      'empty-state',
      'empty-state--default',
      'empty-state--compact',
      'empty-state--fill',
      'empty-state__icon',
      'empty-state__title',
      'empty-state__description',
      'empty-state__actions',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/EmptyState.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('keeps the retired bespoke empty-state classes from returning', () => {
    const retired = new Set([
      'empty-state-icon',
      'empty-state-title',
      'empty-state-sub',
      'agents-view__empty-list',
      'project-empty-list',
      'project-empty-title',
      'project-empty-sub',
      'project-detail-empty',
      'projects-file-empty',
      'projects-team-empty',
      'cron-empty-list',
      'cron-empty-title',
      'cron-empty-sub',
      'cron-detail-empty',
      'session-drawer__empty',
      'session-drawer__empty-title',
      'session-drawer__empty-subtitle',
      'logs-view__state',
      'logs-view__state-title',
      'logs-view__state-subtitle',
      'debug-view__state',
      'debug-view__state-title',
      'debug-view__state-subtitle',
      'stats-empty',
    ]);

    const violations = findRawClassViolations(ANY_ELEMENT, retired, '');

    expect(violations).toEqual([]);
  });

  it('routes every content tab list through components/ui/TabList.svelte', () => {
    const forbidden = new Set([
      'tab-list',
      'tab-list--underline',
      'tab-list--segmented',
      'tab-list--default',
      'tab-list--compact',
      'tab-list__tab',
      'tab-list__tab--active',
    ]);

    const violations = findRawClassViolations(
      ANY_ELEMENT,
      forbidden,
      'components/ui/TabList.svelte',
    );

    expect(violations).toEqual([]);
  });

  it('keeps the retired bespoke content-tab classes from returning', () => {
    const retired = new Set([
      'stats-view__tabs',
      'stats-view__tab',
      'stats-view__tab--active',
      'debug-view__detail-tabs',
      'debug-view__tab',
      'debug-view__tab--active',
      'debug-view__body-tabs',
      'debug-view__body-tab',
      'debug-view__body-tab--active',
    ]);

    const violations = findRawClassViolations(ANY_ELEMENT, retired, '');

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
    // Editable inputs are scoped to <input>; multi-line fields have their own
    // TextArea primitive below. The read-only value-box may live on any element,
    // so it is scanned everywhere.
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

  it('routes every ordinary multi-line field through components/ui/TextArea.svelte', () => {
    const rawTextAreaAllowlist = new Set([
      'components/ChatComposer.svelte',
      'components/QueuedMessages.svelte',
      'components/ui/TextArea.svelte',
    ]);
    const rawTextAreas = [];

    for (const filePath of SVELTE_FILES) {
      const relativePath = relative(SRC_DIR, filePath).split(sep).join('/');
      if (
        !rawTextAreaAllowlist.has(relativePath) &&
        /<textarea\b/.test(readFileSync(filePath, 'utf8'))
      ) {
        rawTextAreas.push(`${relativePath}: <textarea>`);
      }
    }

    const primitiveViolations = findRawClassViolations(
      'textarea',
      new Set([
        'text-area',
        'text-area--default',
        'text-area--inset',
        'text-area--code',
        'text-area--invalid',
      ]),
      'components/ui/TextArea.svelte',
    );

    expect([...rawTextAreas, ...primitiveViolations]).toEqual([]);
  });

  it('routes every ordinary form field shell through components/ui/FormField.svelte', () => {
    const canonical = new Set([
      'form-field',
      'form-field--full',
      'form-field__label',
      'form-field__required',
      'form-field__help',
      'form-field__error',
    ]);
    const retired = new Set([
      'modal-field',
      'modal-label',
      's-field',
      's-field--full',
      's-field-label',
      's-field-hint',
      's-field-help',
      's-field-error',
      'agents-view__field-help',
      'agents-view__field-error',
    ]);

    const canonicalViolations = findRawClassViolations(
      ANY_ELEMENT,
      canonical,
      'components/ui/FormField.svelte',
    );
    const retiredViolations = findRawClassViolations(ANY_ELEMENT, retired, '');

    expect([...canonicalViolations, ...retiredViolations]).toEqual([]);
  });

  it('keeps the retired bespoke multi-line field classes from returning', () => {
    const retired = new Set([
      's-input',
      's-textarea',
      's-textarea--json',
      's-textarea--invalid',
      'sp-textarea',
      'cron-textarea',
      's-skill-manager-editor',
    ]);

    const violations = findRawClassViolations('textarea', retired, '');

    expect(violations).toEqual([]);
  });
});
