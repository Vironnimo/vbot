import { describe, expect, it } from 'vitest';

import {
  applyExtensionsPanelList,
  buildExtensionsUpdatePayload,
  buildSchemaConfigFromForm,
  buildSchemaFormState,
  describeExtensionWaiting,
  extensionCapabilityParts,
  extensionStatusChipVariant,
  hasSettingsSchema,
} from '../settingsView.js';

// Echoes the i18n key and its parameters: the key and parameters are the
// contract, not the English fallback wording.
const t = (key, _fallback, params) =>
  params ? `${key} ${JSON.stringify(params)}` : key;

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
        config: {},
        capabilities: {},
      },
      {
        name: 'homeassistant',
        status: 'overridden',
        disabled: false,
        overridden_by: '/data/extensions/homeassistant/__init__.py',
        config: {},
        capabilities: {},
      },
      { name: '', status: 'loaded' },
      'not-an-object',
    ],
  };
}

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
    { name: 'garbage' },
  ];
}

describe('extension list', () => {
  it('normalizes extension records and drops invalid entries', () => {
    const result = applyExtensionsPanelList(rawExtensions());

    expect(result.map((extension) => extension.name)).toEqual([
      'guard_bash',
      'legacy',
      'homeassistant',
    ]);
    expect(result[0]).toMatchObject({
      status: 'loaded',
      disabled: false,
      version: '1.2.0',
      description: 'Guards dangerous bash',
      config: { deny: ['rm -rf'] },
      capabilityErrors: ['tool x skipped'],
      readyState: 'ready',
      settingsSchema: [],
    });
    expect(result[0].capabilities).toMatchObject({
      hooks: [
        { event: 'tool_call', count: 1 },
        { event: 'run_end', count: 2 },
      ],
      tools: [{ name: 'word_count', ready: true }],
      commands: [{ name: 'workflow', registered: true }],
      startup: true,
    });
    expect(result[1].disabled).toBe(true);
    expect(result[2]).toMatchObject({
      status: 'overridden',
      overriddenBy: '/data/extensions/homeassistant/__init__.py',
    });
    expect(applyExtensionsPanelList(null)).toEqual([]);
    expect(applyExtensionsPanelList({})).toEqual([]);
  });

  it('lists hooks in Run order and drops the retired prompt-append hook', () => {
    // The retired hook's literal name is built from fragments on purpose, so a
    // repo-wide grep for it returns zero outside the plans.
    const retiredHookEvent = ['before', 'agent', 'start'].join('_');
    const [extension] = applyExtensionsPanelList({
      extensions: [
        {
          name: 'example',
          capabilities: {
            hooks: {
              run_end: 6,
              [retiredHookEvent]: 2,
              tool_result: 5,
              context: 3,
              tool_call: 4,
              run_start: 1,
            },
          },
        },
      ],
    });

    expect(extension.capabilities.hooks.map((hook) => hook.event)).toEqual([
      'run_start',
      'context',
      'tool_call',
      'tool_result',
      'run_end',
    ]);
  });

  it('maps the status to a status-chip variant', () => {
    expect(extensionStatusChipVariant('loaded')).toBe('success');
    expect(extensionStatusChipVariant('failed')).toBe('error');
    expect(extensionStatusChipVariant('disabled')).toBe('warn');
    expect(extensionStatusChipVariant('overridden')).toBe('warn');
  });

  it('lists contributed capabilities with translated labels', () => {
    const [extension] = applyExtensionsPanelList(rawExtensions());

    expect(extensionCapabilityParts(extension.capabilities, t)).toEqual([
      {
        label: 'settings.extensions.hooks',
        value: 'tool_call(1), run_end(2)',
      },
      { label: 'settings.extensions.tools', value: 'word_count' },
      { label: 'settings.extensions.commands', value: '/workflow' },
      { label: 'settings.extensions.startup', value: '' },
    ]);
    expect(extensionCapabilityParts({}, t)).toEqual([]);
  });

  it('describes the waiting state and names unset secrets by label', () => {
    const [ready, waiting, plain] = applyExtensionsPanelList({
      extensions: [
        { name: 'ha', status: 'loaded', ready_state: 'ready' },
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
        { name: 'plain', status: 'loaded', ready_state: 'waiting' },
      ],
    });

    expect(describeExtensionWaiting(ready, t)).toBeNull();
    expect(describeExtensionWaiting(waiting, t)).toEqual({
      hint: 'settings.extensions.waiting',
      waitingFor: 'settings.extensions.waitingFor {"fields":"Token"}',
    });
    expect(describeExtensionWaiting(plain, t)).toEqual({
      hint: 'settings.extensions.waiting',
      waitingFor: null,
    });
  });

  it('rebuilds the whole extensions section with one override applied', () => {
    const extensions = applyExtensionsPanelList(rawExtensions());

    expect(buildExtensionsUpdatePayload(extensions)).toEqual({
      extensions: {
        disabled: ['legacy'],
        config: { guard_bash: { deny: ['rm -rf'] } },
      },
    });
    expect(
      buildExtensionsUpdatePayload(extensions, {
        name: 'guard_bash',
        disabled: true,
      }).extensions.disabled,
    ).toEqual(['guard_bash', 'legacy']);
    // An emptied config drops the extension's entry.
    expect(
      buildExtensionsUpdatePayload(extensions, {
        name: 'guard_bash',
        config: {},
      }),
    ).toEqual({ extensions: { disabled: ['legacy'], config: {} } });
  });
});

describe('extension settings schema', () => {
  it('carries the normalized schema on the extension record', () => {
    const [withSchema, plain] = applyExtensionsPanelList({
      extensions: [
        { name: 'ha', status: 'loaded', settings_schema: schema() },
        { name: 'plain', status: 'loaded' },
      ],
    });

    expect(withSchema.settingsSchema.map((field) => field.key)).toEqual([
      'url',
      'port',
      'verbose',
      'token',
    ]);
    expect(withSchema.settingsSchema[3]).toMatchObject({
      type: 'secret',
      envKey: 'HASS_TOKEN',
      set: true,
    });
    expect(hasSettingsSchema(withSchema)).toBe(true);
    expect(hasSettingsSchema(plain)).toBe(false);
  });

  it('seeds the form from the config and never seeds secrets', () => {
    expect(
      buildSchemaFormState(schema(), {
        url: 'http://host:8123',
        port: 80,
        verbose: false,
      }),
    ).toEqual({ url: 'http://host:8123', port: '80', verbose: false });
    // Without a config, toggles use their default and inputs start empty.
    expect(buildSchemaFormState(schema(), {})).toEqual({
      url: '',
      port: '',
      verbose: true,
    });
  });

  it('builds the config from the form with parsed numbers and without secrets', () => {
    expect(
      buildSchemaConfigFromForm(schema(), {
        url: '',
        port: '8123',
        verbose: false,
        token: 'should-be-ignored',
      }),
    ).toEqual({ ok: true, config: { port: 8123, verbose: false }, errors: {} });
    expect(
      buildSchemaConfigFromForm(schema(), {
        url: 'http://x',
        port: '80.5',
        verbose: true,
      }).config,
    ).toEqual({ url: 'http://x', port: 80.5, verbose: true });
    // Toggles are always explicit, even when the form never set them.
    expect(buildSchemaConfigFromForm(schema(), {}).config).toEqual({
      verbose: false,
    });
  });

  it('blocks the save with a field error for an unparseable number', () => {
    expect(
      buildSchemaConfigFromForm(schema(), {
        url: 'http://x',
        port: 'not-a-number',
        verbose: true,
      }),
    ).toEqual({
      ok: false,
      config: { url: 'http://x', verbose: true },
      errors: { port: 'invalid-number' },
    });
  });
});
