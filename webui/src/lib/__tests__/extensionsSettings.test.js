import { describe, expect, it } from 'vitest';

import {
  applyExtensionsPanelList,
  buildExtensionsUpdatePayload,
  buildSchemaConfigFromForm,
  buildSchemaFormState,
  describeExtensionWaiting,
  extensionStatusChipVariant,
  formatExtensionConfig,
  hasSettingsSchema,
  normalizeSchemaFields,
  parseExtensionConfigDraft,
  summarizeExtensionCapabilities,
} from '../settingsView.js';

import { englishCatalog } from '../i18n.js';

// Mirrors the real ``t()``: uses the fallback text and interpolates ``{name}``
// placeholders from the values map, so a key like ``waitingFor`` resolves fully.
const translate = (_key, fallback, values) =>
  values
    ? fallback.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) =>
        Object.prototype.hasOwnProperty.call(values, name)
          ? String(values[name])
          : match,
      )
    : fallback;

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
          tools: [{ name: 'word_count', ready: true }],
          commands: [{ name: 'workflow', registered: true }],
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
      {
        name: 'homeassistant',
        status: 'overridden',
        disabled: false,
        version: null,
        description: null,
        error: null,
        overridden_by: '/data/extensions/homeassistant/__init__.py',
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
      'homeassistant',
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
    expect(result[0].capabilities.tools).toEqual([
      { name: 'word_count', ready: true },
    ]);
    expect(result[0].capabilities.commands).toEqual([
      { name: 'workflow', registered: true },
    ]);
    expect(result[0].readyState).toBe('ready');
    expect(result[0].capabilities.startup).toBe(true);
    expect(result[1].disabled).toBe(true);
    expect(result[2]).toMatchObject({
      name: 'homeassistant',
      status: 'overridden',
      disabled: false,
      overriddenBy: '/data/extensions/homeassistant/__init__.py',
    });
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
    expect(extensionStatusChipVariant('overridden')).toBe('warn');
  });
});

describe('summarizeExtensionCapabilities', () => {
  it('summarizes hooks, tools, and lifecycle', () => {
    const [extension] = applyExtensionsPanelList(rawExtensions());

    expect(
      summarizeExtensionCapabilities(extension.capabilities, translate),
    ).toBe(
      'Hooks: tool_call(1), run_end(2) · Tools: word_count · Commands: /workflow · startup',
    );
  });

  it('returns an empty string when nothing is contributed', () => {
    expect(summarizeExtensionCapabilities({}, translate)).toBe('');
  });
});

describe('describeExtensionWaiting', () => {
  it('returns null for a ready extension', () => {
    const [extension] = applyExtensionsPanelList({
      extensions: [{ name: 'ha', status: 'loaded', ready_state: 'ready' }],
    });

    expect(describeExtensionWaiting(extension, translate)).toBeNull();
  });

  it('names unset secret fields by label when waiting', () => {
    const [extension] = applyExtensionsPanelList({
      extensions: [
        {
          name: 'homeassistant',
          status: 'loaded',
          ready_state: 'waiting',
          settings_schema: [
            {
              key: 'token',
              type: 'secret',
              label: 'Token',
              env_key: 'HASS_TOKEN',
              set: false,
            },
            {
              key: 'other',
              type: 'secret',
              label: 'Other',
              env_key: 'OTHER',
              set: true,
            },
            { key: 'url', type: 'text', label: 'URL' },
          ],
        },
      ],
    });

    expect(describeExtensionWaiting(extension, translate)).toEqual({
      hint: 'On, waiting for configuration',
      waitingFor: 'Waiting for: Token',
    });
  });

  it('omits the waitingFor line when no unset secret is known', () => {
    const [extension] = applyExtensionsPanelList({
      extensions: [{ name: 'plain', status: 'loaded', ready_state: 'waiting' }],
    });

    expect(describeExtensionWaiting(extension, translate)).toEqual({
      hint: 'On, waiting for configuration',
      waitingFor: null,
    });
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

function schema() {
  return [
    { key: 'url', type: 'text', label: 'URL', default: 'http://localhost' },
    { key: 'port', type: 'number', label: 'Port' },
    { key: 'verbose', type: 'toggle', label: 'Verbose', default: true },
    {
      key: 'token',
      type: 'secret',
      label: 'Token',
      env_key: 'HASS_TOKEN',
      set: true,
    },
    { name: 'garbage' }, // malformed → dropped
  ];
}

describe('normalizeSchemaFields', () => {
  it('normalizes fields and drops malformed entries', () => {
    const fields = normalizeSchemaFields(schema());

    expect(fields.map((field) => field.key)).toEqual([
      'url',
      'port',
      'verbose',
      'token',
    ]);
    expect(fields[3]).toMatchObject({
      key: 'token',
      type: 'secret',
      envKey: 'HASS_TOKEN',
      set: true,
    });
  });

  it('returns an empty list for a non-array', () => {
    expect(normalizeSchemaFields(null)).toEqual([]);
  });
});

describe('applyExtensionsPanelList carries the settings schema', () => {
  it('exposes settingsSchema and hasSettingsSchema', () => {
    const [extension] = applyExtensionsPanelList({
      extensions: [{ name: 'ha', status: 'loaded', settings_schema: schema() }],
    });

    expect(extension.settingsSchema.map((field) => field.key)).toEqual([
      'url',
      'port',
      'verbose',
      'token',
    ]);
    expect(hasSettingsSchema(extension)).toBe(true);
  });

  it('hasSettingsSchema is false for a schema-less extension', () => {
    const [extension] = applyExtensionsPanelList({
      extensions: [{ name: 'plain', status: 'loaded' }],
    });

    expect(extension.settingsSchema).toEqual([]);
    expect(hasSettingsSchema(extension)).toBe(false);
  });
});

describe('buildSchemaFormState', () => {
  it('seeds non-secret fields and excludes secrets', () => {
    const state = buildSchemaFormState(schema(), {
      url: 'http://host:8123',
      port: 80,
      verbose: false,
    });

    expect(state).toEqual({
      url: 'http://host:8123',
      port: '80',
      verbose: false,
    });
    expect('token' in state).toBe(false);
  });

  it('uses the toggle default and empty text when config is absent', () => {
    const state = buildSchemaFormState(schema(), {});

    expect(state).toEqual({ url: '', port: '', verbose: true });
  });
});

describe('buildSchemaConfigFromForm', () => {
  it('omits empty text/number, keeps toggles explicit, and parses numbers', () => {
    const built = buildSchemaConfigFromForm(schema(), {
      url: '',
      port: '8123',
      verbose: false,
    });

    expect(built.ok).toBe(true);
    expect(built.config).toEqual({ port: 8123, verbose: false });
    // Integer text yields an int (serializes without a fractional part).
    expect(Number.isInteger(built.config.port)).toBe(true);
  });

  it('parses a float and keeps a filled text field', () => {
    const built = buildSchemaConfigFromForm(schema(), {
      url: 'http://x',
      port: '80.5',
      verbose: true,
    });

    expect(built.config).toEqual({
      url: 'http://x',
      port: 80.5,
      verbose: true,
    });
  });

  it('blocks the save with a per-field error on unparseable numbers', () => {
    const built = buildSchemaConfigFromForm(schema(), {
      url: 'http://x',
      port: 'not-a-number',
      verbose: true,
    });

    expect(built.ok).toBe(false);
    expect(built.errors.port).toBeTruthy();
    expect('port' in built.config).toBe(false);
  });

  it('never emits a secret into the config', () => {
    const built = buildSchemaConfigFromForm(schema(), {
      url: 'http://x',
      token: 'should-be-ignored',
    });

    expect('token' in built.config).toBe(false);
  });
});

describe('extension schema i18n keys', () => {
  it('registers the new form and secret labels', () => {
    const requiredKeys = [
      'settings.extensions.saveSettings',
      'settings.extensions.fieldAria',
      'settings.extensions.numberInvalid',
      'settings.extensions.secretSet',
      'settings.extensions.secretUnset',
      'settings.extensions.secretSave',
      'settings.extensions.secretClear',
      'settings.extensions.secretPlaceholder',
      'settings.extensions.secretAria',
      'settings.extensions.secretSaved',
      'settings.extensions.secretCleared',
      'settings.extensions.waiting',
      'settings.extensions.waitingFor',
    ];

    for (const key of requiredKeys) {
      expect(englishCatalog[key], key).toBeTruthy();
    }
  });
});
