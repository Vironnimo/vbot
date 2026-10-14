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
  const base = createChatRpcMock(options);
  let stored = { ...(options.overrides ?? {}) };
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
    it.each([
      ...['model-first', 'effort-first'].flatMap((order) =>
        ['model', 'effort', null].map((failed) => ({ order, failed })),
      ),
      { order: 'model-first', failed: 'effort', refreshFails: true },
    ])(
      'reconciles independent fields with $order replies, $failed failure and refresh failure $refreshFails across a scoped invalidation',
      async ({ order, failed, refreshFails }) => {
        const writes = {
          model: Promise.withResolvers(),
          effort: Promise.withResolvers(),
        };
        const overlapping = Promise.withResolvers();
        const replacement = Promise.withResolvers();
        let stored = { model: SONNET };
        const row = (overrides) => ({
          session: {
            id: 'session-1',
            working_project_id: 'vbot',
            agent_overrides: structuredClone(overrides),
          },
        });
        let phase = 'initial';
        let replacementReads = 0;
        getSessionMock.mockImplementation(() => {
          if (phase === 'initial') return Promise.resolve(row(stored));
          if (phase === 'overlap') return overlapping.promise;
          replacementReads += 1;
          return replacement.promise;
        });
        const replies = {};
        const base = settingsRpcMock();
        rpcMock.mockImplementation((method, params) => {
          if (method !== 'session.set_agent_overrides') {
            return base(method, params);
          }
          const field = Object.hasOwn(params.agent_overrides, 'model')
            ? 'model'
            : 'effort';
          if (field !== failed) {
            stored = { ...stored, ...params.agent_overrides };
            replies[field] = { agent_overrides: structuredClone(stored) };
          }
          return writes[field].promise;
        });
        const props = reactiveProps({ projects: PROJECTS });
        await chat.mountChat(props);
        await waitForCondition(() => readOnlyProject() !== null);
        expect(pickerText('Model')).toBe(SONNET_NAME);

        await choose('Model', R1);
        await choose('Thinking effort', 'high');
        expect(rpcCalls('session.set_agent_overrides')).toHaveLength(2);
        expect(pickerText('Model')).toBe(R1);
        expect(pickerText('Thinking effort')).toBe('high');

        phase = 'overlap';
        const readsBeforeInvalidation = getSessionMock.mock.calls.length;
        props.sessionInvalidations = [
          { id: 1, scope: { agent_id: 'alpha', session_id: 'session-1' } },
        ];
        await settle();
        expect(getSessionMock.mock.calls.length).toBeGreaterThan(
          readsBeforeInvalidation,
        );

        const complete = async (field) => {
          if (field === failed) {
            writes[field].reject(new Error('override-failure-sentinel'));
          } else {
            writes[field].resolve(replies[field]);
          }
          await settle();
        };
        const first = order === 'model-first' ? 'model' : 'effort';
        const second = first === 'model' ? 'effort' : 'model';
        await complete(first);
        // Settling one field must neither drop the other pending choice nor
        // hide an independent success behind a newer request's sequence.
        expect(pickerText('Model')).toBe(
          first === 'model' && failed === 'model' ? SONNET_NAME : R1,
        );
        expect(pickerText('Thinking effort')).toBe(
          first === 'effort' && failed === 'effort'
            ? 'Provider default'
            : 'high',
        );
        expect(replacementReads).toBe(0);

        phase = 'replacement';
        await complete(second);
        expect(replacementReads).toBeGreaterThan(0);
        const expectedModel = failed === 'model' ? SONNET_NAME : R1;
        const expectedEffort =
          failed === 'effort' ? 'Provider default' : 'high';
        // The successful RPC's field remains correct before a refresh answers,
        // even when its independent sibling failed.
        expect(pickerText('Model')).toBe(expectedModel);
        expect(pickerText('Thinking effort')).toBe(expectedEffort);
        const error =
          testChatStateRefs[0].sessions['alpha::session-1'].actionError;
        if (failed) expect(error).toContain('override-failure-sentinel');
        else expect(error).toBe('');

        if (refreshFails) {
          replacement.reject(new Error('refresh-failure-sentinel'));
        } else {
          replacement.resolve(row(stored));
        }
        await settle();
        // An overlapping read that arrives last cannot restore the old Model.
        overlapping.resolve(row({ model: SONNET }));
        await settle();
        expect(pickerText('Model')).toBe(expectedModel);
        expect(pickerText('Thinking effort')).toBe(expectedEffort);
        expect(readOnlyProject().textContent.trim()).toBe('vBot');
      },
    );

    it('uses a final authoritative row when same-field server commits arrive in reverse request order', async () => {
      const older = Promise.withResolvers();
      const newer = Promise.withResolvers();
      const replacement = Promise.withResolvers();
      let stored = { model: SONNET };
      let refresh = false;
      let refreshStarted = false;
      getSessionMock.mockImplementation(() => {
        if (refresh) {
          refreshStarted = true;
          return replacement.promise;
        }
        return Promise.resolve({
          session: { working_project_id: 'vbot', agent_overrides: stored },
        });
      });
      const base = settingsRpcMock();
      let count = 0;
      rpcMock.mockImplementation((method, params) => {
        if (method !== 'session.set_agent_overrides')
          return base(method, params);
        return (++count === 1 ? older : newer).promise;
      });
      await chat.mountChat({ projects: PROJECTS });
      await waitForCondition(() => readOnlyProject() !== null);
      await choose('Model', R1);
      await choose('Model', SONNET);
      refresh = true;

      stored = {};
      newer.resolve({ agent_overrides: stored });
      await settle();
      expect(pickerText('Model')).toBe(SONNET_NAME);
      expect(refreshStarted).toBe(false);
      stored = { model: R1 };
      older.resolve({ agent_overrides: stored });
      await settle();
      expect(refreshStarted).toBe(true);
      replacement.resolve({
        session: { working_project_id: 'vbot', agent_overrides: stored },
      });
      await settle();
      expect(pickerText('Model')).toBe(R1);
    });

    it.each(
      ['older-first', 'newer-first'].flatMap((order) =>
        ['success', 'error'].map((outcome) => ({ order, outcome })),
      ),
    )(
      'keeps same-field ownership with $order replies and newer $outcome',
      async ({ order, outcome }) => {
        const older = Promise.withResolvers();
        const newer = Promise.withResolvers();
        const replacement = Promise.withResolvers();
        let stored = { model: SONNET };
        let readAfterWrites = false;
        getSessionMock.mockImplementation(() =>
          readAfterWrites
            ? replacement.promise
            : Promise.resolve({
                session: {
                  working_project_id: 'vbot',
                  agent_overrides: structuredClone(stored),
                },
              }),
        );
        const base = settingsRpcMock();
        let count = 0;
        rpcMock.mockImplementation((method, params) => {
          if (method !== 'session.set_agent_overrides') {
            return base(method, params);
          }
          count += 1;
          if (count === 1) {
            stored = { model: R1 };
            return older.promise;
          }
          expect(params.agent_overrides).toEqual({ model: null });
          if (outcome === 'success') stored = {};
          return newer.promise;
        });
        await chat.mountChat({ projects: PROJECTS });
        await waitForCondition(() => readOnlyProject() !== null);
        await choose('Model', R1);
        // Returning to the original/default value is still a newer edit.
        await choose('Model', SONNET);
        expect(pickerText('Model')).toBe(SONNET_NAME);
        readAfterWrites = true;

        const completeOlder = async () => {
          older.resolve({ agent_overrides: { model: R1 } });
          await settle();
        };
        const completeNewer = async () => {
          if (outcome === 'success') newer.resolve({ agent_overrides: {} });
          else newer.reject(new Error('newer-override-failure-sentinel'));
          await settle();
        };
        if (order === 'older-first') {
          await completeOlder();
          expect(pickerText('Model')).toBe(SONNET_NAME);
          await completeNewer();
        } else {
          await completeNewer();
          await completeOlder();
        }
        const expected = outcome === 'success' ? SONNET_NAME : R1;
        expect(pickerText('Model')).toBe(expected);
        replacement.resolve({
          session: { working_project_id: 'vbot', agent_overrides: stored },
        });
        await settle();
        expect(pickerText('Model')).toBe(expected);
      },
    );

    it.each(
      ['same', 'other'].flatMap((session) =>
        ['success', 'error'].map((outcome) => ({ session, outcome })),
      ),
    )(
      'loads the $session Session around an override write ending in $outcome',
      async ({ session, outcome }) => {
        const writing = Promise.withResolvers();
        const reading = Promise.withResolvers();
        const sameSession = session === 'same';
        const readingId = sameSession ? 'session-1' : 'session-2';
        const original = {
          session: {
            id: 'session-1',
            working_project_id: 'vbot',
            agent_overrides: { model: SONNET },
          },
        };
        const loaded = {
          session: {
            id: readingId,
            working_project_id: sameSession ? 'vbot' : 'docs',
            agent_overrides: sameSession
              ? { model: outcome === 'success' ? MINI : SONNET }
              : { model: R1, thinking_effort: 'max' },
          },
        };
        let readReady = false;
        getSessionMock.mockImplementation((_agentId, sessionId) =>
          sessionId !== readingId
            ? Promise.resolve(original)
            : readReady
              ? Promise.resolve(loaded)
              : reading.promise,
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

        if (sameSession) {
          // The initial row predates this write and must be discarded. A
          // failed write still needs a fresh read to recover the Project.
          reading.resolve(original);
          await settle();
          expect(readOnlyProject()).toBeNull();
        } else {
          props.pendingSessionNavigation = {
            agentId: 'alpha',
            sessionId: 'session-2',
            requestId: 1,
          };
          await waitForCondition(() =>
            getSessionMock.mock.calls.some(([, id]) => id === readingId),
          );
        }
        readReady = true;
        if (outcome === 'success')
          writing.resolve({ agent_overrides: { model: MINI } });
        else writing.reject(new Error('override-failed-sentinel'));
        await settle();
        if (!sameSession) reading.resolve(loaded);
        const expectedModel = sameSession
          ? outcome === 'success'
            ? MINI
            : SONNET_NAME
          : R1;
        await waitForCondition(
          () => pickerText('Model') === expectedModel && readOnlyProject(),
        );
        expect(readOnlyProject().textContent.trim()).toBe(
          sameSession ? 'vBot' : 'Docs',
        );
        if (!sameSession) expect(pickerText('Thinking effort')).toBe('max');
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
