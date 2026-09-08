// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);

const { default: SwarmPage } =
  await import('../../../../resources/extensions/swarm/ui/SwarmPage.svelte');

let mounted;
const delivery = {
  main: { mode: 'all', wake_idle: true },
  discussion: { mode: 'all', wake_idle: true },
  ping: { mode: 'all', wake_idle: true },
  coalesce_ms: 250,
  batch_messages: 20,
  batch_chars: 24000,
};
const profile = {
  id: 'prf-a',
  revision: 1,
  schema_version: 1,
  name: 'Research',
  slug: 'research',
  participants: [{ model: 'demo/model', count: 2 }],
  working_directory: { kind: 'directory', path: 'C:/work' },
  tool_access: { mode: 'selected', allowed: [] },
  tools: {},
  allowed_skills: ['*'],
  instructions: '',
  prompt_blocks: ['core:tools', 'core:skills'],
  reminders: { delivery: true, wake: true, resume: true, completion: true },
  delivery,
};
const swarm = {
  id: 'swr-a',
  state: 'running',
  prompt: 'Investigate',
  main_discussion_id: 'dsc-main',
  settings_revision: 3,
  delivery,
  discussions: [
    { id: 'dsc-main', title: 'Main discussion' },
    { id: 'dsc-findings', title: 'Findings' },
  ],
  participants: [
    {
      id: 'prt-a',
      display_name: 'Alpha',
      model: 'demo/model',
      state: 'running',
    },
    {
      id: 'prt-b',
      display_name: 'Beta',
      model: 'demo/fallback',
      state: 'waiting',
    },
  ],
};

function button(text) {
  return [...document.querySelectorAll('button')].find((item) =>
    (item.getAttribute('aria-label') || item.textContent).includes(text),
  );
}

function createBridge(initialProfile = profile) {
  let storedProfile = structuredClone(initialProfile);
  let autosaveParticipant;
  let invalidate;
  let runListener;
  const operation = vi.fn((name, args) => {
    if (name === 'profiles.save') {
      storedProfile = {
        ...structuredClone(args.profile),
        id: args.profile.id ?? 'prf-new',
        revision: (args.expected_revision ?? 0) + 1,
        slug: args.profile.slug ?? 'generated-shortcut',
      };
      return Promise.resolve({ profile: structuredClone(storedProfile) });
    }
    if (name === 'catalog')
      return Promise.resolve({
        catalog: {
          prompt_defaults: {
            instructions: 'test-owned editable default',
            prompt_blocks: ['core:tools', 'core:skills'],
            reminders: {
              delivery: true,
              wake: true,
              resume: true,
              completion: true,
            },
          },
          prompt_blocks: [
            { id: 'core:runtime', text: 'test-owned runtime block' },
            { id: 'core:tools', text: 'test-owned tool style' },
            { id: 'core:skills', text: 'test-owned skills' },
            { id: 'core:working_project' },
            { id: 'extension:new', text: 'test-owned new extension' },
          ],
          reminder_texts: {
            delivery: 'test-owned delivery guidance',
            wake: 'test-owned wake guidance',
            resume: 'test-owned resume guidance',
            completion: 'test-owned completion guidance',
          },
          models: [
            {
              id: 'demo/model',
              name: 'Demo',
              context_window: 128000,
              capabilities: {
                tools: true,
                reasoning: {
                  supported: true,
                  control: 'levels',
                  levels: ['low', 'high'],
                },
              },
            },
            {
              id: 'demo/plain',
              name: 'Plain',
              context_window: 128000,
              capabilities: { tools: true, reasoning: { supported: false } },
            },
          ],
          tools: [
            { name: 'read' },
            { name: 'write' },
            { name: 'browser', requires_opt_in: true },
          ],
          projects: [{ id: 'project-a', name: 'Project A', cwd: 'C:/project' }],
        },
      });
    if (name === 'profiles.list')
      return Promise.resolve({
        entries: [structuredClone(storedProfile)],
        has_more: false,
      });
    if (name === 'swarms.list')
      return Promise.resolve({
        entries: [{ ...swarm, participant_count: 2, done_count: 0 }],
        has_more: false,
      });
    if (name === 'swarms.get')
      return Promise.resolve({ swarm: structuredClone(swarm) });
    if (name === 'board.list')
      return Promise.resolve({ entries: structuredClone(swarm.discussions) });
    if (name === 'board.read')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.events')
      return Promise.resolve({ entries: [], has_more: false });
    if (name === 'swarms.usage')
      return Promise.resolve({
        usage: {
          participant_id: args.participant_id ?? null,
          participant_count: 2,
          usage: {
            totals: {
              measured_input_tokens: 30,
              measured_output_tokens: 20,
              estimated_input_tokens: 10,
              estimated_output_tokens: 5,
            },
            models: [
              {
                provider: 'demo',
                model: args.participant_id === 'prt-b' ? 'fallback' : 'model',
                runs: 1,
                measured_input_tokens: 30,
                measured_output_tokens: 20,
                estimated_input_tokens: 10,
                estimated_output_tokens: 5,
              },
            ],
          },
          tools: { total_calls: 4 },
        },
      });
    return Promise.resolve({ profile: structuredClone(profile) });
  });
  let context;
  return {
    operation,
    bridge: {
      operation,
      registerAutosave: vi.fn((participant) => {
        autosaveParticipant = participant;
        return () => (autosaveParticipant = null);
      }),
      notifyAutosave: vi.fn(),
      toast: vi.fn(),
      get autosave() {
        return autosaveParticipant;
      },
      invalidate() {
        invalidate?.();
      },
      openLink: (url) => operation('link.open', { url }),
      openMedia: (url) => operation('media.open', { url }),
      replaceRoute: vi.fn(),
      readHistory: vi.fn(() =>
        Promise.resolve({
          status: 'completed',
          messages: [
            {
              id: 'msg-link',
              role: 'assistant',
              content: 'See https://example.test',
              timestamp: '2026-09-08T09:00:00+00:00',
            },
          ],
        }),
      ),
      subscribeRun: vi.fn(),
      unsubscribeRun: vi.fn(),
      onContext(callback) {
        context = callback;
        return () => {};
      },
      onInvalidation(callback) {
        invalidate = callback;
        return () => {};
      },
      onRunEvent(callback) {
        runListener = callback;
        return () => (runListener = null);
      },
      emitRun(id, event) {
        runListener?.(id, event);
      },
      dispose: vi.fn(),
      updateContext(next) {
        context(next);
      },
      show() {
        context({ route: '' });
      },
    },
  };
}

function fill(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}
async function choose(id, text) {
  document.getElementById(id).click();
  await tick();
  const option = [...document.querySelectorAll('[role="option"]')].find((el) =>
    el.textContent.trim().startsWith(text),
  );
  expect(option).toBeDefined();
  option.click();
  await tick();
}

async function render(bridge) {
  mounted = mount(SwarmPage, {
    target: document.body,
    props: { bridgeClient: bridge },
  });
  flushSync();
  bridge.show();
  await new Promise((resolve) => setTimeout(resolve));
  await tick();
  flushSync();
}

afterEach(async () => {
  if (mounted) mounted = await unmount(mounted);
  document.body.innerHTML = '';
  vi.useRealTimers();
});

describe('SwarmPage', () => {
  it('renders complete prompt, timestamped author headers and participants above posts', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const prompt = 'test-owned long prompt '.repeat(15) + '\nsecond line';
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: { ...structuredClone(swarm), prompt },
        });
      if (name === 'board.read')
        return Promise.resolve({
          entries: [
            {
              id: 'post-time',
              author: { name: 'Alpha' },
              text: 'test-owned board text',
              created_at: '2026-09-08T09:15:00+00:00',
            },
          ],
        });
      return original(name, args);
    });
    await render(bridge);
    bridge.updateContext({
      locale: 'en',
      timezone: 'Europe/Berlin',
      theme: {},
    });
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board time')).not.toBeNull(),
    );
    expect(document.querySelector('.goal').textContent).toBe(prompt);
    expect(document.querySelector('.swarm-head').textContent).not.toContain(
      'swr-a',
    );
    expect(document.querySelector('.swarm-tabs .chip')).not.toBeNull();
    expect(document.querySelector('.post-header strong').textContent).toBe(
      'Alpha',
    );
    expect(document.querySelector('.board time').dateTime).toBe(
      '2026-09-08T09:15:00+00:00',
    );
    expect(document.querySelector('.board time').textContent).toMatch(/11:15/);
    const roster = document.querySelector('.participant-pane');
    expect(
      roster.compareDocumentPosition(document.querySelector('.board')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    button('Usage').click();
    await tick();
    expect(document.querySelector('.swarm-identity dd').textContent).toBe(
      'swr-a',
    );
  });

  it('keeps a failed post in the modal and closes only after successful submission', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let fail = true;
    operation.mockImplementation((name, args) =>
      name === 'board.post' && fail
        ? Promise.reject(new Error('post-failed-sentinel'))
        : original(name, args),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Write post')).toBeDefined());
    button('Write post').click();
    await tick();
    expect(
      document.querySelector('[role="dialog"] #swarm-post'),
    ).not.toBeNull();
    fill('swarm-post', 'retained-draft-sentinel');
    await tick();
    button('Post').click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('[role="dialog"] [role="alert"]').textContent,
      ).toContain('post-failed-sentinel'),
    );
    expect(document.getElementById('swarm-post').value).toBe(
      'retained-draft-sentinel',
    );
    fail = false;
    button('Post').click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).toBeNull(),
    );
  });

  it('shows canonical context and resumes only the selected inactive participant', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation((name, args) =>
      name === 'swarms.get'
        ? Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              participants: swarm.participants.map((peer) => ({
                ...peer,
                state: peer.id === 'prt-a' ? 'running' : 'failed',
                run_active: peer.id === 'prt-a',
              })),
            },
          })
        : original(name, args),
    );
    bridge.readHistory.mockImplementation((_swarm, participant) =>
      Promise.resolve({
        messages: [],
        context_usage: {
          tokens: participant === 'prt-a' ? 120 : 850,
          estimated: participant !== 'prt-a',
        },
        session_usage: { input_tokens: 99000 },
      }),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Beta')).toBeDefined());
    button('Beta').click();
    await vi.waitFor(() => expect(button('Resume participant')).toBeDefined());
    expect(document.querySelector('.context-usage').textContent).toContain(
      '~850',
    );
    button('Resume participant').click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'swarms.resume',
        expect.objectContaining({ swarm_id: 'swr-a', participant_id: 'prt-b' }),
      ),
    );
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.context-usage').textContent).toContain(
        '120 / 128,000',
      ),
    );
    expect(button('Resume participant')).toBeUndefined();
  });

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
    await unmount(mounted);
    mounted = null;
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
    button('New profile').click();
    await tick();
    flushSync();
    await choose('swarm-model-0', 'demo/model');
    fill('swarm-directory', 'C:/work');
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

  it('shows a connection failure if the host never initializes the page', async () => {
    vi.useFakeTimers();
    const { bridge } = createBridge();
    mounted = mount(SwarmPage, {
      target: document.body,
      props: { bridgeClient: bridge },
    });
    flushSync();
    await vi.advanceTimersByTimeAsync(10_000);
    flushSync();
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(button('Refresh').disabled).toBe(false);
    bridge.show();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(button('New profile')).toBeDefined();
  });

  it('loads retained lists without requesting the profile catalog', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    expect(button('New profile')).toBeDefined();
    expect(document.body.textContent).toContain('Retained Swarms');
    expect(operation).toHaveBeenCalledWith('profiles.list', { limit: 100 });
    expect(operation.mock.calls.some(([name]) => name === 'catalog')).toBe(
      false,
    );
    bridge.show();
    await tick();
    expect(
      operation.mock.calls.filter(([name]) => name === 'profiles.list'),
    ).toHaveLength(1);
    button('New profile').click();
    await tick();
    flushSync();
    expect(
      operation.mock.calls.filter(([name]) => name === 'catalog'),
    ).toHaveLength(1);
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('keeps the overview usable when the profile catalog fails and retries on request', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    operation.mockRejectedValueOnce(new Error('catalog-unavailable-test'));
    button('New profile').click();
    await tick();
    flushSync();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'catalog-unavailable-test',
    );
    expect(button('New profile').disabled).toBe(false);
    button('New profile').click();
    await tick();
    flushSync();
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('saves a new profile through the bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('New profile').click();
    await tick();
    flushSync();
    fill('swarm-profile-name', 'New profile');
    await choose('swarm-model-0', 'demo/model');
    await choose('swarm-effort-0', 'high');
    fill('swarm-directory', 'C:/work');
    await tick();
    button('Save profile').click();
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
      button('New profile').click();
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
      button('Save profile').click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'profiles.save',
        expect.objectContaining({
          profile: expect.objectContaining({ instructions: expected }),
        }),
      );
      await unmount(mounted);
      mounted = null;
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
    button('New profile').click();
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
    button('New profile').click();
    await tick();
    flushSync();
    button('Communication').click();
    await tick();
    button('Save profile').click();
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
    button('Save profile').click();
    await tick();
    await tick();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')).not.toBeNull(),
    );
    expect(document.getElementById('swarm-profile-name').value).toBe('Retry');
    expect(button('Save profile').disabled).toBe(false);
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
    button('New profile').click();
    await tick();
    flushSync();
    const input = document.getElementById('swarm-profile-name');
    fill('swarm-profile-name', 'Draft survives');
    await tick();
    bridge.invalidate();
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.getElementById('swarm-profile-name')).toBe(input);
    expect(input.value).toBe('Draft survives');
    expect(document.querySelector('nav[aria-label="Profiles"]')).not.toBeNull();
    expect(button('Refresh')).toBeUndefined();
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
  });

  it('opens participant Activity directly from the Board and detaches its Run when leaving', async () => {
    const { bridge } = createBridge();
    const previousRun = swarm.participants[0].lifecycle_run_id;
    swarm.participants[0].lifecycle_run_id = 'run-live-test';
    bridge.subscribeRun.mockResolvedValue({
      subscription_id: 'subscription-live-test',
    });
    try {
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.participant-pane')).not.toBeNull(),
      );
      button('Alpha').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.history')).not.toBeNull(),
      );
      expect(
        document.querySelector('[role="tab"][aria-selected="true"]')
          .textContent,
      ).toContain('Activity');
      expect(bridge.subscribeRun).toHaveBeenCalledWith(
        'swr-a',
        'run-live-test',
      );
      bridge.emitRun('subscription-live-test', {
        type: 'model_step_usage',
        run_id: 'run-live-test',
        sequence: 1,
        payload: { context_usage: { tokens: 2468, estimated: true } },
      });
      await tick();
      expect(document.querySelector('.context-usage').textContent).toContain(
        '~2,468',
      );
      button('New Swarm').click();
      await tick();
      expect(bridge.unsubscribeRun).toHaveBeenCalledWith(
        'subscription-live-test',
      );
      expect(document.querySelector('.history')).toBeNull();
    } finally {
      swarm.participants[0].lifecycle_run_id = previousRun;
    }
  });

  it('reconciles canonical final output even when completion arrives before the subscription reply', async () => {
    const { bridge } = createBridge();
    const previousRun = swarm.participants[0].lifecycle_run_id;
    swarm.participants[0].lifecycle_run_id = 'run-final-test';
    bridge.readHistory.mockResolvedValueOnce({
      messages: [],
      status: 'running',
    });
    bridge.readHistory.mockResolvedValue({
      messages: [
        {
          id: 'msg-final-test',
          role: 'assistant',
          content: 'final-output-sentinel',
          timestamp: '2026-09-08T09:00:00+00:00',
        },
      ],
      status: 'completed',
    });
    bridge.subscribeRun.mockImplementation(() => {
      bridge.emitRun('subscription-final-test', {
        type: 'run_completed',
        run_id: 'run-final-test',
        sequence: 1,
      });
      return Promise.resolve({ subscription_id: 'subscription-final-test' });
    });
    try {
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.participant-pane')).not.toBeNull(),
      );
      button('Alpha').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.history')?.textContent).toContain(
          'final-output-sentinel',
        ),
      );
      expect(bridge.readHistory).toHaveBeenCalledTimes(2);
      expect(document.querySelectorAll('.history article')).toHaveLength(1);
    } finally {
      swarm.participants[0].lifecycle_run_id = previousRun;
    }
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

  it('posts a Board reply with explicit public recipients', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    flushSync();
    button('Write post').click();
    await tick();
    const textarea = document.querySelector('textarea');
    textarea.value = 'Finding';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    const options = document.querySelectorAll('.post-options input');
    options[0].value = 'pst-parent';
    options[0].dispatchEvent(new Event('input', { bubbles: true }));
    options[1].value = 'prt-a, prt-b';
    options[1].dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    button('Post').click();
    await tick();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'board.post',
      expect.objectContaining({
        swarm_id: 'swr-a',
        discussion_id: 'dsc-main',
        text: 'Finding',
        reply_to: 'pst-parent',
        recipients: ['prt-a', 'prt-b'],
        request_id: expect.any(String),
      }),
    );
  });

  it('changes Board discussion without treating a human read as a mutation', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    await new Promise((resolve) => setTimeout(resolve));
    const discussion = document.querySelector('#swarm-discussion');
    discussion.value = 'dsc-findings';
    discussion.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-findings',
      limit: 100,
    });
    expect(operation).not.toHaveBeenCalledWith('board.post', expect.anything());
  });

  it('renders canonical per-participant Model usage and tool-call totals', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    button('Usage').click();
    await tick();
    expect(document.body.textContent).toContain('Measured tokens');
    expect(document.body.textContent).toContain('Tool Calls');
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('demo/model');
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
      participant_id: 'prt-a',
    });
  });

  it('shows newest Board posts first and appends earlier pages below them', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const posts = (ids) =>
      ids.map((id) => ({
        id: `post-${id}`,
        text: `message-${id}`,
        sender_id: 'prt-a',
        created_at: '2026-09-08T09:00:00+00:00',
      }));
    operation.mockImplementation((name, args) =>
      name === 'board.read'
        ? Promise.resolve(
            args.cursor
              ? { entries: posts([1, 2]), cursor: null, has_more: false }
              : {
                  entries: posts([3, 4]),
                  cursor: 'older-page',
                  has_more: true,
                },
          )
        : original(name, args),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelectorAll('.board li')).toHaveLength(2),
    );
    const messages = () =>
      [...document.querySelectorAll('.board li p')].map((el) =>
        el.textContent.trim(),
      );
    expect(messages()).toEqual(['message-4', 'message-3']);
    button('Load earlier messages').click();
    await vi.waitFor(() =>
      expect(messages()).toEqual([
        'message-4',
        'message-3',
        'message-2',
        'message-1',
      ]),
    );
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: swarm.id,
      discussion_id: swarm.main_discussion_id,
      limit: 100,
      cursor: 'older-page',
    });
    expect(button('Load earlier messages')).toBeUndefined();
  });

  it.each([
    [0, '0'],
    [999, '999'],
    [1000, '1 k'],
    [12523, '12.5 k'],
    [1000000, '1 mio'],
    [11512523, '11.5 mio'],
    [1250000000, '1.3 mrd'],
  ])('formats Usage counts consistently for %s', async (value, expected) => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation(async (name, args) => {
      const result = await original(name, args);
      if (name === 'swarms.usage') {
        const counts = {
          measured_input_tokens: value,
          measured_output_tokens: 0,
          estimated_input_tokens: 0,
          estimated_output_tokens: value,
        };
        Object.assign(result.usage.usage.totals, counts);
        Object.assign(result.usage.usage.models[0], counts, { runs: value });
        result.usage.tools.total_calls = value;
      }
      return result;
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    expect(
      [...document.querySelectorAll('.usage-summary dd')].map((el) =>
        el.textContent.trim(),
      ),
    ).toEqual([expected, expected, expected]);
    for (const row of document.querySelectorAll('.table-wrap tbody tr')) {
      expect(
        [...row.querySelectorAll('td')]
          .slice(2)
          .map((el) => el.textContent.trim()),
      ).toEqual([expected, expected, expected]);
    }
  });

  it('renders canonical UsageSection totals and participant Model rows', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    expect(document.body.textContent).toContain('50');
    expect(document.body.textContent).toContain('15');
    expect(document.body.textContent).toContain('4');
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('demo/model');
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
      participant_id: 'prt-a',
    });
  });

  it('opens Activity links through the extension bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Alpha').click();
    await tick();
    button('Activity').click();
    await tick();
    const link = document.querySelector('.history a[href]');
    expect(link).not.toBeNull();
    link.click();
    await tick();
    expect(operation).toHaveBeenCalledWith('link.open', {
      url: 'https://example.test/',
    });
  });

  it('offers explicit Resume beside Stop when a participant is waiting', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(button('Stop')).toBeDefined();
    button('Resume').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.resume',
      expect.objectContaining({ swarm_id: 'swr-a' }),
    );
  });

  it.each([
    ['cancelled', 'cancelled'],
    ['interrupted', 'interrupted'],
  ])(
    'offers Resume for an unfinished %s Swarm participant',
    async (swarmState, participantState) => {
      const { bridge, operation } = createBridge();
      const originalGet = operation.getMockImplementation();
      operation.mockImplementation((name, arguments_) => {
        if (name === 'swarms.get') {
          return Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              state: swarmState,
              participants: [
                {
                  ...structuredClone(swarm.participants[0]),
                  state: participantState,
                },
              ],
            },
          });
        }
        return originalGet(name, arguments_);
      });
      await render(bridge);
      button('Investigate').click();
      await new Promise((resolve) => setTimeout(resolve));
      expect(button('Resume')).toBeDefined();
      button('Resume').click();
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'swarms.resume',
        expect.objectContaining({ swarm_id: 'swr-a' }),
      );
    },
  );

  it('does not autosave delivery changes and stops only on an explicit click', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    flushSync();
    button('Change communication settings').click();
    flushSync();
    const select = document.querySelector('.communication select');
    select.value = 'pull';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).not.toHaveBeenCalledWith(
      'swarms.settings',
      expect.anything(),
    );
    expect(document.body.textContent).toContain('Proposed changes');
    button('Apply changes').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.settings',
      expect.objectContaining({
        swarm_id: 'swr-a',
        expected_revision: 3,
        delivery: expect.objectContaining({
          main: expect.objectContaining({ mode: 'pull' }),
        }),
      }),
    );
    button('Stop').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.stop',
      expect.objectContaining({
        swarm_id: 'swr-a',
        request_id: expect.any(String),
      }),
    );
  });
});
