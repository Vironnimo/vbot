// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SettingsActivityPanel } =
  await import('../settings/SettingsActivityPanel.svelte');

const DOWNLOAD = Object.freeze({
  id: 'local_setup:local/parakeet',
  kind: 'local_model_install',
  label: 'Parakeet',
  state: 'running',
  phase: 'downloading',
  error: '',
  message: '',
  target: 'local/parakeet',
  task_type: 'speech_to_text',
  progress: { completed: 512e6, total: 1.1e9, unit: 'bytes' },
});
const RECALL_FAILED = Object.freeze({
  id: 'recall_index',
  kind: 'recall_index',
  label: '',
  state: 'failed',
  phase: '',
  error: 'unknown_code',
  message: '',
});
const EMBEDDING_RESTART = Object.freeze({
  id: 'local_setup:local/granite',
  kind: 'local_model_install',
  label: 'Granite',
  state: 'action_required',
  phase: 'restart_required',
  error: '',
  message: '',
  target: 'local/granite',
  task_type: 'text_embedding',
});

describe('SettingsActivityPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    rpcMock.mockResolvedValue({ activities: [] });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  function render(props) {
    mountedComponent = mount(SettingsActivityPanel, {
      target: document.body,
      props,
    });
    flushSync();
  }

  function rows() {
    return [...document.body.querySelectorAll('[data-activity]')].map(
      (row) => ({
        id: row.dataset.activity,
        title: row.querySelector('.s-row-label').textContent.trim(),
        status: row.querySelector('.s-row-desc').textContent.trim(),
        progress:
          row.querySelector('.progress-bar__text')?.textContent.trim() ?? '',
        buttons: [...row.querySelectorAll('button')].map((button) =>
          button.textContent.trim(),
        ),
      }),
    );
  }

  it('says so when nothing runs', () => {
    render({ activities: [] });

    expect(document.body.textContent.trim()).toBe('No background activity.');
  });

  it('shows each activity with its progress and the actions it allows', async () => {
    const opened = [];
    render({
      activities: [
        DOWNLOAD,
        {
          id: 'recall_index',
          kind: 'recall_index',
          label: '',
          state: 'running',
          phase: 'indexing',
          error: '',
          message: '',
          eta_seconds: 30,
          progress: { completed: 1200, total: 5000, unit: 'items' },
        },
        EMBEDDING_RESTART,
        {
          id: 'whatsapp_setup:wa',
          kind: 'whatsapp_setup',
          label: 'wa',
          state: 'failed',
          phase: '',
          error: 'setup_failed',
          message: 'Node.js is missing',
        },
      ],
      onOpen: (section) => opened.push(section),
    });

    expect(rows()).toEqual([
      {
        id: 'local_setup:local/parakeet',
        title: 'Speech to text: Parakeet',
        status: 'Downloading the model…',
        progress: '512 MB of 1.1 GB',
        buttons: ['Open', 'Cancel'],
      },
      {
        id: 'recall_index',
        title: 'Conversation search index',
        status: 'Indexing conversations…',
        progress: 'Indexed 1,200 of 5,000 passages · less than a minute left',
        buttons: ['Open'],
      },
      {
        id: 'local_setup:local/granite',
        title: 'Conversation search: Granite',
        status: 'Installed. Restart the vBot server to use it.',
        progress: '',
        buttons: ['Open', 'Dismiss'],
      },
      {
        id: 'whatsapp_setup:wa',
        title: 'WhatsApp support: wa',
        status: 'Node.js is missing',
        progress: '',
        buttons: ['Open', 'Dismiss'],
      },
    ]);

    const buttons = (id) =>
      document.body.querySelectorAll(`[data-activity="${id}"] button`);
    for (const id of [
      'local_setup:local/parakeet',
      'recall_index',
      'whatsapp_setup:wa',
    ]) {
      buttons(id)[0].click();
    }
    expect(opened).toEqual(['speech_models', 'recall', 'channels']);

    buttons('local_setup:local/parakeet')[1].click();
    buttons('whatsapp_setup:wa')[1].click();
    await vi.waitFor(() => expect(rpcMock).toHaveBeenCalledTimes(2));
    expect(rpcMock.mock.calls).toEqual([
      ['task_model.local_setup_cancel', { target: 'local/parakeet' }],
      ['activity.dismiss', { id: 'whatsapp_setup:wa' }],
    ]);
  });

  it('reports a failed action', async () => {
    const onError = vi.fn();
    rpcMock.mockRejectedValue(new Error(''));
    render({ activities: [RECALL_FAILED], onError });
    expect(rows()[0].status).toBe('Indexing failed.');

    document.body.querySelectorAll('[data-activity] button')[1].click();

    await vi.waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        'The server could not be reached. Try again.',
      ),
    );
  });
});
