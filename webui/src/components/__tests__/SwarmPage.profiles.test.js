// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  tick,
  unmount,
  profile,
  button,
  saveButton,
  saveStateText,
  createBridge,
  overrideOperations,
  callsTo,
  settle,
  fill,
  pickFolder,
  choose,
  render,
  fixtureState,
} from './SwarmPage.support.js';
import { t, tOr } from '../../lib/i18n.js';

const EDIT = t('common.edit');
const NEW_SWARM = t('swarm.newProfile');
const SAVE_SWARM = t('swarm.profile.save');
const SAVED = t('common.saved');
const SYSTEM_PROMPT = t('swarm.profile.systemPrompt');
const TOOLS = t('swarm.profile.access');
const COMMUNICATION = t('swarm.profile.communication');
const GENERATE_PREVIEW = t('swarm.profile.generatePreview');
const DELETE_SWARM = t('swarm.delete.title');

async function openEditor(bridge) {
  await render(bridge);
  button(EDIT).click();
  await tick();
  flushSync();
}
async function openNewProfile(bridge) {
  await render(bridge);
  button(NEW_SWARM).click();
  await tick();
  flushSync();
}
async function reopenEditor(bridge) {
  await unmount(fixtureState.mounted);
  fixtureState.mounted = null;
  await openEditor(bridge);
}
const lastSaved = (operation) =>
  callsTo(operation, 'profiles.save').at(-1)[1].profile;
const toggle = (name) =>
  document.querySelector(`[role="switch"][aria-label="${name}"]`);
const blockTitle = (id) => tOr(`systemPrompt.blockTitle.${id}`, id);
const panel = (id) => document.getElementById(`swarm-profile-panel-${id}`);
const dialog = () => document.querySelector('[role="dialog"]');

describe('Swarm profile prompt', () => {
  it('shows prompt contributions and persists independent context and reminder switches', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    button(SYSTEM_PROMPT).click();
    await tick();
    const runtime = blockTitle('core:runtime');
    const project = blockTitle('core:working_project');
    const resume = t('swarm.profile.reminderResume');
    for (const [block, checked] of [
      ['core:tools', 'true'],
      ['core:skills', 'true'],
      ['core:runtime', 'false'],
      ['core:working_project', 'false'],
      ['extension:new', 'false'],
    ])
      expect(toggle(blockTitle(block)).getAttribute('aria-checked')).toBe(
        checked,
      );
    expect(document.body.textContent).toContain('test-owned runtime block');
    expect(document.body.textContent).toContain('test-owned resume guidance');
    toggle(runtime).click();
    toggle(project).click();
    toggle(resume).click();
    await tick();
    saveButton().click();
    await settle(20);
    const saved = lastSaved(operation);
    expect(saved.prompt_blocks).toEqual([
      'core:tools',
      'core:skills',
      'core:runtime',
      'core:working_project',
    ]);
    expect(saved.reminders.resume).toBe(false);
    expect(saved.working_directory).toEqual(profile.working_directory);
    await reopenEditor(bridge);
    expect(toggle(runtime).getAttribute('aria-checked')).toBe('true');
    expect(toggle(project).getAttribute('aria-checked')).toBe('true');
    expect(toggle(resume).getAttribute('aria-checked')).toBe('false');
  });

  it('previews the current unsaved profile and hides stale or late previews', async () => {
    const { bridge, operation } = createBridge();
    let complete;
    overrideOperations(operation, {
      'profiles.preview': () => new Promise((resolve) => (complete = resolve)),
    });
    await openNewProfile(bridge);
    await choose('swarm-model-0', 'demo/model');
    fill('swarm-directory', 'C:/work');
    button(COMMUNICATION).click();
    await tick();
    fill('swarm-coalesce', '350');
    button(SYSTEM_PROMPT).click();
    await tick();
    fill('swarm-instructions', 'unsaved-sentinel');
    await tick();
    button(GENERATE_PREVIEW).click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'profiles.preview',
      expect.objectContaining({
        profile: expect.objectContaining({
          instructions: 'unsaved-sentinel',
          delivery: expect.objectContaining({ coalesce_ms: 350 }),
          prompt_blocks: ['core:tools', 'core:skills'],
        }),
        formation_index: 0,
      }),
    );
    complete({
      preview: {
        text: 'preview-sentinel',
        blocks: [],
        tools: [{ name: 'swarm_state', description: 'definition-sentinel' }],
      },
    });
    await tick();
    await settle();
    flushSync();
    const preview = () =>
      document.querySelector('[data-testid="swarm-prompt-preview"]');
    expect(preview().textContent).toBe('preview-sentinel');
    expect(document.body.textContent).toContain('definition-sentinel');
    fill('swarm-instructions', 'changed-sentinel');
    await tick();
    expect(preview()).toBeNull();
    button(GENERATE_PREVIEW).click();
    await tick();
    fill('swarm-instructions', 'newer-sentinel');
    await tick();
    complete({ preview: { text: 'stale-sentinel', blocks: [], tools: [] } });
    await tick();
    expect(preview()).toBeNull();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
  });

  it.each(['', 'test-owned replacement instructions'])(
    'persists a new-profile prompt edited to %j',
    async (replacement) => {
      const { bridge, operation } = createBridge();
      await openNewProfile(bridge);
      const input = document.getElementById('swarm-instructions');
      expect(input.disabled).toBe(false);
      expect(input.readOnly).toBe(false);
      fill('swarm-instructions', replacement);
      fill('swarm-profile-name', 'Prompt profile');
      await choose('swarm-model-0', 'demo/model');
      fill('swarm-directory', 'C:/work');
      await tick();
      button(SAVE_SWARM).click();
      await settle(20);
      expect(lastSaved(operation).instructions).toBe(replacement);
      await reopenEditor(bridge);
      expect(document.getElementById('swarm-instructions').value).toBe(
        replacement,
      );
    },
  );

  it.each(['', 'test-owned existing instructions'])(
    'preserves an existing profile prompt of %j',
    async (instructions) => {
      const { bridge, operation } = createBridge({ ...profile, instructions });
      await openEditor(bridge);
      expect(document.getElementById('swarm-instructions').value).toBe(
        instructions,
      );
      expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    },
  );
});

describe('Swarm profile settings', () => {
  it('saves a new profile with its visible default prompt through the bridge', async () => {
    const { bridge, operation } = createBridge();
    await openNewProfile(bridge);
    const prompt = document.getElementById('swarm-instructions').value;
    expect(prompt).toBe('test-owned editable default');
    fill('swarm-profile-name', 'New profile');
    await choose('swarm-model-0', 'demo/model');
    await choose('swarm-effort-0', 'high');
    await pickFolder('swarm-directory', 'C:/w', 'work');
    expect(bridge.listDirectory).toHaveBeenCalledWith({
      path: 'C:/',
      include_files: false,
    });
    button(SAVE_SWARM).click();
    await settle(20);
    expect(document.body.textContent).not.toContain(
      t('swarm.profile.nameRequired'),
    );
    expect(operation).toHaveBeenCalledWith(
      'profiles.save',
      expect.objectContaining({
        expected_revision: null,
        profile: expect.objectContaining({
          name: 'New profile',
          instructions: prompt,
          participants: [
            { model: 'demo/model', count: 2, thinking_effort: 'high' },
          ],
          working_directory: { kind: 'directory', path: 'C:/work/' },
        }),
      }),
    );
    await reopenEditor(bridge);
    expect(document.getElementById('swarm-instructions').value).toBe(prompt);
  });

  it('filters Models and resets incompatible reasoning when the Model changes', async () => {
    const { bridge } = createBridge();
    await openNewProfile(bridge);
    document.getElementById('swarm-model-0').click();
    await tick();
    const search = document.querySelector(
      '.searchable-dropdown__search [role="combobox"]',
    );
    search.value = 'plain';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    expect(
      [...document.querySelectorAll('[role="option"]')].map((el) =>
        el.textContent.trim(),
      ),
    ).toEqual(['demo/plain']);
    document.querySelector('[role="option"]').click();
    await tick();
    expect(document.getElementById('swarm-effort-0').disabled).toBe(true);
    await choose('swarm-model-0', 'demo/model');
    await choose('swarm-effort-0', 'high');
    await choose('swarm-model-0', 'demo/plain');
    expect(document.getElementById('swarm-effort-0').textContent.trim()).toBe(
      t('swarm.profile.providerDefault'),
    );
  });

  it('keeps drafts across tabs and saves bulk Tool selections', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    fill('swarm-instructions', 'test-owned instruction body');
    button(TOOLS).click();
    await settle();
    expect(panel('access').hidden).toBe(false);
    button(t('toolAccess.selectAll')).click();
    await tick();
    saveButton().click();
    await tick();
    await tick();
    const saved = lastSaved(operation);
    expect(saved.tool_access).toEqual({
      mode: 'selected',
      allowed: ['browser', 'read', 'write'],
      granted: ['browser'],
    });
    expect(saved.instructions).toBe('test-owned instruction body');
    expect(saved.slug).toBe('research');
    await settle();
    button(TOOLS).click();
    await tick();
    button(t('toolAccess.deselectAll')).click();
    await tick();
    saveButton().click();
    await tick();
    await tick();
    expect(lastSaved(operation).tool_access).toEqual({
      mode: 'selected',
      allowed: [],
    });
  });

  it('shows and saves each private Swarm Tool independently', async () => {
    const { bridge, operation } = createBridge();
    const names = ['swarm_board', 'swarm_inbox', 'swarm_state', 'swarm_wiki'];
    overrideOperations(operation, {
      catalog: async (_args, fallback) => {
        const result = await fallback();
        result.catalog.tools.push(
          ...names.map((name) => ({
            name,
            family: 'swarm',
            family_label: 'Swarm',
            activation: 'session_grant',
          })),
        );
        return result;
      },
    });
    await render(bridge);
    button(EDIT).click();
    await vi.waitFor(() => expect(button(TOOLS)).toBeDefined());
    button(TOOLS).click();
    await tick();
    const tool = (name) => document.querySelector(`[data-tool-name="${name}"]`);
    for (const name of names)
      expect(tool(name)?.getAttribute('aria-checked')).toBe('true');
    tool('swarm_state').click();
    await tick();
    tool('swarm_board').click();
    await tick();
    saveButton().click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'profiles.save',
        expect.objectContaining({
          profile: expect.objectContaining({
            tool_access: expect.objectContaining({
              denied: ['swarm_state', 'swarm_board'],
            }),
          }),
        }),
      ),
    );
  });

  it('switches working-directory sources without sending stale fields', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    await choose('swarm-directory-source', t('swarm.profile.projectOption'));
    await choose('swarm-project', 'Project A');
    saveButton().click();
    await tick();
    await tick();
    expect(lastSaved(operation).working_directory).toEqual({
      kind: 'project',
      project_id: 'project-a',
    });
  });

  it('overrides the Compaction Policy for participants and returns to inheritance', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    const custom = () => toggle(t('swarm.profile.customCompaction'));
    const editor = () =>
      document.querySelector('[data-testid="swarm-compaction-editor"]');
    // A profile saved without the field inherits and is not dirty on open.
    expect(custom().getAttribute('aria-checked')).toBe('false');
    expect(editor()).toBeNull();
    expect(saveButton()).toBeNull();

    custom().click();
    await tick();
    flushSync();
    expect(editor()).not.toBeNull();
    const tail = editor().querySelector(
      `input[aria-label="${t('compaction.strategy.tailTokens')}"]`,
    );
    tail.value = '9000';
    tail.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    await choose('swarm-compaction-summary-model', 'demo/plain');
    saveButton().click();
    await vi.waitFor(() => expect(saveStateText()).toBe(SAVED));
    expect(lastSaved(operation).compaction_policy).toEqual({
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: {
        type: 'summary_tail',
        tail_tokens: 9000,
        summary_model: 'demo/plain',
      },
    });

    custom().click();
    await tick();
    flushSync();
    expect(editor()).toBeNull();
    saveButton().click();
    await vi.waitFor(() => expect(saveStateText()).toBe(SAVED));
    expect(lastSaved(operation).compaction_policy).toBeNull();
  });
});

describe('Swarm profile saving', () => {
  it('returns to the invalid field and keeps failed saves editable', async () => {
    const { bridge, operation } = createBridge();
    await openNewProfile(bridge);
    button(COMMUNICATION).click();
    await tick();
    button(SAVE_SWARM).click();
    await tick();
    expect(panel('overview').hidden).toBe(false);
    await settle();
    expect(document.activeElement.id).toBe('swarm-profile-name');
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    fill('swarm-profile-name', 'Retry');
    fill('swarm-directory', 'C:/work');
    await choose('swarm-model-0', 'demo/model');
    operation.mockRejectedValueOnce(new Error('save-failure-test'));
    button(SAVE_SWARM).click();
    await vi.waitFor(() =>
      expect(
        [...document.querySelectorAll('[role="alert"]')].map(
          (alert) => alert.textContent,
        ),
      ).toContainEqual(expect.stringContaining('save-failure-test')),
    );
    expect(document.getElementById('swarm-profile-name').value).toBe('Retry');
    expect(button(SAVE_SWARM).disabled).toBe(false);
  });

  it('autosaves after the shared debounce without closing or replacing the editor', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    vi.useFakeTimers();
    const name = () => document.getElementById('swarm-profile-name').value;
    fill('swarm-profile-name', 'Autosaved profile');
    await tick();
    await vi.advanceTimersByTimeAsync(799);
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    await tick();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(1);
    expect(name()).toBe('Autosaved profile');
    expect(bridge.autosave.hasPending()).toBe(false);
    bridge.invalidate();
    await vi.advanceTimersByTimeAsync(0);
    expect(name()).toBe('Autosaved profile');
    expect(saveButton()).toBeNull();
    expect(saveStateText()).toBe(SAVED);
  });

  it('offers Save while the draft is unsaved and confirms the finished save', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    expect(saveButton()).toBeNull();
    expect(saveStateText()).toBe('');
    fill('swarm-profile-name', 'Renamed profile');
    await tick();
    flushSync();
    expect(saveButton().textContent.trim()).toBe(t('common.save'));
    saveButton().click();
    await vi.waitFor(() => expect(saveStateText()).toBe(SAVED));
    expect(saveButton()).toBeNull();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(1);
    expect(lastSaved(operation).name).toBe('Renamed profile');
    fill('swarm-profile-name', 'Research');
    await tick();
    flushSync();
    expect(saveButton().textContent.trim()).toBe(t('common.save'));
    expect(saveStateText()).toBe('');
  });

  it('flushes newer edits made during an in-flight save with the returned revision', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    const saved = {
      ...structuredClone(profile),
      name: 'First edit',
      revision: 2,
    };
    let complete;
    operation.mockImplementationOnce(
      () => new Promise((resolve) => (complete = resolve)),
    );
    fill('swarm-profile-name', 'First edit');
    await tick();
    const flushing = bridge.autosave.flush();
    await tick();
    fill('swarm-profile-name', 'Second edit');
    await tick();
    complete({ profile: saved });
    await expect(flushing).resolves.toBe(true);
    const writes = callsTo(operation, 'profiles.save');
    expect(writes).toHaveLength(2);
    expect(writes[1][1]).toMatchObject({
      expected_revision: 2,
      profile: { name: 'Second edit' },
    });
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Second edit',
    );
    expect(bridge.autosave.hasPending()).toBe(false);
  });

  it('blocks a topic transition on save failure and discards only when requested', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    fill('swarm-profile-name', 'Unsaved');
    await tick();
    operation.mockRejectedValueOnce(new Error('autosave-failure-test'));
    button(COMMUNICATION).click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull());
    expect(panel('overview').hidden).toBe(false);
    button(t('autosave.discardAndContinue')).click();
    await tick();
    expect(panel('communication').hidden).toBe(false);
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Research',
    );
    expect(bridge.autosave.hasPending()).toBe(false);
  });

  it('keeps the profile navigation and draft mounted during invalidation without Refresh in the editor', async () => {
    const { bridge, operation } = createBridge();
    await openNewProfile(bridge);
    const input = document.getElementById('swarm-profile-name');
    fill('swarm-profile-name', 'Draft survives');
    await tick();
    bridge.invalidate();
    await settle();
    expect(document.getElementById('swarm-profile-name')).toBe(input);
    expect(input.value).toBe('Draft survives');
    expect(
      document.querySelector(`nav[aria-label="${t('swarm.profiles')}"]`),
    ).not.toBeNull();
    expect(button(t('common.refresh'))).toBeUndefined();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
  });

  it('flushes profile edits before leaving for a retained Swarm', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Research').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'Before navigation');
    await tick();
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    const calls = operation.mock.calls.map(([name]) => name);
    expect(calls.indexOf('profiles.save')).toBeLessThan(
      calls.indexOf('swarms.get'),
    );
    expect(document.querySelector('.editor')).toBeNull();
    expect(document.querySelector('.swarm-head h2').textContent).toBe(
      'Research',
    );
  });

  it('keeps a failed profile save open when selecting a retained Swarm', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Research').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'Unsaved navigation');
    await tick();
    operation.mockRejectedValueOnce(new Error('navigation-save-failure'));
    button('Investigate').click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull());
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Unsaved navigation',
    );
    expect(callsTo(operation, 'swarms.get')).toHaveLength(0);
  });
});

describe('Swarm profile deletion', () => {
  const confirm = () =>
    [...dialog().querySelectorAll('button')].find(
      (node) => node.textContent.trim() === t('common.delete'),
    );

  it('deletes the open Swarm from its editor without saving the discarded draft', async () => {
    const { bridge, operation } = createBridge();
    let deleted = false;
    overrideOperations(operation, {
      'profiles.delete': (args) => {
        deleted = true;
        return { profile_id: args.profile_id, deleted: true };
      },
      'profiles.list': (_args, fallback) =>
        deleted ? { entries: [], has_more: false } : fallback(),
    });
    await render(bridge);
    button('Research').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'Saved rename');
    await tick();
    saveButton().click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'profiles.save',
        expect.objectContaining({ expected_revision: 1 }),
      ),
    );
    await vi.waitFor(() => expect(button(DELETE_SWARM).disabled).toBe(false));
    fill('swarm-profile-name', 'Discarded draft');
    await tick();
    button(DELETE_SWARM).click();
    await tick();
    expect(dialog().textContent).toContain(
      t('swarm.delete.body', {
        name: 'Saved rename',
      }),
    );
    confirm().click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-profile-editor')).toBeNull(),
    );
    expect(operation).toHaveBeenCalledWith('profiles.delete', {
      profile_id: 'prf-a',
      expected_revision: 2,
    });
    const calls = operation.mock.calls.map(([name]) => name);
    expect(calls.lastIndexOf('profiles.save')).toBeLessThan(
      calls.indexOf('profiles.delete'),
    );
    expect(dialog()).toBeNull();
    expect(button('Saved rename')).toBeUndefined();
    expect(button('Investigate')).toBeDefined();
  });

  it('retains the profile deletion confirmation and allows retry after a failure', async () => {
    const { bridge, operation } = createBridge();
    overrideOperations(operation, {
      'profiles.delete': () => Promise.reject(new Error('Storage unavailable')),
    });
    await render(bridge);
    button('Research').click();
    await tick();
    flushSync();
    button(DELETE_SWARM).click();
    await tick();
    confirm().click();
    await vi.waitFor(() =>
      expect(dialog().textContent).toContain('Storage unavailable'),
    );
    expect(confirm().disabled).toBe(false);
    confirm().click();
    await vi.waitFor(() =>
      expect(callsTo(operation, 'profiles.delete')).toHaveLength(2),
    );
    expect(document.querySelector('.swarm-profile-editor')).not.toBeNull();
  });
});
