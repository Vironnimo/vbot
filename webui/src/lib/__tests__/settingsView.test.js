// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../i18n.js';
import {
  buildLanguageOptions,
  createLanguageUpdatePayload,
  describeProvider,
  formatServerHost,
  providerStatusClass,
  providerStatusLabel,
} from '../settingsView.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsView } =
  await import('../../components/SettingsView.svelte');

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders the desktop split layout, loads settings data, and keeps token-count controls absent', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    const root = document.body.querySelector(
      'section.settings-layout.view.active',
    );
    expect(root).not.toBeNull();
    expect(root?.firstElementChild?.classList.contains('settings-nav')).toBe(
      true,
    );
    expect(root?.lastElementChild?.classList.contains('settings-content')).toBe(
      true,
    );
    expect(document.body.textContent).toContain('Loading settings…');

    await waitForText('0.0.0.0:9001');

    expect(rpcMock).toHaveBeenCalledWith('settings.get');
    expect(document.body.textContent).toContain('Server host');
    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).toContain('Data directory');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot');
    expect(document.body.textContent).not.toMatch(
      /show[_ -]?token[_ -]?counts/i,
    );
    expect(document.body.textContent).not.toMatch(/token count/i);

    clickButton('Providers');

    expect(document.body.textContent).toContain('Anthropic');
    expect(document.body.textContent).toContain('Missing credentials');
    expect(document.body.textContent).toContain('OpenAI');
    expect(document.body.textContent).toContain('Configured');
    expect(document.body.textContent).toContain('Custom endpoint');
    expect(document.body.textContent).toContain('Placeholder');
  });

  it('renders load failures and retries settings.get successfully', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('server offline'))
      .mockResolvedValueOnce(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    await waitForText('Settings could not be loaded. server offline');

    expect(document.body.textContent).toContain(
      'Settings could not be loaded. server offline',
    );

    clickButton('Retry');

    expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.get');

    await waitForText('0.0.0.0:9001');

    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).not.toContain('server offline');
  });

  it('saves appearance language through the narrow settings.update flow', async () => {
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload())
      .mockResolvedValueOnce(
        createSettingsPayload({
          appearance: {
            language: 'fr',
            available_languages: ['en', 'fr'],
          },
        }),
      );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    await waitForText('0.0.0.0:9001');

    clickButton('Appearance');

    const saveButton = getButton('Save');
    const languageSelect = document.body.querySelector('select');

    expect(languageSelect).not.toBeNull();
    expect(saveButton.disabled).toBe(true);

    languageSelect.value = 'fr';
    languageSelect.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();

    expect(saveButton.disabled).toBe(false);

    saveButton.click();
    flushSync();

    expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      appearance: {
        language: 'fr',
      },
    });

    await waitForText('Language preference updated.');

    expect(document.body.textContent).toContain('Language preference updated.');
    expect(document.body.querySelector('select')?.value).toBe('fr');
    expect(getButton('Save').disabled).toBe(true);
  });
});

describe('settingsView helpers', () => {
  it('formats provider metadata and current status labels', () => {
    const provider = {
      name: 'OpenAI',
      base_url: 'https://api.openai.com/v1',
      credential_key: 'OPENAI_API_KEY',
      credentials_configured: true,
      status: 'configured',
      model_count: 2,
    };

    expect(
      formatServerHost(
        { listen_host: '127.0.0.1', listen_port: 8420 },
        translate,
      ),
    ).toBe('127.0.0.1:8420');
    expect(
      buildLanguageOptions({ language: 'en', available_languages: ['en'] }),
    ).toEqual([
      {
        id: 'en',
        labelKey: 'settings.language.en',
        labelFallback: 'en',
      },
    ]);
    expect(createLanguageUpdatePayload('fr')).toEqual({
      appearance: {
        language: 'fr',
      },
    });
    expect(describeProvider(provider, translate)).toBe(
      'Credential key: OPENAI_API_KEY. Endpoint: https://api.openai.com/v1. 2 models available.',
    );
    expect(providerStatusClass(provider)).toBe('chip-green');
    expect(providerStatusLabel(provider, translate)).toBe('Configured');
  });
});

function createSettingsPayload(overrides = {}) {
  const base = {
    general: {
      server: {
        listen_host: '0.0.0.0',
        listen_port: 9001,
        port_source: 'settings.server_port',
      },
      data_directory: 'C:/Users/test/.vbot',
    },
    providers: {
      items: [
        {
          id: 'anthropic',
          name: 'Anthropic',
          base_url: 'https://api.anthropic.com/v1',
          credential_key: 'ANTHROPIC_API_KEY',
          credentials_configured: false,
          status: 'missing_credentials',
          model_count: 1,
        },
        {
          id: 'openai',
          name: 'OpenAI',
          base_url: 'https://api.openai.com/v1',
          credential_key: 'OPENAI_API_KEY',
          credentials_configured: true,
          status: 'configured',
          model_count: 2,
        },
      ],
    },
    appearance: {
      language: 'en',
      available_languages: ['en', 'fr'],
    },
  };

  return mergeSettings(base, overrides);
}

function mergeSettings(base, overrides) {
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) {
    return base;
  }

  const result = { ...base };

  for (const [key, value] of Object.entries(overrides)) {
    if (Array.isArray(value)) {
      result[key] = value;
      continue;
    }

    if (value && typeof value === 'object') {
      result[key] = mergeSettings(base[key] ?? {}, value);
      continue;
    }

    result[key] = value;
  }

  return result;
}

function getButton(label) {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.textContent?.trim() === label,
  );

  if (!button) {
    throw new Error(`Button not found: ${label}`);
  }

  return button;
}

function clickButton(label) {
  getButton(label).click();
  flushSync();
}

async function waitForText(text, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();

    if (document.body.textContent?.includes(text)) {
      return;
    }
  }

  throw new Error(`Timed out waiting for text: ${text}`);
}

function translate(key, fallback, values) {
  const templates = {
    'common.unknown': 'Unknown',
    'settings.providers.description.credentialKey':
      'Credential key: {credentialKey}.',
    'settings.providers.description.baseUrl': 'Endpoint: {baseUrl}.',
    'settings.providers.description.modelCount': '{count} models available.',
    'settings.providers.description.none':
      'Provider metadata is not available yet.',
    'settings.providers.status.configured': 'Configured',
    'settings.providers.status.missingCredentials': 'Missing credentials',
    'settings.providers.status.placeholder': 'Placeholder',
  };
  const template = templates[key] ?? fallback ?? key;

  if (!values) {
    return template;
  }

  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
    return Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name])
      : match;
  });
}
