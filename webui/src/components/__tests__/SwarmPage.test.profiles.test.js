// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  tick,
  unmount,
  profile,
  button,
  createBridge,
  fill,
  choose,
  render,
  fixtureState,
} from './SwarmPage.support.js';

describe('SwarmPage', () => {
  it('shows prompt contributions and persists independent context and reminder switches', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    button('System Prompt').click();
    await tick();
    const toggle = (name) =>
      document.querySelector(`[role="switch"][aria-label="${name}"]`);
    expect(toggle('Tool guidance').getAttribute('aria-checked')).toBe('true');
    expect(toggle('Available Skills').getAttribute('aria-checked')).toBe(
      'true',
    );
    expect(toggle('Runtime').getAttribute('aria-checked')).toBe('false');
    expect(toggle('Working Project').getAttribute('aria-checked')).toBe(
      'false',
    );
    expect(toggle('extension:new').getAttribute('aria-checked')).toBe('false');
    expect(document.body.textContent).toContain('test-owned runtime block');
    expect(document.body.textContent).toContain('test-owned resume guidance');
    toggle('Runtime').click();
    toggle('Working Project').click();
    toggle('When you resume work').click();
    await tick();
    button('Save changes').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    const saved = operation.mock.calls
      .filter(([name]) => name === 'profiles.save')
      .at(-1)[1].profile;
    expect(saved.prompt_blocks).toEqual([
      'core:tools',
      'core:skills',
      'core:runtime',
      'core:working_project',
    ]);
    expect(saved.reminders.resume).toBe(false);
    expect(saved.working_directory).toEqual(profile.working_directory);
    await unmount(fixtureState.mounted);
    fixtureState.mounted = null;
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    expect(toggle('Runtime').getAttribute('aria-checked')).toBe('true');
    expect(toggle('Working Project').getAttribute('aria-checked')).toBe('true');
    expect(toggle('When you resume work').getAttribute('aria-checked')).toBe(
      'false',
    );
  });

  it('previews the current unsaved profile and hides stale or late previews', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let complete;
    operation.mockImplementation((name, args) =>
      name === 'profiles.preview'
        ? new Promise((resolve) => (complete = resolve))
        : original(name, args),
    );
    await render(bridge);
    button('New Swarm').click();
    await tick();
    flushSync();
    await choose('swarm-model-0', 'demo/model');
    fill('swarm-directory', 'C:/work');
    button('Communication').click();
    await tick();
    fill('swarm-coalesce', '350');
    button('System Prompt').click();
    await tick();
    fill('swarm-instructions', 'unsaved-sentinel');
    await tick();
    button('Generate preview').click();
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
    await new Promise((resolve) => setTimeout(resolve));
    flushSync();
    expect(
      document.querySelector('[data-testid="swarm-prompt-preview"]')
        .textContent,
    ).toBe('preview-sentinel');
    expect(document.body.textContent).toContain('definition-sentinel');
    fill('swarm-instructions', 'changed-sentinel');
    await tick();
    expect(
      document.querySelector('[data-testid="swarm-prompt-preview"]'),
    ).toBeNull();
    button('Generate preview').click();
    await tick();
    fill('swarm-instructions', 'newer-sentinel');
    await tick();
    complete({ preview: { text: 'stale-sentinel', blocks: [], tools: [] } });
    await tick();
    expect(
      document.querySelector('[data-testid="swarm-prompt-preview"]'),
    ).toBeNull();
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
  });

  it('saves a new profile through the bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('New Swarm').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'New profile');
    await choose('swarm-model-0', 'demo/model');
    await choose('swarm-effort-0', 'high');
    fill('swarm-directory', 'C:/work');
    await tick();
    button('Save Swarm').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await tick();
    expect(document.body.textContent).not.toContain('Enter a name');
    expect(operation).toHaveBeenCalledWith(
      'profiles.save',
      expect.objectContaining({
        expected_revision: null,
        profile: expect.objectContaining({
          name: 'New profile',
          participants: [
            { model: 'demo/model', count: 2, thinking_effort: 'high' },
          ],
        }),
      }),
    );
  });

  it.each([null, '', 'test-owned replacement instructions'])(
    'persists the visible new-profile prompt after editing it to %s',
    async (replacement) => {
      const { bridge, operation } = createBridge();
      await render(bridge);
      button('New Swarm').click();
      await tick();
      flushSync();
      const input = document.getElementById('swarm-instructions');
      const initial = input.value;
      expect(initial.trim().length).toBeGreaterThan(0);
      expect(input.disabled).toBe(false);
      expect(input.readOnly).toBe(false);
      if (replacement !== null) fill('swarm-instructions', replacement);
      const expected = replacement ?? initial;
      fill('swarm-profile-name', 'Prompt profile');
      await choose('swarm-model-0', 'demo/model');
      fill('swarm-directory', 'C:/work');
      await tick();
      button('Save Swarm').click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'profiles.save',
        expect.objectContaining({
          profile: expect.objectContaining({ instructions: expected }),
        }),
      );
      await unmount(fixtureState.mounted);
      fixtureState.mounted = null;
      await render(bridge);
      button('Edit').click();
      await tick();
      flushSync();
      expect(document.getElementById('swarm-instructions').value).toBe(
        expected,
      );
    },
  );

  it.each(['', 'test-owned existing instructions'])(
    'preserves an existing profile prompt of %s',
    async (instructions) => {
      const { bridge, operation } = createBridge({ ...profile, instructions });
      await render(bridge);
      button('Edit').click();
      await tick();
      flushSync();
      expect(document.getElementById('swarm-instructions').value).toBe(
        instructions,
      );
      expect(
        operation.mock.calls.some(([name]) => name === 'profiles.save'),
      ).toBe(false);
    },
  );

  it('filters Models and resets incompatible reasoning when the Model changes', async () => {
    const { bridge } = createBridge();
    await render(bridge);
    button('New Swarm').click();
    await tick();
    flushSync();
    document.getElementById('swarm-model-0').click();
    await tick();
    const search = document.querySelector('[role="combobox"]');
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
      'Provider default',
    );
  });

  it('keeps drafts across tabs and materializes All and None tool selections', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    fill('swarm-instructions', 'test-owned instruction body');
    button('Tools & Skills').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.getElementById('swarm-profile-panel-access').hidden).toBe(
      false,
    );
    document.querySelector('[role="radio"][aria-label="All"]')?.click();
    const radio = [...document.querySelectorAll('[role="radio"]')].find(
      (el) => el.textContent.trim() === 'All',
    );
    radio.click();
    await tick();
    button('Save changes').click();
    await tick();
    await tick();
    const saved = operation.mock.calls
      .filter(([name]) => name === 'profiles.save')
      .at(-1)[1].profile;
    expect(saved.tool_access).toEqual({
      mode: 'selected',
      allowed: ['read', 'write'],
    });
    expect(saved.instructions).toBe('test-owned instruction body');
    expect(saved.slug).toBe('research');
    await new Promise((resolve) => setTimeout(resolve));
    button('Tools & Skills').click();
    await tick();
    [...document.querySelectorAll('[role="radio"]')]
      .find((el) => el.textContent.trim() === 'None')
      .click();
    await tick();
    button('Save changes').click();
    await tick();
    await tick();
    expect(
      operation.mock.calls
        .filter(([name]) => name === 'profiles.save')
        .at(-1)[1].profile.tool_access,
    ).toEqual({ mode: 'selected', allowed: [] });
  });

  it('switches working-directory sources without sending stale fields', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    await choose('swarm-directory-source', 'Project');
    await choose('swarm-project', 'Project A');
    button('Save changes').click();
    await tick();
    await tick();
    expect(
      operation.mock.calls
        .filter(([name]) => name === 'profiles.save')
        .at(-1)[1].profile.working_directory,
    ).toEqual({ kind: 'project', project_id: 'project-a' });
  });

  it('returns to the invalid field and keeps failed saves editable', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('New Swarm').click();
    await tick();
    flushSync();
    button('Communication').click();
    await tick();
    button('Save Swarm').click();
    await tick();
    expect(document.getElementById('swarm-profile-panel-overview').hidden).toBe(
      false,
    );
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.activeElement.id).toBe('swarm-profile-name');
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
    fill('swarm-profile-name', 'Retry');
    fill('swarm-directory', 'C:/work');
    await choose('swarm-model-0', 'demo/model');
    operation.mockRejectedValueOnce(new Error('save-failure-test'));
    button('Save Swarm').click();
    await tick();
    await tick();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')).not.toBeNull(),
    );
    expect(document.getElementById('swarm-profile-name').value).toBe('Retry');
    expect(button('Save Swarm').disabled).toBe(false);
  });

  it('autosaves after the shared debounce without closing or replacing the editor', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    vi.useFakeTimers();
    fill('swarm-profile-name', 'Autosaved profile');
    await tick();
    await vi.advanceTimersByTimeAsync(799);
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await tick();
    expect(
      operation.mock.calls.filter(([name]) => name === 'profiles.save'),
    ).toHaveLength(1);
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Autosaved profile',
    );
    expect(bridge.autosave.hasPending()).toBe(false);
    bridge.invalidate();
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Autosaved profile',
    );
    button('Save changes').click();
    await vi.advanceTimersByTimeAsync(0);
    expect(
      operation.mock.calls.filter(([name]) => name === 'profiles.save'),
    ).toHaveLength(1);
    expect(bridge.toast).toHaveBeenCalled();
  });

  it('flushes newer edits made during an in-flight save with the returned revision', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
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
    const writes = operation.mock.calls.filter(
      ([name]) => name === 'profiles.save',
    );
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
    await render(bridge);
    button('Edit').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'Unsaved');
    await tick();
    operation.mockRejectedValueOnce(new Error('autosave-failure-test'));
    button('Communication').click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).not.toBeNull(),
    );
    expect(document.getElementById('swarm-profile-panel-overview').hidden).toBe(
      false,
    );
    button('Discard and continue').click();
    await tick();
    expect(
      document.getElementById('swarm-profile-panel-communication').hidden,
    ).toBe(false);
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Research',
    );
    expect(bridge.autosave.hasPending()).toBe(false);
  });

  it('keeps the profile navigation and draft mounted during invalidation without Refresh in the editor', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('New Swarm').click();
    await tick();
    flushSync();
    const input = document.getElementById('swarm-profile-name');
    fill('swarm-profile-name', 'Draft survives');
    await tick();
    bridge.invalidate();
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.getElementById('swarm-profile-name')).toBe(input);
    expect(input.value).toBe('Draft survives');
    expect(document.querySelector('nav[aria-label="Swarms"]')).not.toBeNull();
    expect(button('Refresh')).toBeUndefined();
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
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
    expect(button('Refresh')).toBeDefined();
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
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).not.toBeNull(),
    );
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Unsaved navigation',
    );
    expect(operation.mock.calls.some(([name]) => name === 'swarms.get')).toBe(
      false,
    );
  });
});
