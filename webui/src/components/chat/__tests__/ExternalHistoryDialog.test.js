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
const { default: Entry } = await import('../ChatTimelineEntry.svelte');
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

it('reads without importing, then imports the selected full Session into the named Agent', async () => {
  api.importExternalSession.mockResolvedValue({
    session_id: 'imported',
    agent_id: 'coder',
  });
  component = mount(Dialog, {
    target: document.body,
    props: { agentAddress: 'coder', onClose, onImported },
  });
  await settle();
  await click(t('external.browse'));
  await click('Previous work');
  expect(api.importExternalSession).not.toHaveBeenCalled();
  expect(document.querySelector('pre').textContent).toBe(
    '<script>old</script>',
  );
  expect(document.querySelector('script')).toBeNull();
  await click(t('external.continue'));
  expect(api.importExternalSession).toHaveBeenCalledWith({
    source: 'codex',
    path: '/source.jsonl',
    source_session_id: 'source',
    agent_id: 'coder',
  });
  expect(onImported).toHaveBeenCalledWith({
    session_id: 'imported',
    agent_id: 'coder',
  });
});

it('seeds facts for a complete Memory preview with supplementary Unicode characters', async () => {
  api.readExternalMemories.mockResolvedValue({
    items: [
      {
        path: '/source.md',
        name: 'source.md',
        content: 'Review 🧪 notes.',
        char_offset: 0,
        characters: 15,
      },
    ],
    total: 1,
  });
  api.importExternalMemory.mockResolvedValue({ entries: [] });
  component = mount(Dialog, {
    target: document.body,
    props: { agentAddress: 'coder', onClose, onImported },
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
    agent_id: 'coder',
    entries: ['Review 🧪 notes.'],
  });
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
