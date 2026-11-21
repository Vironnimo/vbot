// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const api = vi.hoisted(() => ({
  listExternalSources: vi.fn(),
  listExternalSessions: vi.fn(),
  readExternalSession: vi.fn(),
  readExternalMemories: vi.fn(),
  importExternalSession: vi.fn(),
  importExternalMemory: vi.fn(),
}));
vi.mock('$lib/api.js', async (original) => ({ ...(await original()), ...api }));
const { default: Dialog } = await import('../ExternalHistoryDialog.svelte');
const { default: Panel } =
  await import('../SettingsExternalHistoryPanel.svelte');
const { default: Entry } = await import('../../chat/ChatTimelineEntry.svelte');
let component;
const onImported = vi.fn();
const onClose = vi.fn();
const button = (label) =>
  [...document.querySelectorAll('button')].find(
    (el) => (el.getAttribute('aria-label') || el.textContent.trim()) === label,
  );
async function settle() {
  flushSync();
  await tick();
  await tick();
  flushSync();
}
async function click(label) {
  const el = button(label);
  expect(el).toBeTruthy();
  el.click();
  await settle();
}
beforeEach(() => {
  init('en');
  vi.clearAllMocks();
  api.listExternalSources.mockResolvedValue({
    items: [{ id: 'codex', name: 'Codex', path: '/codex' }],
  });
  api.listExternalSessions.mockResolvedValue({
    items: [{ id: 'source', path: '/source.jsonl', title: 'Previous work' }],
    total: 1,
    next_offset: null,
  });
  api.readExternalSession.mockResolvedValue({
    entry_count: 1,
    items: [{ ordinal: 0, role: 'tool', content: '<script>old</script>' }],
    next_offset: null,
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

it.each(['coder', 'dev@repo'])(
  'opens the Session imported from Settings into %s',
  async (address) => {
    api.importExternalSession.mockResolvedValue({
      session_id: 'imported',
      agent_id: address,
    });
    const onOpenSession = vi.fn();
    component = mount(Panel, {
      target: document.body,
      props: {
        agents: [{ id: 'coder', name: 'Coder', root_project_id: 'repo' }],
        projects: [
          {
            project_id: 'repo',
            display_name: 'Repo',
            scan: { team: [{ agent_id: 'dev' }] },
          },
        ],
        onOpenSession,
      },
    });
    await settle();
    expect(api.listExternalSources).not.toHaveBeenCalled();
    await click(t('external.open'));
    await click(t('external.browse'));
    await click('Previous work');
    expect(api.importExternalSession).not.toHaveBeenCalled();
    expect(document.querySelector('pre').textContent).toBe(
      '<script>old</script>',
    );
    expect(document.querySelector('script')).toBeNull();
    expect(button(t('external.continue')).disabled).toBe(true);
    await click(t('dropdown.placeholder'));
    await click(address === 'coder' ? 'Coder' : address);
    if (address === 'coder') {
      await click(t('common.defaultOption', { label: 'Repo' }));
      await click(t('external.workspace'));
    }
    await click(t('external.continue'));
    expect(api.importExternalSession).toHaveBeenCalledWith({
      source: 'codex',
      path: '/source.jsonl',
      source_session_id: 'source',
      agent_id: address,
      ...(address === 'coder' ? { working_project_id: null } : {}),
    });
    expect(onOpenSession).toHaveBeenCalledWith(address, 'imported');
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  },
);

it('binds reviewed Unicode Memory facts to the selected preview and requires a refresh on change', async () => {
  api.readExternalMemories.mockResolvedValue({
    items: [
      {
        path: '/source.md',
        name: 'source.md',
        content: 'Review 🧪 notes.',
        char_offset: 0,
        characters: 15,
        digest: 'a'.repeat(64),
      },
    ],
    total: 1,
  });
  api.importExternalMemory.mockRejectedValueOnce(
    Object.assign(new Error('The source changed'), {
      code: 'external_memory_changed',
    }),
  );
  component = mount(Dialog, {
    target: document.body,
    props: { agents: [{ id: 'coder' }], projects: [], onClose, onImported },
  });
  await settle();
  await click(t('external.sessions'));
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (el) => el.textContent.trim() === t('external.memories'),
  );
  option.click();
  await settle();
  await click(t('external.browse'));
  await click('source.md');
  expect(document.querySelector('textarea').value).toBe('Review 🧪 notes.');
  await click(t('external.importMemory'));
  expect(api.importExternalMemory).toHaveBeenCalledWith({
    source: 'codex',
    path: '/source.md',
    scope: 'agent',
    expected_digest: 'a'.repeat(64),
    agent_id: 'coder',
    entries: ['Review 🧪 notes.'],
  });
  const importButton = [...document.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === t('external.importMemory'),
  );
  expect(importButton.disabled).toBe(true);
  expect(document.querySelector('textarea').value).toBe('Review 🧪 notes.');
  api.readExternalMemories.mockResolvedValue({
    items: [
      {
        path: '/source.md',
        name: 'source.md',
        content: 'New facts.',
        char_offset: 0,
        characters: 10,
        digest: 'b'.repeat(64),
      },
    ],
    total: 1,
  });
  api.importExternalMemory.mockResolvedValue({ entries: [] });
  await click(t('external.browse'));
  await click('source.md');
  await click(t('external.importMemory'));
  expect(api.importExternalMemory).toHaveBeenLastCalledWith(
    expect.objectContaining({
      expected_digest: 'b'.repeat(64),
      entries: ['New facts.'],
    }),
  );
});

it('renders imported text with a source marker and inert historical tool details', async () => {
  component = mount(Entry, {
    target: document.body,
    props: {
      item: {
        id: 'foreign',
        type: 'external_history',
        message: {
          role: 'assistant',
          content: 'old_shell: historical result',
          external_history: {
            source: 'codex',
            role: 'tool',
            kind: 'tool_activity',
          },
        },
      },
    },
  });
  await settle();
  expect(document.body.textContent).toContain(
    t('external.imported', { source: 'codex' }),
  );
  expect(document.querySelector('details').open).toBe(false);
  expect(document.querySelector('pre').textContent).toContain('old_shell');
});
