import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { readStyleSheet } from '../../__tests__/styles.support.js';

// Guard scan for the shared UI primitives. Each design-system control is owned
// by exactly one component under `components/ui/`; every other view must go
// through that component instead of re-applying the global CSS classes by hand.
// This test fails the build if a raw element reintroduces a primitive's class,
// so a bypassed primitive cannot drift back in. Each phase adds its rule below.

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const APP_CSS = readStyleSheet(join(SRC_DIR, 'styles', 'app.css'));
const INDEX_HTML = readFileSync(join(SRC_DIR, '..', 'index.html'), 'utf8');
const SYSTEM_PROMPT_SOURCE = [
  readFileSync(join(SRC_DIR, 'components', 'SystemPromptView.svelte'), 'utf8'),
  readFileSync(join(SRC_DIR, 'components', 'prompt', 'prompt.css'), 'utf8'),
].join('\n');

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
// The bundled Swarm Extension UI imports the same tooltip/InfoHint helpers
// from webui/src, so the native-title guard covers it too.
const SWARM_UI_DIR = join(
  SRC_DIR,
  '..',
  '..',
  'resources',
  'extensions',
  'swarm',
  'ui',
);
const SWARM_UI_SVELTE_FILES = collectSvelteFiles(SWARM_UI_DIR);

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

// Each primitive owns its canonical classes: a raw element anywhere else that
// carries one bypasses the primitive. The scan is keyed by element: `ANY` scans
// every raw element, a tag name narrows it where only that element is reserved.
const PRIMITIVE_CLASSES = [
  {
    owner: 'Button',
    classes: {
      button: [
        // canonical variant + footprint classes
        'btn-primary',
        'btn-secondary',
        'btn-danger',
        'btn-tertiary',
        'btn-icon',
        // retired aliases
        'btn-new',
        'btn-outline',
        'btn-dang',
        'modal-btn-confirm',
        'modal-btn-cancel',
        'send-btn',
        'icon-btn',
        'tl-btn',
        'pane-action',
      ],
    },
  },
  {
    // The shell owns the overlay, header, title, and close button; callers
    // only supply body/footer content (modal-body/modal-footer stay caller-side).
    owner: 'Modal',
    classes: {
      [ANY_ELEMENT]: [
        'modal-overlay',
        'modal-header',
        'modal-title',
        'modal-close',
      ],
    },
  },
  {
    // The two switch sizes; other "toggle"-named controls (stats-toggle,
    // voice-toggle, chat-sessions-toggle) are distinct tokens.
    owner: 'Toggle',
    classes: { button: ['toggle', 'tl-toggle'] },
  },
  {
    // The canonical `chip` base plus the retired color aliases; scoped chips
    // named differently (sp-scope-chip, ...) are distinct.
    owner: 'StatusChip',
    classes: {
      [ANY_ELEMENT]: [
        'chip',
        'chip-green',
        'chip-amber',
        'chip-orange',
        'chip-red',
      ],
    },
  },
  {
    owner: 'Badge',
    classes: {
      [ANY_ELEMENT]: [
        'badge',
        'badge--neutral',
        'badge--info',
        'badge--success',
        'badge--warn',
        'badge--error',
      ],
    },
  },
  {
    owner: 'Banner',
    classes: {
      [ANY_ELEMENT]: [
        'banner',
        'banner--neutral',
        'banner--info',
        'banner--success',
        'banner--warn',
        'banner--error',
      ],
    },
  },
  {
    owner: 'EmptyState',
    classes: {
      [ANY_ELEMENT]: [
        'empty-state',
        'empty-state--default',
        'empty-state--compact',
        'empty-state--fill',
        'empty-state__icon',
        'empty-state__title',
        'empty-state__description',
        'empty-state__actions',
      ],
    },
  },
  {
    owner: 'TabList',
    classes: {
      [ANY_ELEMENT]: [
        'tab-list',
        'tab-list--underline',
        'tab-list--segmented',
        'tab-list--default',
        'tab-list--compact',
        'tab-list__tab',
        'tab-list__tab--active',
      ],
    },
  },
  {
    owner: 'Checkbox',
    classes: { [ANY_ELEMENT]: ['checkbox', 'checkbox__box'] },
  },
  {
    // Editable inputs are scoped to <input>; the read-only value-box may live
    // on any element.
    owner: 'TextField',
    classes: {
      input: ['s-input', 'modal-input'],
      [ANY_ELEMENT]: ['s-value-box'],
    },
  },
  {
    owner: 'TextArea',
    classes: {
      textarea: [
        'text-area',
        'text-area--default',
        'text-area--inset',
        'text-area--code',
        'text-area--invalid',
      ],
    },
  },
  {
    owner: 'FormField',
    classes: {
      [ANY_ELEMENT]: [
        'form-field',
        'form-field--full',
        'form-field__label',
        'form-field__required',
        'form-field__help',
        'form-field__error',
      ],
    },
  },
];

// Bespoke classes folded into the primitives; none may return anywhere.
// Former multi-line field classes are retired on <textarea> only, where
// `s-input` would bypass TextArea (on <input> it belongs to TextField).
const RETIRED_CLASSES = {
  [ANY_ELEMENT]: [
    // feedback, now Banner
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
    // empty states, now EmptyState
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
    // content tabs, now TabList
    'stats-view__tabs',
    'stats-view__tab',
    'stats-view__tab--active',
    'debug-view__detail-tabs',
    'debug-view__tab',
    'debug-view__tab--active',
    'debug-view__body-tabs',
    'debug-view__body-tab',
    'debug-view__body-tab--active',
    // metadata pills, now Badge
    'sp-badge',
    'session-row__badge',
    'stats-badge',
    'stats-skill-badge',
    'stats-origin',
    'logs-view__stream-chip',
    'debug-view__status-chip',
    's-ext-version',
    // form shells, now FormField
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
  ],
  textarea: [
    's-input',
    's-textarea',
    's-textarea--json',
    's-textarea--invalid',
    'sp-textarea',
    'cron-textarea',
    's-skill-manager-editor',
  ],
};

function classViolations(classesByTag, ownerRelativePath) {
  return Object.entries(classesByTag).flatMap(([tagPattern, classes]) =>
    findRawClassViolations(tagPattern, new Set(classes), ownerRelativePath),
  );
}

describe('UI primitive guard', () => {
  it('paints the app background before the Svelte bundle loads', () => {
    expect(INDEX_HTML).toMatch(
      /html,\s*body\s*\{\s*background:\s*#15130f;\s*\}/,
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
    expect(SYSTEM_PROMPT_SOURCE).toContain("!block.enabled && 'sp-block--off'");
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

  it.each(PRIMITIVE_CLASSES)(
    'reserves the $owner classes for its components/ui/ owner',
    ({ owner, classes }) => {
      expect(classViolations(classes, `components/ui/${owner}.svelte`)).toEqual(
        [],
      );
    },
  );

  it('keeps the retired bespoke classes from returning', () => {
    expect(classViolations(RETIRED_CLASSES, '')).toEqual([]);
  });

  it('keeps raw <textarea> to the Chat composer and queued-message editing', () => {
    // Ordinary multi-line fields go through components/ui/TextArea.svelte;
    // these two own specialized resize/send/inline-edit behavior.
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

    expect(rawTextAreas).toEqual([]);
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

  it('bans raw checkboxes — use Toggle.svelte or Checkbox.svelte', () => {
    // A single on/off setting is the shared Toggle (role="switch") button; a
    // selection within a list is the shared Checkbox (role="checkbox")
    // button. A raw `<input type="checkbox">` or a hand-built checkbox role
    // would bypass both primitives, so this scan fails if one reappears.
    const RAW_CHECKBOX = /type\s*=\s*"checkbox"|role\s*=\s*"checkbox"/;
    const violations = [];

    for (const filePath of SVELTE_FILES) {
      const relativePath = relative(SRC_DIR, filePath);
      if (relativePath.split(sep).join('/') === 'components/ui/Checkbox.svelte')
        continue;
      if (RAW_CHECKBOX.test(readFileSync(filePath, 'utf8'))) {
        violations.push(`${relativePath}: raw checkbox`);
      }
    }

    expect(violations).toEqual([]);
  });

  it('bans native title tooltips — use the shared tooltip action or InfoHint', () => {
    // The quick tooltip (`use:tooltip` from lib/tooltip.js, or the Button
    // `tooltip` prop) replaced every native `title` attribute: styled,
    // multi-line, keyboard-reachable, and consistent. A raw `title=` on an element
    // would bring back the unstyled, keyboard- and touch-less browser tooltip.
    // Capitalized Svelte components are unaffected — their `title` props are
    // real headings (Modal, ConfirmDialog), not native tooltips.
    // An iframe's title names its browsing context for assistive technology;
    // it is not a hover-help substitute.
    const NATIVE_TITLE = /<(?!iframe\b)[a-z][\w-]*\b[^>]*\stitle\s*=/g;
    const violations = [];

    expect(SWARM_UI_SVELTE_FILES.length).toBeGreaterThan(0);
    for (const filePath of [...SVELTE_FILES, ...SWARM_UI_SVELTE_FILES]) {
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
});
