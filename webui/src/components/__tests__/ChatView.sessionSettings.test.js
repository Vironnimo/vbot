// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  agentPill,
  createAgent,
  createChatRpcMock,
  findButtonByText,
  findNewSessionButton,
  flushSync,
  getSessionMock,
  handledCommand,
  listedSessions,
  projectChatProps,
  rpcCalls,
  rpcMock,
  runningRun,
  sendComposerMessage,
  sessionListButton,
  settle,
  setupChatViewTestSuite,
  showProjectMock,
  streamResponses,
  testChatStateRefs,
  waitForCondition,
} from './ChatView.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const SONNET = 'openrouter/anthropic/claude-sonnet-4';
const MINI = 'openrouter/openai/gpt-4o-mini';
const R1 = 'openrouter/deepseek/deepseek-r1';
// The row names a Model by its catalog name and falls back to its id.
const SONNET_NAME = 'Anthropic: Claude Sonnet 4';

function catalogModel(id, reasoning, name) {
  return {
    id,
    name,
    provider_id: 'openrouter',
    context_window: 200000,
    capabilities: { tools: true, reasoning },
  };
}

const MODELS = [
  catalogModel(
    SONNET,
    { supported: true, levels: ['low', 'medium', 'high'] },
    SONNET_NAME,
  ),
  catalogModel(MINI, { supported: false }),
  catalogModel(R1, {
    supported: true,
    mandatory: true,
    levels: ['high', 'max'],
  }),
];

const PROJECTS = [
  { project_id: 'vbot', display_name: 'vBot' },
  { project_id: 'docs', display_name: 'Docs' },
];

// The Chat RPC mock plus the Model catalog, `files.list`, and Session Agent
// overrides that merge each change like the server does.
function settingsRpcMock(options = {}) {
  const agents = options.agents ?? [createAgent()];
  const base = createChatRpcMock({ ...options, agents });
  let stored = { ...(options.overrides ?? {}) };
  return async (method, params) => {
    if (method === 'agent.update') {
      const agent = agents.find((entry) => entry.id === params.id);
      Object.assign(agent, params);
      return { ...agent };
    }
    if (method === 'chat.stream' && params.content?.startsWith('/model ')) {
      agents[0].model = params.content.slice(7);
    }
    if (method === 'model.list') return { models: MODELS };
    if (method === 'connection.list') return { connections: [] };
    if (method === 'files.list') return { files: ['notes.md'] };
    if (method === 'session.set_agent_overrides') {
      stored = Object.fromEntries(
        Object.entries({ ...stored, ...params.agent_overrides }).filter(
          ([, value]) => value !== null,
        ),
      );
      if (params.remember) {
        const agent = agents.find((entry) => entry.id === params.agent_id);
        if (agent) Object.assign(agent, params.agent_overrides);
        if (params.agent_overrides.thinking_effort === null)
          stored.thinking_effort_default = true;
        else if (params.agent_overrides.thinking_effort)
          delete stored.thinking_effort_default;
      }
      return {
        agent_id: params.agent_id,
        session_id: params.session_id,
        agent_overrides: stored,
        effective: {},
      };
    }
    return base(method, params);
  };
}

const footer = () => document.querySelector('.session-settings');
const picker = (name) =>
  footer()?.querySelector(`button[aria-label="${name}"]`) ?? null;
const pickerText = (name) => picker(name)?.textContent.trim() ?? '';
const readOnlyProject = () =>
  footer()?.querySelector('[role="group"][aria-label="Project"]') ?? null;
const listedOptions = () =>
  Array.from(document.querySelectorAll('[role="option"]'));
const optionLabel = (option) =>
  option.querySelector('[class*="option-label"]').textContent.trim();

async function openPicker(name) {
  await waitForCondition(() => picker(name)?.disabled === false);
  picker(name).click();
  flushSync();
  await waitForCondition(() => listedOptions().length > 0);
}

async function pickerOptions(name) {
  await openPicker(name);
  const labels = listedOptions().map(optionLabel);
  picker(name).click();
  flushSync();
  return labels;
}

async function choose(name, label) {
  await openPicker(name);
  listedOptions()
    .find((option) => optionLabel(option) === label)
    .click();
  flushSync();
  await settle();
}

describe('ChatView Session settings', () => {
  const chat = setupChatViewTestSuite();

  describe('draft', () => {
    // An Identity Agent without a current Session shows its draft.
    const draftAgent = (overrides = {}) =>
      createAgent({
        current_session_id: '',
        root_project_id: 'vbot',
        model: SONNET,
        thinking_effort: 'medium',
        ...overrides,
      });

    it.each([
      ['a chosen Project', 'Docs', 'docs', false],
      ['the Workspace', 'Workspace', null, false],
      ['a Project changed during submission', 'Docs', 'docs', true],
      ['the Workspace changed during submission', 'Workspace', null, true],
    ])(
      'creates its Session from the submitted choices for %s',
      async (_case, projectLabel, projectId, editWhilePending) => {
        const listing = Promise.withResolvers();
        const base = settingsRpcMock({
          agents: [draftAgent(), draftAgent({ id: 'beta', name: 'Beta' })],
          streamHandler: ({ content }) => ({
            ...runningRun('run-first'),
            session_id:
              content === 'Next message' ? 'created-next' : 'created-alpha',
          }),
        });
        rpcMock.mockImplementation((method, params) =>
          editWhilePending && method === 'files.list'
            ? listing.promise
            : base(method, params),
        );
        await chat.mountChat({ projects: PROJECTS }, { ready: null });
        await waitForCondition(() => pickerText('Project') === 'vBot');
        await waitForCondition(() => pickerText('Thinking effort') !== '');
        expect(pickerText('Model')).toBe(SONNET_NAME);
        expect(pickerText('Thinking effort')).toBe('medium');

        await choose('Project', projectLabel);
        await waitForCondition(() =>
          rpcCalls('chat.commands').some(
            (params) => params.working_project_id === projectId,
          ),
        );
        expect(rpcCalls('chat.commands').at(-1)).toEqual({
          agent_id: 'alpha',
          working_project_id: projectId,
        });
        await choose('Model', R1);
        await choose('Thinking effort', 'max');
        sendComposerMessage('Read @notes.md');

        if (editWhilePending) {
          await waitForCondition(() => rpcCalls('files.list').length > 0);
          expect(rpcCalls('chat.stream')).toEqual([]);
          await choose('Project', 'vBot');
          await choose('Model', SONNET);
          expect(pickerText('Thinking effort')).toBe('high');
          // Leaving and returning while the original lookup is pending must
          // not replace either its settings or the newer draft choices.
          agentPill('Beta').click();
          flushSync();
          agentPill('Alpha').click();
          flushSync();
          expect(pickerText('Project')).toBe('vBot');
          listing.resolve({ files: ['notes.md'] });
        }

        await waitForCondition(() => readOnlyProject() !== null);
        // The file mention is looked up in the chosen Project.
        expect(rpcCalls('files.list')).toContainEqual({
          agent_id: 'alpha',
          working_project_id: projectId,
        });
        expect(
          rpcCalls('files.list').filter(
            (params) => params.working_project_id !== projectId,
          ),
        ).toEqual([]);
        expect(rpcCalls('chat.stream')).toEqual([
          expect.objectContaining({
            agent_id: 'alpha',
            new_session: {
              working_project_id: projectId,
              agent_overrides: { model: R1, thinking_effort: 'max' },
            },
          }),
        ]);
        // The created Session keeps its Project and shows its overrides.
        expect(picker('Project')).toBeNull();
        expect(readOnlyProject().textContent.trim()).toBe(projectLabel);
        expect(pickerText('Model')).toBe(R1);
        expect(pickerText('Thinking effort')).toBe('max');
        expect(rpcCalls('session.set_agent_overrides')).toEqual([]);

        findNewSessionButton().click();
        flushSync();
        await waitForCondition(() => picker('Project') !== null);
        expect(pickerText('Project')).toBe(
          editWhilePending ? 'vBot' : projectLabel,
        );
        expect(pickerText('Model')).toBe(editWhilePending ? SONNET_NAME : R1);
        expect(pickerText('Thinking effort')).toBe(
          editWhilePending ? 'high' : 'max',
        );
        expect(rpcCalls('agent.update')).toContainEqual({
          id: 'alpha',
          root_project_id: projectId,
        });
        if (editWhilePending) {
          sendComposerMessage('Next message');
          await waitForCondition(() => rpcCalls('chat.stream').length === 2);
          expect(rpcCalls('chat.stream')[1].new_session).toEqual({
            working_project_id: 'vbot',
            agent_overrides: { model: SONNET, thinking_effort: 'high' },
          });
        }
      },
    );

    it('drops a held Model after /model changes the Agent Model', async () => {
      rpcMock.mockImplementation(
        settingsRpcMock({
          agents: [draftAgent()],
          streamHandler: streamResponses({
            [`/model ${R1}`]: handledCommand(`Model set to ${R1}`, {
              output: 'toast',
              data: { command: 'model', agent_id: 'alpha', model: R1 },
            }),
            'First message': {
              ...runningRun('run-first'),
              session_id: 'created-alpha',
            },
          }),
        }),
      );
      await chat.mountChat({ projects: PROJECTS }, { ready: null });
      await choose('Model', MINI);
      expect(pickerText('Model')).toBe(MINI);
      const agentReads = rpcCalls('agent.list').length;

      sendComposerMessage(`/model ${R1}`);
      await waitForCondition(() => pickerText('Model') === R1);
      await waitForCondition(() => rpcCalls('agent.list').length > agentReads);
      sendComposerMessage('First message');
      await waitForCondition(() => rpcCalls('chat.stream').length === 2);

      expect(rpcCalls('chat.stream')[1]).toEqual({
        agent_id: 'alpha',
        new_session: {
          working_project_id: 'vbot',
          agent_overrides: { model: R1, thinking_effort_default: true },
        },
        content: 'First message',
      });
    });
  });

  describe('existing Session', () => {
    it('refreshes the displayed Compaction Policy after a scoped change without reading History again', async () => {
      rpcMock.mockImplementation(settingsRpcMock());
      let effective = { enabled: true, strategy: 'summary_tail' };
      getSessionMock.mockImplementation(async () => ({
        session: {
          id: 'session-1',
          agent_overrides: {},
          working_project_id: null,
          compaction_policy_effective: structuredClone(effective),
        },
      }));
      const props = reactiveProps({ projects: PROJECTS });
      await chat.mountChat(props);
      const session = () => testChatStateRefs[0].sessions['alpha::session-1'];
      await waitForCondition(
        () => session()?.compactionPolicy?.enabled === true,
      );
      const historyReads = rpcCalls('chat.timeline').length;
      effective = { ...effective, enabled: false };
      props.sessionInvalidations = [
        { id: 1, scope: { agent_id: 'alpha', session_id: 'session-1' } },
      ];
      await waitForCondition(
        () => session()?.compactionPolicy?.enabled === false,
      );
      expect(rpcCalls('chat.timeline')).toHaveLength(historyReads);
    });

    it('keeps an older Session when another view changes the remembered start values', async () => {
      const agents = [createAgent({ model: SONNET, thinking_effort: 'high' })];
      rpcMock.mockImplementation(settingsRpcMock({ agents }));
      let stored = {};
      getSessionMock.mockImplementation(async () => ({
        session: {
          id: 'session-1',
          working_project_id: null,
          agent_overrides: { ...stored },
        },
      }));
      const props = reactiveProps({
        projects: PROJECTS,
        agentsRefreshToken: 0,
      });
      await chat.mountChat(props);
      await waitForCondition(() => pickerText('Thinking effort') === 'high');
      const reads = getSessionMock.mock.calls.length;
      // The server captures legacy Sessions before changing the Agent defaults.
      stored = { model: SONNET, thinking_effort: 'high' };
      agents[0] = { ...agents[0], model: MINI, thinking_effort: '' };
      props.agentsRefreshToken += 1;
      await waitForCondition(() => getSessionMock.mock.calls.length > reads);
      await waitForCondition(() => pickerText('Thinking effort') === 'high');
      expect(pickerText('Model')).toBe(SONNET_NAME);
      expect(rpcCalls('agent.update')).toEqual([]);
      expect(rpcCalls('session.set_agent_overrides')).toEqual([]);
    });

    it.each([false, true])(
      'serializes newer choices and retains a failed save for retry (%s)',
      async (fail) => {
        const first = Promise.withResolvers();
        let stored = { model: SONNET, thinking_effort: 'low' };
        getSessionMock.mockImplementation(async () => ({
          session: {
            id: 'session-1',
            working_project_id: 'vbot',
            agent_overrides: { ...stored },
          },
        }));
        const base = settingsRpcMock();
        let writes = 0;
        rpcMock.mockImplementation(async (method, params) => {
          if (method !== 'session.set_agent_overrides')
            return base(method, params);
          writes += 1;
          if (writes === 1) await first.promise;
          stored = { ...stored, ...params.agent_overrides };
          return { agent_overrides: { ...stored } };
        });
        await chat.mountChat({ projects: PROJECTS });
        await waitForCondition(() => pickerText('Thinking effort') === 'low');
        await choose('Thinking effort', 'medium');
        await choose('Thinking effort', 'high');
        expect(writes).toBe(1);
        expect(pickerText('Thinking effort')).toBe('high');
        if (fail) first.reject(new Error('save-failure-sentinel'));
        else first.resolve();
        await waitForCondition(() => writes === 2);
        expect(stored.thinking_effort).toBe('high');
        expect(rpcCalls('session.set_agent_overrides').at(-1)).toEqual({
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { thinking_effort: 'high' },
          remember: true,
        });
        expect(pickerText('Thinking effort')).toBe('high');
      },
    );

    it('keeps a failed choice until the user retries and waits before sending', async () => {
      const write = Promise.withResolvers();
      const base = settingsRpcMock();
      let fail = true;
      rpcMock.mockImplementation((method, params) => {
        if (method === 'session.set_agent_overrides' && fail)
          return write.promise;
        return base(method, params);
      });
      await chat.mountChat({ projects: PROJECTS });
      await choose('Model', MINI);
      sendComposerMessage('Wait for the setting');
      await settle();
      expect(rpcCalls('chat.stream')).toEqual([]);
      write.reject(new Error('save-failure-sentinel'));
      await waitForCondition(() => footer()?.textContent.includes('Save'));
      expect(pickerText('Model')).toBe(MINI);
      fail = false;
      const retry = Array.from(footer().querySelectorAll('button')).find(
        (button) => button.textContent.trim() === 'Save',
      );
      retry.click();
      await waitForCondition(
        () => rpcCalls('session.set_agent_overrides').length === 2,
      );
      sendComposerMessage('Saved now');
      await waitForCondition(() => rpcCalls('chat.stream').length === 1);
    });

    it.each(['session', 'agent', 'draft'])(
      'keeps a failed save in its own conversation while another %s saves and sends',
      async (destination) => {
        const agents = [
          createAgent(),
          createAgent({ id: 'beta', name: 'Beta' }),
        ];
        const base = settingsRpcMock({
          agents,
          sessionMessages: { 'session-2': [] },
          streamHandler: (params) => ({
            ...runningRun('run-other'),
            session_id: params.session_id || 'created-other',
          }),
        });
        rpcMock.mockImplementation((method, params) => {
          if (
            method === 'session.set_agent_overrides' &&
            params.agent_id === 'alpha' &&
            params.session_id === 'session-1'
          )
            return Promise.reject(new Error('save-failure-sentinel'));
          return base(method, params);
        });
        listedSessions({ id: 'session-2', title: 'Other conversation' });
        await chat.mountChat({
          projects: PROJECTS,
          preserveSessionSelection: true,
        });
        await choose('Model', MINI);
        await waitForCondition(() => footer()?.textContent.includes('Save'));
        expect(rpcCalls('session.set_agent_overrides')).toHaveLength(1);

        if (destination === 'agent') agentPill('Beta').click();
        else if (destination === 'draft') findNewSessionButton().click();
        else {
          sessionListButton().click();
          await waitForCondition(() =>
            Boolean(findButtonByText('Other conversation')),
          );
          findButtonByText('Other conversation').click();
        }
        flushSync();
        await waitForCondition(
          () => footer() && !footer().textContent.includes('Save'),
        );
        await choose('Model', R1);
        sendComposerMessage('Send from the other conversation');
        await waitForCondition(() => rpcCalls('chat.stream').length === 1);
        expect(
          rpcCalls('session.set_agent_overrides').filter(
            (params) =>
              params.agent_id === 'alpha' && params.session_id === 'session-1',
          ),
        ).toHaveLength(1);
        expect(
          testChatStateRefs[0].sessions['alpha::session-1'].actionError,
        ).toContain('save-failure-sentinel');
      },
    );

    it('writes Model and effort changes at once and offers the levels of the effective Model', async () => {
      let stored = { model: SONNET };
      getSessionMock.mockImplementation(async () => ({
        session: {
          id: 'session-1',
          working_project_id: 'vbot',
          agent_overrides: structuredClone(stored),
        },
      }));
      // The Agent's Model pins a Connection, which the label leaves out.
      const base = settingsRpcMock({
        agents: [createAgent({ model: `${R1}::default` })],
        overrides: stored,
      });
      rpcMock.mockImplementation(async (method, params) => {
        const result = await base(method, params);
        if (method === 'session.set_agent_overrides') {
          stored = result.agent_overrides;
        }
        return result;
      });
      await chat.mountChat({ projects: PROJECTS });
      await waitForCondition(() => pickerText('Model') === SONNET_NAME);
      expect(readOnlyProject().textContent.trim()).toBe('vBot');
      await waitForCondition(() => picker('Thinking effort') !== null);
      expect(await pickerOptions('Thinking effort')).toEqual([
        'Provider default',
        'none',
        'low',
        'medium',
        'high',
      ]);

      await choose('Thinking effort', 'medium');
      expect(pickerText('Thinking effort')).toBe('medium');
      // Returning to the Agent's Model snaps medium to its nearest level and
      // saves that effort in the same override write.
      await choose('Model', R1);
      await waitForCondition(
        () => rpcCalls('session.set_agent_overrides').length === 2,
      );
      expect(pickerText('Model')).toBe(R1);
      expect(pickerText('Thinking effort')).toBe('high');
      expect(await pickerOptions('Thinking effort')).toEqual([
        'Provider default',
        'high',
        'max',
      ]);
      // A Model without reasoning hides the effort and drops the Session's.
      await choose('Model', MINI);
      await waitForCondition(
        () => rpcCalls('session.set_agent_overrides').length === 3,
      );

      expect(rpcCalls('session.set_agent_overrides')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { thinking_effort: 'medium' },
          remember: true,
        },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { model: R1, thinking_effort: 'high' },
          remember: true,
        },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { model: MINI, thinking_effort: null },
          remember: true,
        },
      ]);
      expect(pickerText('Model')).toBe(MINI);
      expect(picker('Thinking effort')).toBeNull();
    });
  });

  // A Project missing from the list is unknown until the list has been read.
  it.each([
    ['the default Project of a draft', { current_session_id: '' }, null],
    [
      'the Project of an existing Session',
      {},
      { id: 'session-1', working_project_id: 'gone' },
    ],
  ])(
    'warns that %s is no longer registered only once the Project list says so',
    async (_case, agent, session) => {
      getSessionMock.mockResolvedValue({ session });
      rpcMock.mockImplementation(
        settingsRpcMock({
          agents: [createAgent({ root_project_id: 'gone', ...agent })],
        }),
      );
      const props = reactiveProps({ projects: [], projectsLoaded: false });
      await chat.mountChat(props, { ready: null });
      const projectField = () =>
        footer()?.querySelector(
          '.session-settings__start .session-settings__field',
        ) ?? null;
      const warned = () =>
        projectField().classList.contains('session-settings__field--warning');

      await waitForCondition(
        () => projectField()?.textContent.trim() === 'gone',
      );
      await settle();
      expect(warned()).toBe(false);
      expect(picker('Project') !== null).toBe(session === null);

      props.projects = PROJECTS;
      props.projectsLoaded = true;
      await waitForCondition(warned);
      expect(projectField().textContent.trim()).toBe('gone');
    },
  );

  it('shows the Project of a Project team Agent read-only', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          {
            agent_id: 'builder',
            display_name: 'Builder',
            effective: {
              model: { value: SONNET, source: 'project' },
              thinking_effort: { value: 'low', source: 'project' },
            },
          },
        ],
        report: { clean: true, findings: [] },
      },
    });
    rpcMock.mockImplementation(settingsRpcMock());
    await chat.mountChat(projectChatProps({ projects: PROJECTS }), {
      ready: null,
    });

    await waitForCondition(
      () => readOnlyProject()?.textContent.trim() === 'vBot',
    );
    expect(picker('Project')).toBeNull();
    expect(
      readOnlyProject().closest('.session-settings__field--warning'),
    ).toBeNull();
    await waitForCondition(() => pickerText('Thinking effort') === 'low');
    expect(pickerText('Model')).toBe(SONNET_NAME);
  });
});
