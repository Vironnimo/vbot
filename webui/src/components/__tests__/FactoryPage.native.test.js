// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  createBridge,
  render,
  settle,
  button,
  fill,
  choose,
  calls,
  t,
  tick,
  flushSync,
} from './FactoryPage.support.js';

function nativeBridge({ established = true } = {}) {
  const fixture = createBridge([]);
  fixture.state.workflows = [];
  const data = {
    factories: established
      ? [
          {
            id: 'fac-one',
            project_id: 'project-a',
            name: 'Test Factory',
            leader_member_id: 'mem-leader',
            remit: 'Test remit',
            state: 'active',
            capacity: 2,
            config: {},
            limits: {},
            revision: 1,
          },
        ]
      : [],
    members: established
      ? [
          {
            id: 'mem-leader',
            factory_id: 'fac-one',
            agent_id: 'planner',
            role: 'Leader',
            instructions: 'Lead',
            config: {},
            capacity: 1,
            state: 'active',
            revision: 1,
          },
          {
            id: 'mem-worker',
            factory_id: 'fac-one',
            agent_id: 'drafter',
            role: 'Builder',
            instructions: 'Build',
            config: {},
            capacity: 1,
            state: 'active',
            revision: 1,
          },
        ]
      : [],
    tasks: [],
    topics: [],
    posts: [],
  };
  const initialOperation = fixture.operation.getMockImplementation();
  fixture.operation.mockImplementation((name, args) => {
    if (args?.limit != null && (args.limit < 1 || args.limit > 100))
      throw Object.assign(new Error('Test schema range'), {
        code: 'invalid_request',
      });
    return initialOperation(name, args);
  });
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const find = (rows, id) => rows.find((item) => item.id === id);
  const put = (rows, row) => {
    rows.push(row);
    return clone(row);
  };
  fixture.state.handlers['factory.list'] = () => ({
    factories: clone(data.factories),
  });
  fixture.state.handlers['factory.get'] = ({ id }) => ({
    factory: clone(find(data.factories, id)),
  });
  fixture.state.handlers['factory.create'] = (args) => ({
    factory: put(data.factories, {
      id: 'fac-one',
      project_id: args.project_id,
      leader_member_id: 'mem-leader',
      name: 'Created Factory',
      remit: args.remit,
      state: 'active',
      capacity: 2,
      config: {},
      limits: {},
      revision: 1,
    }),
  });
  fixture.state.handlers['member.list'] = () => ({
    members: clone(data.members),
  });
  for (const op of ['member.add', 'member.create'])
    fixture.state.handlers[op] = (args) => ({
      member: put(data.members, {
        ...args,
        id: 'mem-added',
        state: 'active',
        config: {},
        capacity: 1,
        revision: 1,
      }),
    });
  for (const resource of ['factory', 'member'])
    fixture.state.handlers[`${resource}.update`] = ({
      id,
      expected_revision,
      ...args
    }) => {
      const rows = resource === 'factory' ? data.factories : data.members;
      const record = find(rows, id);
      if (record.revision !== expected_revision)
        throw Object.assign(new Error('Test conflict'), {
          code: 'revision_conflict',
        });
      Object.assign(record, args, { revision: record.revision + 1 });
      return { [resource]: clone(record) };
    };
  fixture.state.handlers['task.list'] = () => ({ tasks: clone(data.tasks) });
  fixture.state.handlers['task.get'] = ({ id }) => ({
    task: clone(find(data.tasks, id)),
    children: clone(data.tasks.filter((item) => item.parent_id === id)),
    events: [],
    results: [],
    topics: clone(data.topics.filter((item) => item.task_id === id)),
    sessions: [],
  });
  fixture.state.handlers['task.create'] = (args) => ({
    task: put(data.tasks, {
      ...args,
      id: `task-${data.tasks.length + 1}`,
      state: args.assignee_id ? 'ready' : 'backlog',
      revision: 1,
    }),
  });
  fixture.state.handlers['task.update'] = ({
    id,
    expected_revision,
    ...args
  }) => {
    const task = find(data.tasks, id);
    if (expected_revision !== task.revision)
      throw Object.assign(new Error('test conflict'), {
        code: 'revision_conflict',
      });
    Object.assign(task, args, { revision: task.revision + 1 });
    return { task: clone(task) };
  };
  for (const [op, state] of [
    ['pause', 'paused'],
    ['resume', 'ready'],
    ['cancel', 'cancelled'],
    ['reopen', 'ready'],
    ['complete', 'done'],
  ])
    fixture.state.handlers[`task.${op}`] = ({ id, ...args }) => {
      const task = find(data.tasks, id);
      Object.assign(
        task,
        { state, revision: task.revision + 1 },
        op === 'complete'
          ? { result: { outcome: args.outcome, evidence: args.evidence } }
          : {},
      );
      return { task: clone(task) };
    };
  fixture.state.handlers['task.assign'] = ({ id, assignee_id }) => {
    const task = find(data.tasks, id);
    Object.assign(task, { assignee_id, revision: task.revision + 1 });
    return { task: clone(task) };
  };
  fixture.state.handlers['topic.list'] = () => ({ topics: clone(data.topics) });
  fixture.state.handlers['topic.get'] = ({ id }) => ({
    topic: clone(find(data.topics, id)),
    posts: clone(data.posts),
  });
  fixture.state.handlers['topic.create'] = (args) => ({
    topic: put(data.topics, {
      ...args,
      id: 'top-one',
      participants: args.member_ids,
      state: 'open',
      revision: 1,
    }),
  });
  fixture.state.handlers['topic.invite'] = (args) => {
    const topic = find(data.topics, args.id);
    topic.participants = [
      ...new Set([...topic.participants, ...args.member_ids]),
    ];
    topic.revision++;
    return { topic: clone(topic) };
  };
  fixture.state.handlers['topic.post'] = (args) => ({
    post: put(data.posts, {
      id: 'post-one',
      text: args.text,
      author: 'user',
      sequence: 1,
    }),
  });
  return { ...fixture, data };
}
const modal = () => document.querySelector('[role="dialog"]');
const saveModal = async (label) => {
  button(label, modal()).click();
  await settle();
  await settle();
};
const taskRecord = (id, title, extra = {}) => ({
  id,
  factory_id: 'fac-one',
  title,
  objective: 'Test objective',
  conditions: 'Test conditions',
  state: 'ready',
  assignee_id: 'mem-leader',
  parent_id: '',
  dependencies: [],
  priority: 0,
  limits: {},
  reviewer_id: '',
  user_approval: false,
  workflow_id: '',
  revision: 1,
  ...extra,
});

describe('Factory native work', () => {
  it('selects an active Factory on the default route and isolates native invalidations', async () => {
    const { bridge, operation } = nativeBridge();
    await render(bridge, '');
    await settle();
    expect(button(t('factory.native.newTask'))?.disabled).toBe(false);
    const count = (name) => calls(operation, name).length;
    const boards = count('board.get'),
      workflows = count('workflow.list');
    bridge.invalidate({ resource: 'tasks', ids: ['task-new'], revision: 2 });
    await settle();
    expect(count('board.get')).toBe(boards);
    expect(count('workflow.list')).toBe(workflows);
    const factories = count('factory.list');
    bridge.invalidate({ resource: 'cards', ids: ['card-new'], revision: 2 });
    await settle();
    expect(count('factory.list')).toBe(factories);
    bridge.invalidate({ resource: 'page_badges', ids: ['board'], revision: 2 });
    await settle();
    expect(count('factory.list')).toBe(factories);
    expect(count('board.get')).toBe(boards + 1);
    await choose('native-project', 'Project B');
    await settle();
    expect(button(t('factory.native.setup'))?.disabled).toBe(false);
    expect(button(t('factory.native.newTask'))).toBeUndefined();
  });

  it('resumes the Leader topic, sends inline and keeps a different Project separate', async () => {
    const { bridge, state, operation } = nativeBridge();
    const topic = { id: 'top-current', title: 'Startup time', state: 'open' };
    const posts = [
      { id: 'one', author: 'member:mem-leader', text: 'Existing answer' },
    ];
    state.handlers['conversation.get'] = ({ project_id }) =>
      project_id === 'project-a'
        ? { topic, posts: [...posts] }
        : { topic: null, posts: [] };
    state.handlers['conversation.send'] = ({ text }) => {
      posts.push({ id: 'two', author: 'user', text });
      return { topic, posts: [...posts] };
    };
    state.handlers['work.list'] = ({ project_id }) => ({
      work:
        project_id === 'project-a'
          ? [
              {
                kind: 'card',
                id: 'scheduled',
                target: 'scheduled',
                title: 'Review PR 42',
                attention: true,
              },
              {
                kind: 'task',
                id: 'task-a',
                target: 'task-a',
                title: 'Startup time',
                state: 'working',
              },
            ]
          : [],
      next_after: '',
    });
    await render(bridge, '');
    await settle();
    const log = () => document.querySelector('[role="log"]');
    expect(log().textContent).toContain('Existing answer');
    expect(document.querySelector('.project-overview').textContent).toContain(
      'Review PR 42',
    );
    const input = document.querySelector('.project-overview textarea');
    input.value = 'Continue here';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await settle();
    expect(calls(operation, 'conversation.send').at(-1)[1]).toMatchObject({
      project_id: 'project-a',
      topic_id: topic.id,
      text: 'Continue here',
      request_id: expect.any(String),
    });
    expect(log().textContent).toContain('Continue here');
    await choose('native-project', 'Project B');
    await settle();
    expect(log().textContent).not.toContain('Continue here');
    expect(calls(operation, 'conversation.get').at(-1)[1]).toEqual({
      project_id: 'project-b',
    });
  });

  it('repairs unknown native destinations and deleted record routes', async () => {
    const { bridge, state } = nativeBridge();
    await render(bridge, '/factory/fac-one/typo');
    await settle();
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
    for (const resource of ['task', 'topic', 'factory']) {
      state.handlers[`${resource}.get`] = () => {
        throw Object.assign(new Error('test-owned-missing'), {
          code: `${resource}_not_found`,
        });
      };
      await bridge.goTo(`/${resource}/missing`);
      await settle();
      expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
    }
  });

  it('establishes a permanent team with an existing Leader and creates an assigned task with no Workflows', async () => {
    const { bridge, operation, data } = nativeBridge({ established: false });
    await render(bridge, '');
    await choose('native-project', 'Project A');
    button(t('factory.native.setup')).click();
    await tick();
    await choose('native-leader', 'Planner');
    fill('native-setup-remit', 'Build the test product');
    await saveModal(t('factory.native.setup'));
    expect(calls(operation, 'factory.create').at(-1)[1]).toMatchObject({
      project_id: 'project-a',
      leader_agent_id: 'planner',
      remit: 'Build the test product',
    });
    data.members.push({
      id: 'mem-leader',
      factory_id: 'fac-one',
      agent_id: 'planner',
      role: 'Leader',
      state: 'active',
      revision: 1,
    });
    bridge.invalidate({ resource: 'members' });
    await settle();
    button(t('factory.native.newTask')).click();
    await tick();
    fill('native-create-title', 'Ship feature');
    fill('native-create-objective', 'Implement the requested behavior');
    fill('native-create-conditions', 'Observable result');
    await choose('native-owner', 'Leader');
    await saveModal(t('factory.native.newTask'));
    expect(calls(operation, 'task.create').at(-1)[1]).toMatchObject({
      factory_id: 'fac-one',
      title: 'Ship feature',
      assignee_id: 'mem-leader',
      workflow_id: '',
    });
    expect(calls(operation, 'workflow.save')).toHaveLength(0);
    expect(document.getElementById('native-title').value).toBe('Ship feature');
  });

  it('creates a new Leader with the Model selected through search', async () => {
    const { bridge, operation } = nativeBridge({ established: false });
    await render(bridge, '');
    await choose('native-project', 'Project A');
    button(t('factory.native.setup')).click();
    await tick();
    for (const id of ['native-leader', 'native-setup-remit']) {
      expect(
        document.querySelector(`label[for="${id}"] button[data-help-text]`),
      ).not.toBeNull();
    }
    fill('native-leader-name', 'Coordinator');

    document.getElementById('native-new-model').click();
    await tick();
    const search = document.querySelector('input[role="combobox"]');
    expect(search).not.toBeNull();
    search.value = 'reviewer';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    const options = document.querySelectorAll('[role="option"]');
    expect(options).toHaveLength(1);
    options[0].click();
    await tick();
    await saveModal(t('factory.native.setup'));

    expect(calls(operation, 'factory.create').at(-1)[1]).toMatchObject({
      project_id: 'project-a',
      leader_name: 'Coordinator',
      leader_model: 'demo/reviewer',
      request_id: expect.any(String),
    });
  });

  it('creates a child with its own owner and dependency, pauses, reassigns and records completion evidence', async () => {
    const { bridge, operation, data } = nativeBridge();
    data.tasks.push(
      taskRecord('task-parent', 'Parent'),
      taskRecord('task-pre', 'Prerequisite', { state: 'done' }),
    );
    await render(bridge, '/task/task-parent');
    button(t('factory.native.newTask')).click();
    await settle();
    fill('native-create-title', 'Child');
    fill('native-create-objective', 'Implement child');
    await choose('native-owner', 'Builder');
    const checkbox = [...modal().querySelectorAll('[role="checkbox"]')].find(
      (input) => input.textContent.includes('Prerequisite'),
    );
    checkbox.click();
    await tick();
    await saveModal(t('factory.native.newTask'));
    expect(calls(operation, 'task.create').at(-1)[1]).toMatchObject({
      parent_id: 'task-parent',
      assignee_id: 'mem-worker',
      dependencies: ['task-pre'],
    });
    button(t('factory.native.pause')).click();
    await settle();
    expect(calls(operation, 'task.pause').at(-1)[1]).toEqual({
      id: 'task-3',
      expected_revision: 1,
    });
    expect(button(t('factory.native.complete'))).toBeUndefined();
    button(t('factory.native.reassign')).click();
    await settle();
    await choose('native-owner', 'Leader');
    await saveModal(t('factory.native.reassign'));
    expect(calls(operation, 'task.assign').at(-1)[1].assignee_id).toBe(
      'mem-leader',
    );
    button(t('factory.native.resume')).click();
    await settle();
    button(t('factory.native.complete')).click();
    await settle();
    fill('native-outcome', 'Integrated');
    fill('native-evidence', 'Test-owned verification');
    await saveModal(t('factory.native.complete'));
    expect(calls(operation, 'task.complete').at(-1)[1]).toMatchObject({
      id: 'task-3',
      outcome: 'Integrated',
      evidence: 'Test-owned verification',
    });
    expect(data.tasks.at(-1).state).toBe('done');
  });
  it('adds a real Agent, opens an independent topic, directly invites a peer and lets the user post', async () => {
    const { bridge, operation } = nativeBridge();
    await render(bridge, '/factory/fac-one/team');
    button(t('factory.native.addMember')).click();
    await tick();
    await choose('native-agent', 'Planner');
    fill('native-member-role', 'Reviewer');
    await saveModal(t('factory.native.addMember'));
    expect(calls(operation, 'member.add').at(-1)[1]).toMatchObject({
      factory_id: 'fac-one',
      agent_id: 'planner',
      role: 'Reviewer',
    });
    button(t('factory.native.discussions')).click();
    await settle();
    await settle();
    button(t('factory.native.newTopic')).click();
    await tick();
    fill('native-topic-title', 'Design question');
    await saveModal(t('factory.native.newTopic'));
    expect(calls(operation, 'topic.create').at(-1)[1].task_id).toBe('');
    button(t('factory.native.invite')).click();
    await tick();
    [...modal().querySelectorAll('[role="checkbox"]')]
      .find((input) => input.textContent.includes('Builder'))
      .click();
    fill('native-message', 'Please advise');
    await saveModal(t('factory.native.invite'));
    expect(calls(operation, 'topic.invite').at(-1)[1]).toMatchObject({
      member_ids: ['mem-worker'],
      text: 'Please advise',
    });
    button(t('factory.native.post')).click();
    await tick();
    fill('native-message', 'Human contribution');
    await saveModal(t('factory.native.post'));
    expect(calls(operation, 'topic.post').at(-1)[1]).toMatchObject({
      id: 'top-one',
      text: 'Human contribution',
    });
    expect(document.querySelector('.native-posts').textContent).toContain(
      'Human contribution',
    );
  });
  it('creates a durable specialist with a stable retry receipt and saves Factory-local configuration without changing its Identity', async () => {
    const { bridge, operation, state, data } = nativeBridge();
    await render(bridge, '/factory/fac-one/team');
    button(t('factory.native.addMember')).click();
    await tick();
    modal().querySelector('[role="switch"]').click();
    await tick();
    fill('native-agent-name', 'Specialist');
    fill('native-member-role', 'Implementer');
    const create = state.handlers['member.create'];
    let failed = true;
    state.handlers['member.create'] = (args) => {
      if (failed) {
        failed = false;
        throw new Error('Test-owned transient failure');
      }
      return create(args);
    };
    await saveModal(t('factory.native.addMember'));
    expect(modal()).not.toBeNull();
    const receipt = calls(operation, 'member.create').at(-1)[1].request_id;
    await saveModal(t('factory.native.addMember'));
    expect(calls(operation, 'member.create').at(-1)[1]).toMatchObject({
      name: 'Specialist',
      request_id: receipt,
    });
    const panel = [
      ...document.querySelectorAll('.native-members section'),
    ].find((item) => item.textContent.includes('Implementer'));
    button(t('common.edit'), panel).click();
    await settle();
    fill('native-instructions', 'Local member remit');
    expect(await bridge.autosave.flush()).toBe(true);
    expect(calls(operation, 'member.update').at(-1)[1]).toMatchObject({
      instructions: 'Local member remit',
    });
    expect(calls(operation, 'member.update').at(-1)[1]).not.toHaveProperty(
      'identity_config',
    );
    await choose('native-model', 'Demo worker');
    expect(await bridge.autosave.flush()).toBe(true);
    await choose('native-model', t('factory.native.inheritedModel'));
    expect(await bridge.autosave.flush()).toBe(true);
    expect(calls(operation, 'member.update').at(-1)[1].clear_fields).toEqual([
      'model',
    ]);
    button(t('factory.native.settings')).click();
    await settle();
    fill('native-allowed-models', 'demo/worker, demo/reviewer');
    fill('native-allowed-skills', 'one, two');
    expect(await bridge.autosave.flush()).toBe(true);
    expect(data.factories[0].config).toMatchObject({
      allowed_models: ['demo/worker', 'demo/reviewer'],
      allowed_skills: ['one', 'two'],
    });
    fill('native-allowed-skills', '');
    expect(await bridge.autosave.flush()).toBe(true);
    expect(calls(operation, 'factory.update').at(-1)[1].clear_fields).toEqual([
      'allowed_skills',
    ]);
    button(t('factory.native.pause')).click();
    await settle();
    expect(data.factories[0].state).toBe('paused');
    expect(button(t('factory.native.pause'))).toBeUndefined();
    button(t('factory.native.resume')).click();
    await settle();
    expect(data.factories[0].state).toBe('active');
    state.handlers['factory.archive'] = () => {
      Object.assign(data.factories[0], {
        state: 'archived',
        revision: data.factories[0].revision + 1,
      });
      return { factory: JSON.parse(JSON.stringify(data.factories[0])) };
    };
    button(t('factory.native.archive')).click();
    await settle();
    expect(data.factories[0].state).toBe('archived');
    expect(button(t('factory.native.pause'))).toBeUndefined();
    expect(button(t('factory.native.resume'))).toBeUndefined();
    expect(button(t('factory.native.archive'))).toBeUndefined();
    expect(document.getElementById('native-remit')).not.toBeNull();
  });
  it('renders results and audit events with independent identities and distinct canonical Session controls', async () => {
    const { bridge, state, data, operation } = nativeBridge();
    data.tasks.push(taskRecord('task-one', 'Task', { result: {} }));
    const detail = state.handlers['task.get'];
    state.handlers['task.get'] = (args) => ({
      ...detail(args),
      events: [
        {
          id: 1,
          kind: 'task',
          event: 'created',
          actor: 'user',
          created_at: '2026-11-16T10:00:00Z',
          detail: {},
        },
      ],
      results: [
        {
          id: 1,
          kind: 'completed',
          outcome: 'Test-owned result',
          evidence: 'Test-owned evidence',
          comment: 'Test-owned required review changes',
          actor: 'member:mem-worker',
          created_at: '2026-11-16T10:00:00Z',
        },
      ],
      sessions: [
        {
          group_id: 'native-group',
          participant_id: 'mem-worker',
          session: {
            agent_id: 'drafter',
            session_id: 'ses-one',
            project_id: 'project-a',
          },
        },
        {
          group_id: 'native-group-epoch-two',
          participant_id: 'mem-worker',
          session: {
            agent_id: 'drafter',
            session_id: 'ses-two',
            project_id: 'project-a',
          },
        },
      ],
    });
    const firstPage = state.handlers['task.get'];
    state.handlers['task.get'] = (args) => {
      const response = firstPage(args);
      return {
        ...response,
        events: args.events_after
          ? [{ ...response.events[0], id: 2, event: 'updated' }]
          : response.events,
        results: args.results_after
          ? [
              {
                ...response.results[0],
                id: 2,
                evidence: 'Test-owned later evidence',
              },
            ]
          : response.results,
        events_after: args.events_after ? 0 : 1,
        results_after: args.results_after ? 0 : 1,
      };
    };
    await render(bridge, '/task/task-one');
    expect(
      document.querySelector('[data-native-results]').textContent,
    ).toContain('Test-owned evidence');
    expect(
      document.querySelector('[data-native-results]').textContent,
    ).toContain('Test-owned required review changes');
    vi.useFakeTimers();
    const draft = fill('native-objective', 'Draft survives history pagination');
    draft.focus();
    button(t('factory.native.moreResults')).click();
    await settle();
    expect(
      document.querySelector('[data-native-results]').textContent,
    ).toContain('Test-owned later evidence');
    expect(button(t('factory.native.moreResults'))).toBeUndefined();
    expect(button(t('factory.native.moreEvents'))).toBeDefined();
    button(t('factory.native.moreEvents')).click();
    await settle();
    expect(document.querySelectorAll('[data-native-events] li')).toHaveLength(
      2,
    );
    expect(document.getElementById('native-objective')).toBe(draft);
    data.tasks[0].revision = 2;
    bridge.invalidate({ resource: 'tasks' });
    await settle();
    expect(document.querySelectorAll('[data-native-results] li')).toHaveLength(
      2,
    );
    expect(document.querySelectorAll('[data-native-events] li')).toHaveLength(
      2,
    );
    expect(draft.value).toBe('Draft survives history pagination');
    expect(
      calls(operation, 'task.get').some(([, args]) => args.results_after === 1),
    ).toBe(true);
    expect(
      calls(operation, 'task.get').some(([, args]) => args.events_after === 1),
    ).toBe(true);
    expect(calls(operation, 'task.update')).toHaveLength(0);
    const sessionButtons = [...document.querySelectorAll('button')].filter(
      (node) => node.textContent.trim() === t('factory.native.openSession'),
    );
    expect(sessionButtons).toHaveLength(2);
    expect(document.querySelector('[data-native-events] time')).not.toBeNull();
    expect(document.querySelector('[data-native-events]').textContent).not.toBe(
      'task',
    );
    expect(
      [...document.querySelectorAll('h2')].some(
        (node) => node.textContent === t('factory.native.outcome'),
      ),
    ).toBe(false);
    button(t('factory.native.openSession')).click();
    await settle();
    expect(bridge.openSession).toHaveBeenCalledWith({
      agent_id: 'drafter',
      session_id: 'ses-one',
      project_id: 'project-a',
    });
    sessionButtons[1].click();
    await settle();
    expect(bridge.openSession).toHaveBeenLastCalledWith({
      agent_id: 'drafter',
      session_id: 'ses-two',
      project_id: 'project-a',
    });
    button(t('factory.native.readHistory')).click();
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledWith(
      'native-group',
      'mem-worker',
      { limit: 100, child_limit: 100 },
    );
  });
  it('retains an edited objective through invalidation, rebases its revision and flushes before leaving', async () => {
    const { bridge, operation, data } = nativeBridge();
    data.tasks.push(taskRecord('task-one', 'Task'));
    await render(bridge, '/task/task-one');
    vi.useFakeTimers();
    const field = fill('native-objective', 'Local draft');
    field.focus();
    data.tasks[0].priority = 4;
    data.tasks[0].revision = 2;
    bridge.invalidate({ resource: 'tasks' });
    await settle();
    expect(document.getElementById('native-objective')).toBe(field);
    expect(field.value).toBe('Local draft');
    button(t('factory.native.team')).click();
    await settle();
    expect(calls(operation, 'task.update').at(-1)[1]).toEqual({
      id: 'task-one',
      expected_revision: 2,
      objective: 'Local draft',
    });
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/factory/fac-one/team');
    flushSync();
  });
});
