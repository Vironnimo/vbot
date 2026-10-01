import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { parse as parseSvelte } from 'svelte/compiler';
import { parseAst } from 'vite';
import { describe, expect, it } from 'vitest';

import { englishCatalog } from '../i18n.js';

// Guard scan over every WebUI and Extension page source. The English catalogs
// are the only source of UI text: every key a call names has non-empty text,
// no call passes an English fallback, every catalog entry is used, and each
// entry's `{name}` placeholders are supplied by the call's params object.
//
// Calls are `t(key, params)` and `tOr(key, fallback, params)`. A key is a string literal, or a template
// literal documented in COMPOSED_KEYS below.

const REPO_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
  '..',
);

// Composed keys: template-literal keys completed at runtime, where `*` stands
// for one key segment.
// - A `t` key takes its values from a fixed WebUI list, repeated here. Each
//   value has a catalog entry, and only those entries count as used.
// - A `tOr` key is completed with a server-sent code and renders its fallback
//   when the code has no entry. Every entry the pattern matches counts as used.
const COMPOSED_KEYS = {
  // agentForm.js AGENT_MEMORY_PROMPT_MODES
  'agents.form.memoryPromptModeOption.*': ['off', 'agent', 'agent_user'],
  // agentForm.js THINKING_EFFORT_OPTIONS without the inherit option ''
  'agents.form.thinkingEffortOption.*': [
    'none',
    'minimal',
    'low',
    'medium',
    'high',
    'xhigh',
    'max',
  ],
  // calendarView.js CALENDAR_VIEWS
  'calendar.view.*': ['month', 'week', 'day', 'agenda'],
  // sessions/presentation.js REFLECTION_BADGE_RUN_KINDS
  'sessions.runKind.*': ['memory_reflection', 'skill_reflection', 'reflection'],
  // settingsView/appearance.js CHAT_WIDTH_OPTIONS
  'settings.appearance.chatWidth.*': ['comfortable', 'wide', 'full'],
  // settingsView/appearance.js CHAT_WORKING_MODE_OPTIONS
  'settings.appearance.chatWorkingMode.*': ['normal', 'compact'],
  // LocalSpeechSupport.svelte: setup states without their own message
  'settings.localSpeech.state.*': [
    'checking',
    'missing',
    'restart_required',
    'restarting',
  ],
  // statisticsView.js MODEL_CALL_KINDS
  'statistics.kind.*': [
    'chat',
    'compaction',
    'speech_to_text',
    'text_to_speech',
    'image_understanding',
    'image_generation',
    'video_generation',
    'music_generation',
    'text_embedding',
    'decision',
    'live_voice',
    'live_voice_backend',
    'session_title',
    'group_title',
    'extension_sampling',
  ],
  // statisticsView.js DAILY_GRANULARITIES
  'statistics.overview.activityWindow.*': ['day', 'week', 'month'],
  // statisticsView.js STATISTICS_RANGES
  'statistics.range.*': ['7d', '30d', '90d', 'all'],
  'statistics.range.short.*': ['7d', '30d', '90d', 'all'],
  // statisticsView.js MODEL_CALL_STATUSES
  'statistics.requestStatus.*': [
    'started',
    'completed',
    'failed',
    'cancelled',
    'interrupted',
  ],
  // statisticsView.js parseOrigin(): origins that carry a detail
  'statistics.skills.scopedOrigin.*': ['agent', 'project'],
  // terminalsView/protocol.js parseTerminalCommandLine() errors
  'terminals.commandError.*': ['unclosedQuote', 'emptyArgument'],
  'calendar.actions.status.*': 'tOr', // Calendar action status
  'chat.voice.progress.*': 'tOr', // speech job phase
  'logs.level.*': 'tOr', // log record level
  'projects.report.group.*': 'tOr', // Project scan finding type
  'settings.language.*': 'tOr', // server language id
  'settings.localSpeech.choices.*': 'tOr', // Local Speech option value
  'settings.localSpeech.error.*': 'tOr', // Local Speech setup error
  'settings.localSpeech.options.*.help': 'tOr', // Local Speech option name
  'settings.localSpeech.options.*.label': 'tOr', // Local Speech option name
  'settings.localSpeech.phase.*': 'tOr', // Local Speech setup phase
  'settings.recall.backends.*': 'tOr', // Recall backend id
  'settings.recall.indexError.*': 'tOr', // Recall index failure code
  'settings.webSearch.providers.*': 'tOr', // web search or fetch provider
  'statistics.skills.origin.*': 'tOr', // Skill origin scope
  'statistics.status.*': 'tOr', // Run status
  'systemPrompt.blockTitle.*': 'tOr', // System Prompt block id
};

// One source collector for every check: production `.js` and `.svelte` files
// under `webui/src` (scope `core`) and each `resources/extensions/<name>/ui`
// (scope `<name>`), tests excluded.
function collectSources() {
  const roots = [{ dir: join(REPO_DIR, 'webui', 'src'), scope: 'core' }];
  const extensionsDir = join(REPO_DIR, 'resources', 'extensions');
  for (const name of readdirSync(extensionsDir)) {
    const dir = join(extensionsDir, name, 'ui');
    if (existsSync(dir)) roots.push({ dir, scope: name });
  }
  const sources = [];
  const visit = (dir, scope) => {
    for (const entry of readdirSync(dir)) {
      const fullPath = join(dir, entry);
      if (entry === '__tests__' || entry === 'node_modules') continue;
      if (statSync(fullPath).isDirectory()) visit(fullPath, scope);
      else if (/\.(js|svelte)$/.test(entry) && !entry.endsWith('.test.js')) {
        sources.push({
          file: relative(REPO_DIR, fullPath).split(sep).join('/'),
          scope,
          source: readFileSync(fullPath, 'utf8'),
        });
      }
    }
  };
  for (const { dir, scope } of roots) visit(dir, scope);
  return { roots, sources };
}

function walk(node, onCall) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) walk(child, onCall);
    return;
  }
  if (
    node.type === 'CallExpression' &&
    node.callee.type === 'Identifier' &&
    ['t', 'tOr'].includes(node.callee.name)
  ) {
    onCall(node);
  }
  for (const [key, value] of Object.entries(node)) {
    if (key !== 'parent' && key !== 'metadata' && value) walk(value, onCall);
  }
}

function describeKey(node) {
  if (node?.type === 'Literal' && typeof node.value === 'string') {
    return { literal: node.value };
  }
  if (node?.type === 'TemplateLiteral') {
    const parts = node.quasis.map((quasi) => quasi.value.cooked);
    return parts.length === 1
      ? { literal: parts[0] }
      : { pattern: parts.join('*') };
  }
  return {};
}

function isFallbackValue(node) {
  return (
    (node.type === 'Literal' &&
      (typeof node.value === 'string' || node.value === null)) ||
    node.type === 'TemplateLiteral' ||
    node.type === 'BinaryExpression' ||
    (node.type === 'Identifier' && node.name === 'undefined')
  );
}

// The property names of a literal params object; null for any other params.
function paramNames(node) {
  if (!node) return [];
  if (node.type !== 'ObjectExpression') return null;
  const names = [];
  for (const property of node.properties) {
    if (property.type !== 'Property' || property.computed) return null;
    names.push(property.key.name ?? String(property.key.value));
  }
  return names;
}

// Every t/tOr call with its location, scope, key and params.
function scanCalls(sources) {
  const calls = [];
  for (const { file, scope, source } of sources) {
    const ast = file.endsWith('.svelte')
      ? parseSvelte(source, { modern: true })
      : parseAst(source);
    walk(ast, (node) => {
      const [keyNode, ...rest] = node.arguments;
      const callee = node.callee.name;
      const params = callee === 'tOr' ? rest[1] : rest[0];
      const extra = rest.slice(callee === 'tOr' ? 2 : 1);
      const passesFallback =
        callee !== 'tOr' &&
        (extra.length > 0 || Boolean(params && isFallbackValue(params)));
      calls.push({
        where: `${file}:${source.slice(0, node.start).split('\n').length}`,
        scope,
        callee,
        ...describeKey(keyNode),
        passesFallback,
        params: passesFallback ? [] : paramNames(params),
      });
    });
  }
  return calls;
}

function patternRegExp(pattern) {
  const escaped = pattern
    .split('*')
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&'))
    .join('[^.]+');
  return new RegExp(`^${escaped}$`);
}

function placeholders(text) {
  return [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]);
}

// The catalog keys one call can render.
function reachableKeys(call, catalog) {
  if (call.literal !== undefined) return [call.literal];
  const composed = COMPOSED_KEYS[call.pattern];
  if (Array.isArray(composed)) {
    return composed.map((value) => call.pattern.replace('*', value));
  }
  const pattern = patternRegExp(call.pattern);
  return Object.keys(catalog).filter((key) => pattern.test(key));
}

// Checks the calls of one scan against the catalogs of each scope.
function catalogViolations({ catalogs, calls }) {
  const violations = {
    keys: [],
    missing: [],
    fallbacks: [],
    unused: [],
    placeholders: [],
  };
  const available = (scope) => ({ ...catalogs.core, ...catalogs[scope] });
  const used = new Map(
    Object.keys(catalogs).map((scope) => [scope, new Set()]),
  );
  const usedPatterns = new Map();

  for (const call of calls) {
    const catalog = available(call.scope);
    if (call.literal === undefined && call.pattern === undefined) {
      violations.keys.push(`${call.where}: key is not a literal`);
      continue;
    }
    if (call.pattern !== undefined) {
      const composed = COMPOSED_KEYS[call.pattern];
      if (composed === undefined) {
        violations.keys.push(`${call.where}: undocumented ${call.pattern}`);
        continue;
      }
      const documentedCallee = Array.isArray(composed) ? 't' : 'tOr';
      if ((call.callee === 'tOr') !== (documentedCallee === 'tOr')) {
        violations.keys.push(
          `${call.where}: ${call.pattern} is documented for ${documentedCallee}()`,
        );
      }
      usedPatterns.set(call.pattern, call.scope);
    } else if (call.callee === 'tOr') {
      violations.fallbacks.push(
        `${call.where}: tOr() with literal key ${call.literal}`,
      );
    }
    if (call.passesFallback) {
      violations.fallbacks.push(
        `${call.where}: ${call.callee}() with a fallback for ${call.literal ?? call.pattern}`,
      );
    }

    for (const key of reachableKeys(call, catalog)) {
      used.get(call.scope)?.add(key);
      used.get('core').add(key);
      if (!Object.hasOwn(catalog, key) || !catalog[key]) {
        violations.missing.push(`${call.where}: ${key}`);
        continue;
      }
      const missing =
        call.params === null
          ? ['params as an object literal']
          : placeholders(catalog[key]).filter(
              (name) => !call.params.includes(name),
            );
      if (missing.length > 0) {
        violations.placeholders.push(
          `${call.where}: ${key} needs ${missing.join(', ')}`,
        );
      }
    }
  }

  for (const [pattern, composed] of Object.entries(COMPOSED_KEYS)) {
    if (!usedPatterns.has(pattern)) {
      violations.keys.push(`COMPOSED_KEYS documents unused ${pattern}`);
    } else if (
      composed === 'tOr' &&
      reachableKeys({ pattern }, available(usedPatterns.get(pattern)))
        .length === 0
    ) {
      violations.keys.push(`COMPOSED_KEYS ${pattern} matches no entry`);
    }
  }
  for (const [scope, catalog] of Object.entries(catalogs)) {
    for (const key of Object.keys(catalog)) {
      if (!used.get(scope).has(key)) {
        violations.unused.push(scope === 'core' ? key : `${scope}: ${key}`);
      }
    }
  }
  return violations;
}

async function loadCatalogs(roots) {
  const catalogs = { core: englishCatalog };
  for (const { dir, scope } of roots.slice(1)) {
    const file = join(dir, 'i18n.js');
    if (!existsSync(file)) continue;
    const { default: entries } = await import(pathToFileURL(file).href);
    catalogs[scope] = entries;
  }
  return catalogs;
}

const { roots, sources } = collectSources();
const catalogs = await loadCatalogs(roots);
const violations = catalogViolations({ catalogs, calls: scanCalls(sources) });

describe('i18n catalog guard', () => {
  it('keeps Extension catalog keys apart from the core catalog', () => {
    const shared = Object.entries(catalogs)
      .filter(([scope]) => scope !== 'core')
      .flatMap(([scope, entries]) =>
        Object.keys(entries)
          .filter((key) => Object.hasOwn(englishCatalog, key))
          .map((key) => `${scope}: ${key}`),
      );
    expect(shared).toEqual([]);
  });

  it('names every key literally or through a documented composed key', () => {
    expect(violations.keys).toEqual([]);
  });

  it('resolves every key in the catalogs of its page', () => {
    expect(violations.missing).toEqual([]);
  });

  it('passes no English fallback to t()', () => {
    expect(violations.fallbacks).toEqual([]);
  });

  it('uses every catalog entry', () => {
    expect(violations.unused).toEqual([]);
  });

  it("supplies every placeholder of a key's entry in the call's params", () => {
    expect(violations.placeholders).toEqual([]);
  });
});
