// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import {
  openSearchableDropdown,
  selectSearchableOption,
} from './AgentsView.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsLibrarianPanel } =
  await import('../settings/SettingsLibrarianPanel.svelte');

const SETTINGS = Object.freeze({
  librarian: {
    enabled: false,
    interval_days: 7,
    archive_after_days: 90,
    consolidate: true,
  },
});
const MODELS = [
  {
    id: 'openai/gpt-5.2-mini',
    provider_id: 'openai',
    model_id: 'gpt-5.2-mini',
    name: 'GPT-5.2 Mini',
    capabilities: { tools: true },
    context_window: 128000,
    effective_context_window: 128000,
  },
];
const CONNECTIONS = [
  {
    id: 'openai:api-key',
    provider_id: 'openai',
    type: 'api_key',
    label: 'API Key',
    usable: true,
  },
];
const LIBRARIAN = Object.freeze({
  id: 'librarian',
  name: 'Librarian',
  builtin: 'librarian',
  model: 'anthropic/claude-default',
  config: {
    model: '',
    fallback_models: [],
    temperature: null,
    thinking_effort: null,
  },
  effective: {
    model: { value: 'anthropic/claude-default', source: 'global_default' },
  },
});
const PASS = Object.freeze({
  agent_id: 'main',
  agent_name: 'Main',
  started_at: '2026-09-30T10:00:00.000000Z',
  finished_at: '2026-09-30T10:04:00.000000Z',
  trigger: 'schedule',
  archived: 1,
  candidates: 3,
  consolidation: 'ran',
  session_id: 'lib-1',
  run_id: 'r-9',
  created: 0,
  changed: 1,
  merged: 1,
});

function serve({ problem = null, passes = [PASS], running = null } = {}) {
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'model.list') return { models: MODELS };
    if (method === 'connection.list') return { connections: CONNECTIONS };
    if (method === 'librarian.overview')
      return {
        agent_id: 'librarian',
        available: problem === null,
        problem,
        settings: SETTINGS.librarian,
        running,
        passes,
      };
    if (method === 'agent.get') return LIBRARIAN;
    if (method === 'agent.update')
      return { ...LIBRARIAN, config: { ...LIBRARIAN.config, ...params } };
    return { librarian: params.librarian };
  });
}

describe('SettingsLibrarianPanel', () => {
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

  function render(props = {}) {
    mountedComponent = mount(SettingsLibrarianPanel, {
      target: document.body,
      props: { settings: SETTINGS, ...props },
    });
    flushSync();
  }

  const calls = (method) =>
    rpcMock.mock.calls
      .filter(([name]) => name === method)
      .map(([, params]) => params);

  it('shows the interval only while the schedule is on, keeps the hand-run options, and saves the section with the Librarian’s Model', async () => {
    serve();
    const commits = [];
    render({ onCommit: (next) => commits.push(next) });

    const [scheduleToggle, consolidateToggle] =
      document.body.querySelectorAll('[role="switch"]');
    const intervalInput = () =>
      document.getElementById('settings-librarian-interval');
    const archiveInput = () =>
      document.getElementById('settings-librarian-archive-after');
    expect(scheduleToggle.getAttribute('aria-checked')).toBe('false');
    expect(consolidateToggle.getAttribute('aria-checked')).toBe('true');
    expect(intervalInput().closest('.s-row').hidden).toBe(true);
    // A pass started by hand uses these even while the schedule is off.
    expect(archiveInput().closest('.s-row').hidden).toBe(false);
    expect(archiveInput().value).toBe('90');

    // The Librarian is an Agent of its own: its Model shows what it inherits
    // and saves with agent.update.
    await waitForCondition(
      () =>
        Boolean(document.getElementById('settings-librarian-model')) &&
        calls('connection.list').length > 0,
    );
    expect(calls('agent.get')).toEqual([{ id: 'librarian' }]);
    await openSearchableDropdown('settings-librarian-model');
    selectSearchableOption('settings-librarian-model', 'openai/gpt-5.2-mini');

    scheduleToggle.click();
    flushSync();
    expect(intervalInput().closest('.s-row').hidden).toBe(false);
    intervalInput().value = '14';
    intervalInput().dispatchEvent(new Event('input', { bubbles: true }));
    archiveInput().value = '0';
    archiveInput().dispatchEvent(new Event('input', { bubbles: true }));
    archiveInput().value = '3651';
    archiveInput().dispatchEvent(new Event('input', { bubbles: true }));
    consolidateToggle.click();
    flushSync();
    document.body.querySelector('.save-status button').click();
    flushSync();
    await waitForCondition(
      () => commits.length === 1 && calls('agent.update').length === 1,
    );

    // The rejected 0 and 3651 keep the last valid archive age.
    expect(calls('settings.update')).toEqual([
      {
        librarian: {
          enabled: true,
          consolidate: false,
          interval_days: 14,
          archive_after_days: 90,
        },
        base: { librarian: SETTINGS.librarian },
      },
    ]);
    expect(commits[0].librarian.interval_days).toBe(14);
    expect(calls('agent.update')).toEqual([
      { model: 'openai/gpt-5.2-mini::api-key', id: 'librarian' },
    ]);
  });

  it('lists the recent passes with their Sessions and says why an unavailable Librarian cannot run', async () => {
    serve({ running: 'coder' });
    const onOpenSession = vi.fn();
    render({
      agents: [{ id: 'coder', name: 'Coder' }],
      onOpenSession,
    });
    await waitForCondition(() =>
      document.body.textContent.includes('Open session'),
    );

    expect(document.body.textContent).toContain(
      t('settings.librarian.running', { name: 'Coder' }),
    );
    const row = [...document.body.querySelectorAll('.s-row')].find((item) =>
      item.textContent.includes('Open session'),
    );
    expect(row.querySelector('.s-row-label').textContent).toBe('Main');
    expect(row.querySelector('.s-row-desc').textContent).toContain(
      '(scheduled) · 1 merged away, 1 changed, 0 created',
    );
    row.querySelector('button').click();
    expect(onOpenSession).toHaveBeenCalledWith('librarian', 'lib-1');

    // While one of the user's Agents holds its id, the Librarian is
    // unavailable: the panel names the fix and never edits that Agent.
    await unmount(mountedComponent);
    rpcMock.mockReset();
    serve({ problem: 'agent_id_taken', passes: [] });
    render();
    await waitForCondition(() =>
      document.body.textContent.includes(t('settings.librarian.passesEmpty')),
    );

    expect(document.body.querySelector('.banner').textContent.trim()).toBe(
      t('skills.librarian.unavailable', {
        problem: t('librarian.problem.agentIdTaken'),
      }),
    );
    expect(calls('agent.get')).toEqual([]);
    expect(document.getElementById('settings-librarian-model')).toBeNull();
  });
});

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    if (check()) {
      return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}
