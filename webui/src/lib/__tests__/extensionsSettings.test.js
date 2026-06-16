import { describe, expect, it } from 'vitest';

import {
  applyExtensionsPanelList,
  buildExtensionsUpdatePayload,
  extensionStatusChipVariant,
  formatExtensionConfig,
  parseExtensionConfigDraft,
  summarizeExtensionCapabilities,
} from '../settingsView.js';

const translate = (_key, fallback) => fallback;

function rawExtensions() {
  return {
    extensions: [
      {
        name: 'guard_bash',
        status: 'loaded',
        disabled: false,
        version: '1.2.0',
        description: 'Guards dangerous bash',
        error: null,
        config: { deny: ['rm -rf'] },
        capability_errors: ['tool x skipped'],
        capabilities: {
          hooks: { tool_call: 1, run_end: 2 },
          tools: ['word_count'],
          recall_backends: [],
          startup: true,
          shutdown: false,
        },
      },
      {
        name: 'legacy',
        status: 'disabled',
        disabled: true,
        version: null,
        description: null,
        error: null,
        config: {},
        capability_errors: [],
        capabilities: {},
      },
      { name: '', status: 'loaded' },
      'not-an-object',
    ],
  };
}

describe('applyExtensionsPanelList', () => {
  it('normalizes records and drops invalid entries', () => {
    const result = applyExtensionsPanelList(rawExtensions());

    expect(result.map((extension) => extension.name)).toEqual([
      'guard_bash',
      'legacy',
    ]);
    expect(result[0]).toMatchObject({
      name: 'guard_bash',
      status: 'loaded',
      disabled: false,
      version: '1.2.0',
      description: 'Guards dangerous bash',
      config: { deny: ['rm -rf'] },
      capabilityErrors: ['tool x skipped'],
    });
    expect(result[0].capabilities.hooks).toEqual([
      { event: 'tool_call', count: 1 },
      { event: 'run_end', count: 2 },
    ]);
    expect(result[0].capabilities.tools).toEqual(['word_count']);
    expect(result[0].capabilities.startup).toBe(true);
    expect(result[1].disabled).toBe(true);
  });

  it('returns an empty list for a malformed result', () => {
    expect(applyExtensionsPanelList(null)).toEqual([]);
    expect(applyExtensionsPanelList({})).toEqual([]);
  });
});

describe('extensionStatusChipVariant', () => {
  it('maps status to a status-chip variant', () => {
    expect(extensionStatusChipVariant('loaded')).toBe('success');
    expect(extensionStatusChipVariant('failed')).toBe('error');
    expect(extensionStatusChipVariant('disabled')).toBe('warn');
  });
});

describe('summarizeExtensionCapabilities', () => {
  it('summarizes hooks, tools, and lifecycle', () => {
    const [extension] = applyExtensionsPanelList(rawExtensions());

    expect(
      summarizeExtensionCapabilities(extension.capabilities, translate),
    ).toBe('Hooks: tool_call(1), run_end(2) · Tools: word_count · startup');
  });

  it('returns an empty string when nothing is contributed', () => {
    expect(summarizeExtensionCapabilities({}, translate)).toBe('');
  });
});

describe('formatExtensionConfig', () => {
  it('pretty-prints a non-empty config and empties an empty one', () => {
    expect(formatExtensionConfig({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(formatExtensionConfig({})).toBe('');
    expect(formatExtensionConfig(null)).toBe('');
  });
});

describe('parseExtensionConfigDraft', () => {
  it('accepts an empty draft as an empty object', () => {
    expect(parseExtensionConfigDraft('   ')).toEqual({ ok: true, value: {} });
  });

  it('parses a JSON object', () => {
    expect(parseExtensionConfigDraft('{"a": 1}')).toEqual({
      ok: true,
      value: { a: 1 },
    });
  });

  it('rejects invalid JSON and non-object JSON', () => {
    expect(parseExtensionConfigDraft('{bad}').ok).toBe(false);
    expect(parseExtensionConfigDraft('[1, 2]').ok).toBe(false);
    expect(parseExtensionConfigDraft('42').ok).toBe(false);
  });
});

describe('buildExtensionsUpdatePayload', () => {
  it('reconstructs the full section from the current list', () => {
    const extensions = applyExtensionsPanelList(rawExtensions());

    expect(buildExtensionsUpdatePayload(extensions)).toEqual({
      extensions: {
        disabled: ['legacy'],
        config: { guard_bash: { deny: ['rm -rf'] } },
      },
    });
  });

  it('applies a disable override for one extension', () => {
    const extensions = applyExtensionsPanelList(rawExtensions());

    expect(
      buildExtensionsUpdatePayload(extensions, {
        name: 'guard_bash',
        disabled: true,
      }),
    ).toEqual({
      extensions: {
        disabled: ['guard_bash', 'legacy'],
        config: { guard_bash: { deny: ['rm -rf'] } },
      },
    });
  });

  it('applies a config override and drops emptied config', () => {
    const extensions = applyExtensionsPanelList(rawExtensions());

    expect(
      buildExtensionsUpdatePayload(extensions, {
        name: 'guard_bash',
        config: {},
      }),
    ).toEqual({
      extensions: { disabled: ['legacy'], config: {} },
    });
  });
});
