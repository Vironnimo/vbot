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
const CREATE_SWARM = t('swarm.profile.create');
const SAVED = t('common.saved');
const GENERATE_PREVIEW = t('swarm.profile.generatePreview');
const DELETE_SWARM = t('swarm.profile.deleteAction');

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
const dialog = () => document.querySelector('[role="dialog"]');
const inDialog = (text) =>
  [...dialog().querySelectorAll('button')].find(
    (node) => node.textContent.trim() === text,
  );
// Adds `fields` to the catalog the page reads.
const extendCatalog = (operation, extend) =>
  overrideOperations(operation, {
    catalog: async (_args, fallback) => {
      const result = await fallback();
      extend(result.catalog);
      return result;
    },
  });
async function saveAndConfirm() {
  saveButton().click();
  await vi.waitFor(() => expect(saveStateText()).toBe(SAVED));
}

describe('Swarm profile prompt', () => {
  it('shows prompt contributions and persists independent context and reminder switches', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
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
    // A reminder without a title is listed under its event name.
    expect(toggle('wake').getAttribute('aria-checked')).toBe('false');
    toggle(runtime).click();
    toggle(project).click();
    toggle(resume).click();
    toggle('wake').click();
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
    expect(saved.reminders).toEqual({
      delivery: true,
      resume: false,
      wake: true,
    });
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
    fill('swarm-delivery-coalesce', '350');
    fill('swarm-instructions', 'unsaved-sentinel');
    document.querySelector('[aria-controls="swarm-preview"]').click();
    await tick();
    expect(document.getElementById('swarm-preview').hidden).toBe(false);
    // A preview needs what a save needs except the name: a missing setting
    // shows its error and takes focus instead of a request.
    button(GENERATE_PREVIEW).click();
    await settle();
    expect(callsTo(operation, 'profiles.preview')).toHaveLength(0);
    expect(
      document.getElementById('swarm-directory-error').textContent.trim(),
    ).toBe(t('swarm.profile.directoryRequired'));
    expect(document.activeElement?.id).toBe('swarm-directory');
    fill('swarm-directory', 'C:/work');
    await tick();
    button(GENERATE_PREVIEW).click();
    await tick();
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
    // The preview has no size estimates, so a definition shows none.
    expect(document.querySelector('.tool-index-item').textContent.trim()).toBe(
      'swarm_state',
    );
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
      button(CREATE_SWARM).click();
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
      prefix: 'w',
    });
    button(CREATE_SWARM).click();
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

  it('saves bulk Tool selections together with other unsaved edits', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    fill('swarm-instructions', 'test-owned instruction body');
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
    extendCatalog(operation, (catalog) =>
      catalog.tools.push(
        ...names.map((name) => ({
          name,
          family: 'swarm',
          family_label: 'Swarm',
          activation: 'session_grant',
        })),
      ),
    );
    await openEditor(bridge);
    const tool = (name) => document.querySelector(`[data-tool-name="${name}"]`);
    await vi.waitFor(() => expect(tool('swarm_board')).not.toBeNull());
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

  it('adds new Skills automatically until single Skills are chosen', async () => {
    const { bridge, operation } = createBridge();
    extendCatalog(operation, (catalog) => {
      catalog.skills = [
        { name: 'alpha', description: 'test-owned alpha' },
        { name: 'beta', description: 'test-owned beta' },
      ];
    });
    await openEditor(bridge);
    const autoAdd = () => toggle(t('skills.panel.autoAdd'));
    const skill = (name) =>
      document.querySelector(
        `[role="checkbox"][aria-label="${t('skills.panel.toggle', { name })}"]`,
      );
    await vi.waitFor(() => expect(skill('beta')).not.toBeNull());
    expect(autoAdd().getAttribute('aria-checked')).toBe('true');
    expect(skill('beta').getAttribute('aria-checked')).toBe('true');
    skill('beta').click();
    await tick();
    expect(autoAdd().getAttribute('aria-checked')).toBe('false');
    await saveAndConfirm();
    expect(lastSaved(operation).allowed_skills).toEqual(['alpha']);
    autoAdd().click();
    await tick();
    expect(skill('beta').getAttribute('aria-checked')).toBe('true');
    await saveAndConfirm();
    expect(lastSaved(operation).allowed_skills).toEqual(['*']);
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

  it('offers the Team agents of the selected Project and drops them outside it', async () => {
    const { bridge, operation } = createBridge();
    extendCatalog(operation, (catalog) => {
      catalog.projects[0].agent_profiles = [
        { id: 'reviewer', name: 'Reviewer', source: 'claude' },
      ];
    });
    await openEditor(bridge);
    const teamAgent = () => document.getElementById('swarm-team-agent-0');
    await vi.waitFor(() =>
      expect(document.getElementById('swarm-directory-source')).not.toBeNull(),
    );
    expect(teamAgent()).toBeNull();
    await choose('swarm-directory-source', t('swarm.profile.projectOption'));
    await choose('swarm-project', 'Project A');
    await choose('swarm-team-agent-0', 'Reviewer');
    await saveAndConfirm();
    expect(lastSaved(operation).participants[0].repository_profile).toBe(
      'reviewer',
    );
    await choose('swarm-directory-source', t('swarm.profile.directoryOption'));
    expect(teamAgent()).toBeNull();
    fill('swarm-directory', 'C:/work');
    await saveAndConfirm();
    expect(lastSaved(operation).participants[0]).not.toHaveProperty(
      'repository_profile',
    );
  });

  it('saves when and how each route delivers Board messages and keeps an invalid limit at its field', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    await choose(
      'swarm-delivery-ping-receive',
      t('swarm.delivery.receive.pull'),
    );
    document.getElementById('swarm-delivery-discussion-wake').click();
    const disclosure = () =>
      document.querySelector('[aria-controls="swarm-delivery-advanced"]');
    const limitsShown = () =>
      !document.getElementById('swarm-delivery-advanced').hidden;
    const batchMessages = () =>
      document.getElementById('swarm-delivery-batch-messages');
    const batchError = () =>
      document.getElementById('swarm-delivery-batch-messages-error');
    disclosure().click();
    await tick();
    expect(limitsShown()).toBe(true);

    // A save stops at an invalid limit: its error shows under its field, out
    // of the closed disclosure, and nothing is saved until it is corrected.
    fill('swarm-delivery-batch-messages', '0');
    disclosure().click();
    await tick();
    expect(limitsShown()).toBe(false);
    saveButton().click();
    await vi.waitFor(() =>
      expect(document.activeElement.id).toBe('swarm-delivery-batch-messages'),
    );
    expect(limitsShown()).toBe(true);
    expect(batchError().textContent.trim()).toBe(
      t('swarm.communication.invalidNumber', {
        field: t('swarm.delivery.batchMessages'),
        min: '1',
        max: '100',
      }),
    );
    expect(batchMessages().getAttribute('aria-invalid')).toBe('true');
    expect(batchMessages().getAttribute('aria-describedby')).toBe(
      'swarm-delivery-batch-messages-help swarm-delivery-batch-messages-error',
    );
    await expect(bridge.autosave.flush()).resolves.toBe(false);
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    expect(batchMessages().value).toBe('0');
    expect(batchError()).not.toBeNull();
    fill('swarm-delivery-batch-messages', '5');
    await tick();
    expect(batchError()).toBeNull();
    expect(batchMessages().getAttribute('aria-invalid')).toBeNull();
    await saveAndConfirm();
    expect(lastSaved(operation).delivery).toEqual({
      main: { mode: 'all', wake_idle: true },
      discussion: { mode: 'all', wake_idle: false },
      ping: { mode: 'pull', wake_idle: true },
      coalesce_ms: 250,
      batch_messages: 5,
      batch_chars: 24000,
    });
  });

  it('overrides the Compaction Policy for participants and returns to inheritance', async () => {
    const { bridge, operation } = createBridge();
    await openEditor(bridge);
    const custom = () =>
      document.querySelector(
        '[role="switch"][aria-labelledby="swarm-compaction-custom-label"]',
      );
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
    const tail = document.querySelector(
      `input[aria-label="${t('compaction.strategy.tailTokens')}"]`,
    );
    tail.value = '9000';
    tail.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    await choose('swarm-compaction-summary-model', 'demo/plain');
    await saveAndConfirm();
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
    await saveAndConfirm();
    expect(lastSaved(operation).compaction_policy).toBeNull();
  });
});

describe('Swarm profile saving', () => {
  it('returns to the invalid field and keeps failed saves editable', async () => {
    const { bridge, operation } = createBridge();
    await openNewProfile(bridge);
    button(CREATE_SWARM).click();
    await settle();
    expect(document.activeElement.id).toBe('swarm-profile-name');
    expect(
      document.getElementById('swarm-profile-name-error').textContent.trim(),
    ).toBe(t('swarm.profile.nameRequired'));
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    fill('swarm-profile-name', 'Retry');
    fill('swarm-directory', 'C:/work');
    await choose('swarm-model-0', 'demo/model');
    operation.mockRejectedValueOnce(new Error('save-failure-test'));
    button(CREATE_SWARM).click();
    await vi.waitFor(() =>
      expect(
        [...document.querySelectorAll('[role="alert"]')].map(
          (alert) => alert.textContent,
        ),
      ).toContainEqual(expect.stringContaining('save-failure-test')),
    );
    expect(document.getElementById('swarm-profile-name').value).toBe('Retry');
    expect(button(CREATE_SWARM).disabled).toBe(false);
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

    // An invalid draft is reported in place; focus stays where the user is.
    fill('swarm-profile-slug', 'Not A Shortcut');
    document.getElementById('swarm-instructions').focus();
    await tick();
    await vi.advanceTimersByTimeAsync(800);
    expect(callsTo(operation, 'profiles.save')).toHaveLength(1);
    expect(
      document.getElementById('swarm-profile-slug-error').textContent.trim(),
    ).toBe(t('swarm.profile.shortcutValidation'));
    expect(document.activeElement.id).toBe('swarm-instructions');
    await expect(bridge.autosave.flush()).resolves.toBe(false);
    expect(document.activeElement.id).toBe('swarm-instructions');
    expect(callsTo(operation, 'profiles.save')).toHaveLength(1);
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
      'Investigate',
    );
    // The Run names the Swarm it started from, not the edited profile.
    expect(document.querySelector('.swarm-head__summary').textContent).toMatch(
      /^Research · /u,
    );
  });

  it('keeps unsaved profile changes open on leaving until they are completed or discarded', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Research').click();
    await tick();
    flushSync();
    // Incomplete settings cannot be saved; the user completes them.
    fill('swarm-profile-name', '');
    await tick();
    button('Investigate').click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull());
    expect(dialog().textContent).toContain(
      t('swarm.profile.leaveIncompleteBody'),
    );
    inDialog(t('swarm.profile.keepEditing')).click();
    await vi.waitFor(() =>
      expect(document.activeElement.id).toBe('swarm-profile-name'),
    );
    expect(dialog()).toBeNull();
    // A failed save keeps the changes until the user discards them.
    fill('swarm-profile-name', 'Unsaved navigation');
    await tick();
    operation.mockRejectedValueOnce(new Error('navigation-save-failure'));
    button('Investigate').click();
    await vi.waitFor(() => expect(dialog()).not.toBeNull());
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Unsaved navigation',
    );
    expect(callsTo(operation, 'swarms.get')).toHaveLength(0);
    inDialog(t('autosave.discardAndContinue')).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    expect(document.querySelector('.swarm-profile-editor')).toBeNull();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(1);
    expect(bridge.autosave).toBeNull();
  });

  it('keeps a changed new Swarm across navigation until Cancel discards it', async () => {
    const { bridge, operation } = createBridge();
    const name = () => document.getElementById('swarm-profile-name')?.value;
    const editorShown = () => document.querySelector('.swarm-profile-editor');
    await openNewProfile(bridge);
    fill('swarm-profile-name', 'Unfinished');
    await tick();
    // Leaving by the pane or by Back keeps the draft without asking, and New
    // Swarm or Forward continues it.
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    expect(dialog()).toBeNull();
    button(NEW_SWARM).click();
    await vi.waitFor(() => expect(name()).toBe('Unfinished'));
    bridge.goTo('');
    await vi.waitFor(() => expect(editorShown()).toBeNull());
    bridge.goTo('/profiles/new');
    await vi.waitFor(() => expect(name()).toBe('Unfinished'));
    // Cancel asks before it discards the draft.
    button(t('common.cancel')).click();
    await tick();
    expect(dialog().textContent).toContain(t('swarm.profile.discardTitle'));
    inDialog(t('swarm.profile.keepEditing')).click();
    await tick();
    expect(dialog()).toBeNull();
    expect(name()).toBe('Unfinished');
    button(t('common.cancel')).click();
    await tick();
    inDialog(t('common.discard')).click();
    await vi.waitFor(() => expect(editorShown()).toBeNull());
    button(NEW_SWARM).click();
    await vi.waitFor(() => expect(name()).toBe(''));
    // An untouched new Swarm cancels without asking.
    button(t('common.cancel')).click();
    await vi.waitFor(() => expect(editorShown()).toBeNull());
    expect(dialog()).toBeNull();
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
  });

  it('leaves no new Swarm draft behind once it is created', async () => {
    const { bridge, operation } = createBridge();
    await openNewProfile(bridge);
    fill('swarm-profile-name', 'Created draft');
    await choose('swarm-model-0', 'demo/model');
    fill('swarm-directory', 'C:/work');
    await tick();
    button(CREATE_SWARM).click();
    await settle(20);
    expect(lastSaved(operation)).toMatchObject({ name: 'Created draft' });
    button(NEW_SWARM).click();
    await vi.waitFor(() =>
      expect(document.getElementById('swarm-profile-name')?.value).toBe(''),
    );
  });
});

describe('Swarm profile deletion', () => {
  const confirm = () => inDialog(t('common.delete'));

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
