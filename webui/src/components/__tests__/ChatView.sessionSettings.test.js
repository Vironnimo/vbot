// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  agentPill,
  createAgent,
  createChatRpcMock,
  findNewSessionButton,
  flushSync,
  getSessionMock,
  handledCommand,
  projectChatProps,
  rpcCalls,
  rpcMock,
  runningRun,
  sendComposerMessage,
  settle,
  setupChatViewTestSuite,
  showProjectMock,
  streamResponses,
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
  const base = createChatRpcMock(options);
  let stored = {};
  return async (method, params) => {
    if (method === 'model.list') return { models: MODELS };
    if (method === 'connection.list') return { connections: [] };
    if (method === 'files.list') return { files: ['notes.md'] };
    if (method === 'session.set_agent_overrides') {
      stored = Object.fromEntries(
        Object.entries({ ...stored, ...params.agent_overrides }).filter(
          ([, value]) => value !== null,
        ),
      );
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
          await choose('Thinking effort', 'high');
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
        expect(pickerText('Project')).toBe('vBot');
        expect(pickerText('Model')).toBe(SONNET_NAME);
        expect(pickerText('Thinking effort')).toBe(
          editWhilePending ? 'high' : 'medium',
        );
        if (editWhilePending) {
          sendComposerMessage('Next message');
          await waitForCondition(() => rpcCalls('chat.stream').length === 2);
          expect(rpcCalls('chat.stream')[1].new_session).toEqual({
            working_project_id: 'vbot',
            agent_overrides: { thinking_effort: 'high' },
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
      await waitForCondition(() => pickerText('Model') === SONNET_NAME);
      await waitForCondition(() => rpcCalls('agent.list').length > agentReads);
      sendComposerMessage('First message');
      await waitForCondition(() => rpcCalls('chat.stream').length === 2);

      expect(rpcCalls('chat.stream')[1]).toEqual({
        agent_id: 'alpha',
        new_session: {},
        content: 'First message',
      });
    });
  });

  describe('existing Session', () => {
    it.each(['success', 'error'])(
      'loads another Session while an earlier override write finishes with %s',
      async (outcome) => {
        const writing = Promise.withResolvers();
        const reading = Promise.withResolvers();
        getSessionMock.mockImplementation((_agentId, sessionId) =>
          sessionId === 'session-2'
            ? reading.promise
            : Promise.resolve({
                session: {
                  id: 'session-1',
                  working_project_id: 'vbot',
                  agent_overrides: { model: SONNET },
                },
              }),
        );
        const base = settingsRpcMock();
        rpcMock.mockImplementation((method, params) =>
          method === 'session.set_agent_overrides'
            ? writing.promise
            : base(method, params),
        );
        const props = reactiveProps({ projects: PROJECTS });
        await chat.mountChat(props);
        await waitForCondition(() => pickerText('Model') === SONNET_NAME);
        await choose('Model', MINI);
        expect(rpcCalls('session.set_agent_overrides')).toHaveLength(1);

        props.pendingSessionNavigation = {
          agentId: 'alpha',
          sessionId: 'session-2',
          requestId: 1,
        };
        await waitForCondition(() =>
          getSessionMock.mock.calls.some(([, id]) => id === 'session-2'),
        );
        if (outcome === 'success')
          writing.resolve({ agent_overrides: { model: MINI } });
        else writing.reject(new Error('override-failed-sentinel'));
        await settle();
        reading.resolve({
          session: {
            id: 'session-2',
            working_project_id: 'docs',
            agent_overrides: { model: R1, thinking_effort: 'max' },
          },
        });
        await waitForCondition(() => pickerText('Model') === R1);
        expect(readOnlyProject().textContent.trim()).toBe('Docs');
        expect(pickerText('Thinking effort')).toBe('max');
      },
    );

    it('writes Model and effort changes at once and offers the levels of the effective Model', async () => {
      getSessionMock.mockResolvedValue({
        session: {
          id: 'session-1',
          working_project_id: 'vbot',
          agent_overrides: { model: SONNET },
        },
      });
      // The Agent's Model pins a Connection, which the label leaves out.
      rpcMock.mockImplementation(
        settingsRpcMock({ agents: [createAgent({ model: `${R1}::default` })] }),
      );
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

      await choose('Thinking effort', 'high');
      expect(pickerText('Thinking effort')).toBe('high');
      // The Agent default names the Agent's Model; the Session's high is
      // offered by it too, so only the Model returns to the Agent's.
      await choose('Model', R1);
      await waitForCondition(
        () => rpcCalls('session.set_agent_overrides').length === 2,
      );
      expect(pickerText('Model')).toBe(R1);
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
          agent_overrides: { thinking_effort: 'high' },
        },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { model: null },
        },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          agent_overrides: { model: MINI, thinking_effort: null },
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
