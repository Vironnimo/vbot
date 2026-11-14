// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  createBridge,
  card,
  startedCard,
  render,
  settle,
  button,
  fill,
  choose,
  calls,
  actionButtons,
  stepButtons,
  implementWorkflow,
  triageWorkflow,
  workflow,
  step,
  t,
  tick,
  flushSync,
} from './FactoryPage.support.js';
import { assistantRun, outputItem } from './timelineItems.support.js';

const planImplement = workflow('planimpl', 'Plan, then implement', [
  step('plan', 'Plan'),
  step('planreview', 'Plan review', { after: ['plan'], returns_to: 'plan' }),
  step('implement', 'Implement', { after: ['planreview'] }),
  step('review', 'Review', { after: ['implement'], returns_to: 'implement' }),
]);
const done = { status: 'done' };
const output = (id, stepId, name, kind, extra = {}) => ({
  id,
  step: stepId,
  step_name: name,
  kind,
  pass: 1,
  participant_id: stepId,
  created_at: '2026-11-07T09:00:00Z',
  ...extra,
});
const editorText = () =>
  document.querySelector('.factory-workflow-editor').textContent;
const stepIds = () =>
  [...document.querySelectorAll('.factory-diagram [data-step-id]')].map(
    (node) => node.dataset.stepId,
  );
const stepStatuses = () =>
  [...document.querySelectorAll('[data-step-status]')].map(
    (node) => node.dataset.stepStatus,
  );
const dialog = () => document.querySelector('[role="dialog"]');
const waitingStep = (id) =>
  document.querySelector(`[data-waiting-step="${id}"]`);
async function selectStep(id) {
  document
    .querySelector(`.factory-diagram [data-step-id="${id}"] button`)
    .click();
  await tick();
}

describe('Factory Board', () => {
  it('groups every state, shows each card Workflow with its active steps and keeps discarded cards behind their filter', async () => {
    const states = [
      'proposal',
      'queued',
      'working',
      'waiting',
      'done',
      'discarded',
    ];
    const { bridge, state } = createBridge(
      states.map((value, index) =>
        value === 'working'
          ? startedCard(
              value,
              value,
              '',
              implementWorkflow,
              { implement: done, review: { status: 'running' } },
              { position: index + 1 },
            )
          : card(value, value, { position: index + 1 }),
      ),
    );
    await render(bridge);
    const ids = (column) =>
      [
        ...document.querySelectorAll(
          `[data-column="${column}"] [data-card-id]`,
        ),
      ].map((node) => node.dataset.cardId);
    expect(ids('proposals')).toEqual(['proposal']);
    expect(ids('backlog')).toEqual(['queued']);
    expect(ids('progress')).toEqual(['working']);
    expect(ids('waiting')).toEqual(['waiting']);
    expect(ids('done')).toEqual(['done']);
    const stepOf = (id) =>
      document.querySelector(`[data-card-id="${id}"] [data-card-step]`)
        .textContent;
    expect(stepOf('working')).toBe('Implement·Review');
    expect(stepOf('queued')).toBe('Implement');
    expect(document.querySelector('.factory-wip').textContent).toBe(
      t('factory.running', { running: 1, limit: 2 }),
    );
    button(t('factory.showDiscarded')).click();
    await tick();
    expect(ids('done')).toEqual(['done', 'discarded']);
    state.cards = state.cards.filter((entry) => entry.state !== 'proposal');
    bridge.invalidate({ resource: 'cards', ids: ['proposal'], revision: 2 });
    await settle();
    expect(document.querySelector('[data-column="proposals"]')).toBeNull();
  });

  it('invites to create a Workflow while neither Workflows nor cards exist', async () => {
    const { bridge, state } = createBridge([]);
    state.workflows = [];
    await render(bridge);
    button(t('factory.createFirstWorkflow')).click();
    await settle();
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/workflows');
  });

  it('filters Projects, reorders the backlog and creates cards for a chosen Workflow, keeping a Workflow Project fixed', async () => {
    const { bridge, operation } = createBridge([
      card('a'),
      card('b', 'queued', { position: 2 }),
      card('other', 'done', { project_id: 'project-b', position: 3 }),
    ]);
    await render(bridge);
    button(t('factory.moveDown', { title: 'Title a' })).click();
    await settle();
    expect(calls(operation, 'card.move').at(-1)[1]).toEqual({
      id: 'a',
      position: 2,
    });
    expect(
      [
        ...document.querySelectorAll('[data-column="backlog"] [data-card-id]'),
      ].map((node) => node.dataset.cardId),
    ).toEqual(['b', 'a']);
    await choose('factory-project-filter', 'Project A');
    await settle();
    expect(calls(operation, 'board.get').at(-1)[1]).toEqual({
      project_id: 'project-a',
    });
    expect(document.querySelector('[data-card-id="other"]')).toBeNull();
    const scrollport = document.querySelector('.factory-scrollport');
    scrollport.scrollTop = 170;
    scrollport.dispatchEvent(new Event('scroll'));
    button(t('factory.newCard')).click();
    await tick();
    expect(dialog()).not.toBeNull();
    const project = () => document.getElementById('factory-create-project');
    expect(
      document.getElementById('factory-create-workflow').textContent,
    ).toContain('Implement');
    expect(project().textContent).toContain('Project A');
    expect(project().disabled).toBe(true);
    expect(dialog().textContent).toContain(t('factory.workflowFixesProject'));
    await choose('factory-create-workflow', 'Triage');
    expect(project().disabled).toBe(false);
    await choose('factory-create-project', 'Project B');
    fill('factory-create-title', 'Operator card');
    fill('factory-create-description', 'test-owned objective');
    button(t('factory.createCard')).click();
    await settle();
    expect(calls(operation, 'card.create').at(-1)[1]).toEqual({
      workflow: 'triage',
      project_id: 'project-b',
      title: 'Operator card',
      description: 'test-owned objective',
    });
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/card/fct-new');
    expect(document.querySelector('.factory-detail')).not.toBeNull();
    expect(scrollport.scrollTop).toBe(0);
    await bridge.goTo('');
    await settle();
    expect(scrollport.scrollTop).toBe(170);
  });
});

describe('Factory card decisions', () => {
  const waitingOn = (reason, steps) =>
    startedCard('fct-a', 'waiting', reason, implementWorkflow, steps);
  it.each([
    ['proposal', card('fct-a', 'proposal'), ['approveProposal', 'discard']],
    ['queued', card('fct-a'), ['discard']],
    [
      'working',
      startedCard('fct-a', 'working', '', implementWorkflow, {
        implement: { status: 'running' },
      }),
      ['discard'],
    ],
    [
      'checkpoint',
      waitingOn('checkpoint', {
        implement: done,
        review: done,
        integrate: { status: 'approval' },
      }),
      ['sendBack', 'discard'],
      { integrate: ['acceptCheckpoint'] },
    ],
    [
      'finished',
      waitingOn('finished', { implement: done, review: done, integrate: done }),
      ['acceptDone', 'sendBack', 'discard'],
    ],
    [
      'no_agreement',
      waitingOn('no_agreement', {
        implement: done,
        review: { status: 'waiting', reason: 'no_agreement' },
      }),
      ['sendBack', 'discard'],
      { review: ['acceptAnyway'] },
    ],
    ...['blocked', 'run_failed', 'restart', 'budget_exhausted'].map(
      (reason) => [
        reason,
        waitingOn(reason, { implement: { status: 'waiting', reason } }),
        ['sendBack', 'discard'],
        { implement: [reason === 'blocked' ? 'answer' : 'retry'] },
      ],
    ),
    [
      'workflow_missing',
      card('fct-a', 'waiting', {
        waiting_reason: 'workflow_missing',
        note: 'test-owned blocker note',
      }),
      ['retry', 'discard'],
    ],
    ['done', card('fct-a', 'done'), ['delete']],
    ['discarded', card('fct-a', 'discarded'), ['delete']],
  ])(
    'offers only state-appropriate actions for %s',
    async (_name, initial, expected, expectedSteps = {}) => {
      const { bridge } = createBridge([initial]);
      await render(bridge, '/card/fct-a');
      const labels = {
        approveProposal: t('factory.approveProposal'),
        discard: t('factory.discard'),
        acceptCheckpoint: t('factory.acceptCheckpoint', { step: 'Integrate' }),
        acceptDone: t('factory.acceptDone'),
        acceptAnyway: t('factory.acceptAnyway'),
        sendBack: t('factory.sendBack'),
        answer: t('factory.answer'),
        retry: t('factory.retry'),
        delete: t('factory.delete'),
      };
      expect(actionButtons()).toEqual(expected.map((key) => labels[key]));
      expect(stepButtons()).toEqual(
        Object.fromEntries(
          Object.entries(expectedSteps).map(([id, keys]) => [
            id,
            keys.map((key) => labels[key]),
          ]),
        ),
      );
      if (initial.note)
        expect(
          document.querySelector('[data-card-blocked]').textContent,
        ).toContain('test-owned blocker note');
    },
  );

  it('keeps step results, provenance, related cards, cost and permissions reachable and accepts only the displayed step revision', async () => {
    const origin = {
      project_id: 'project-a',
      agent_id: 'builder',
      session_id: 'ses-source',
    };
    const permission = {
      run_id: 'run-w',
      project_id: 'project-a',
      agent_id: 'owned-worker',
      session_id: 'ses-worker',
    };
    const { bridge, state, operation } = createBridge([
      startedCard(
        'fct-a',
        'waiting',
        'checkpoint',
        implementWorkflow,
        { implement: done, review: done, integrate: { status: 'approval' } },
        {
          summary: 'test-owned summary',
          origin: 'trigger',
          origin_session: origin,
          origin_run: 'run-source',
        },
      ),
    ]);
    state.details['fct-a'] = {
      approvals: [permission],
      trigger: { event: 'push', repository: 'test-owned/repository' },
      children: [card('fct-child', 'working', { parent_id: 'fct-a' })],
      outputs: [
        output(1, 'review', 'Review', 'work', {
          summary: 'test-owned review result',
        }),
      ],
      events: [
        {
          id: 1,
          event: 'submitted',
          actor: 'review',
          at: '2026-11-07T09:00:00Z',
          detail: { state: 'waiting', previous_state: 'working' },
        },
      ],
    };
    await render(bridge, '/card/fct-a');
    expect(
      document.querySelector('[data-output-step="review"]').textContent,
    ).toContain('test-owned review result');
    expect(
      document.querySelector('[aria-labelledby="factory-timeline-heading"]')
        .textContent,
    ).toContain(t('factory.event.submitted'));
    expect(document.querySelector('[data-usage] dd').textContent).toBe('150');
    expect(document.querySelector('.factory-cost').textContent).not.toBe(
      t('factory.costUnavailable'),
    );
    expect(stepStatuses()).toEqual(['done', 'done', 'approval']);
    expect(
      document.querySelector('[data-trigger-event]').textContent,
    ).toContain('test-owned/repository');
    button(t('factory.provenance')).click();
    await tick();
    expect(bridge.openSession).toHaveBeenLastCalledWith(origin);
    button(t('factory.reviewPermission')).click();
    await tick();
    expect(bridge.openSession).toHaveBeenLastCalledWith({
      project_id: 'project-a',
      agent_id: 'owned-worker',
      session_id: 'ses-worker',
    });
    button(t('factory.acceptCheckpoint', { step: 'Integrate' })).click();
    await settle();
    expect(calls(operation, 'card.accept').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 1,
      step: 'integrate',
    });
    expect(document.querySelector('[data-state]').dataset.state).toBe(
      'working',
    );
    button('Title fct-child').click();
    await settle();
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/card/fct-child');
  });

  it('labels a user continuation waiting for WIP without reopening its reviewed objective', async () => {
    const { bridge, state } = createBridge([
      card('fct-a', 'queued', { queued_action: 'accept' }),
    ]);
    await render(bridge, '/card/fct-a');
    expect(
      document.querySelector('.factory-detail-badges').textContent,
    ).toContain(t('factory.queued.accept'));
    expect(document.getElementById('factory-card-title')).toBeNull();
    expect(actionButtons()).toEqual([t('factory.discard')]);
    state.cards[0].queued_action = 'send_back';
    state.cards[0].revision += 1;
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
    await settle();
    expect(
      document.querySelector('.factory-detail-badges').textContent,
    ).toContain(t('factory.queued.sendBack'));
    await bridge.goTo('');
    await settle();
    expect(
      document.querySelector('[data-column="backlog"] .factory-card-reason')
        .textContent,
    ).toBe(t('factory.queued.sendBack'));
  });

  it.each([
    [
      'blocked',
      'answer',
      { text: 'test-owned response', step: 'implement' },
      { implement: { status: 'waiting', reason: 'blocked', question: 'Q?' } },
    ],
    [
      'finished',
      'sendBack',
      { comment: 'test-owned response' },
      { implement: done, review: done, integrate: done },
    ],
  ])(
    'submits a %s response without replacing its draft on invalidation',
    async (reason, action, payload, steps) => {
      const { bridge, operation } = createBridge([
        startedCard('fct-a', 'waiting', reason, implementWorkflow, steps),
      ]);
      await render(bridge, '/card/fct-a');
      button(t(`factory.${action}`)).click();
      await tick();
      if (action === 'answer') expect(dialog().textContent).toContain('Q?');
      const input = fill('factory-response', 'test-owned response');
      input.focus();
      bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
      await settle();
      expect(document.getElementById('factory-response')).toBe(input);
      expect(input.value).toBe('test-owned response');
      button(t(`factory.${action}`), dialog()).click();
      await settle();
      expect(
        calls(
          operation,
          `card.${action === 'sendBack' ? 'send_back' : 'answer'}`,
        ).at(-1)[1],
      ).toEqual({ id: 'fct-a', ...payload });
      expect(dialog()).toBeNull();
    },
  );

  it('shows step progress and every result, and sends back to any chosen step', async () => {
    const { bridge, state, operation } = createBridge([
      startedCard('fct-a', 'waiting', 'no_agreement', planImplement, {
        plan: { status: 'done', passes: 2 },
        planreview: done,
        implement: done,
        review: { status: 'waiting', reason: 'no_agreement' },
      }),
    ]);
    state.details['fct-a'] = {
      outputs: [
        output(1, 'plan', 'Plan', 'work', { result: '# test-owned old plan' }),
        output(2, 'plan', 'Plan', 'work', {
          pass: 2,
          summary: 'test-owned plan summary',
          result: '# test-owned plan',
        }),
        output(3, 'planreview', 'Plan review', 'work', {
          summary: 'test-owned plan approved',
        }),
        output(4, 'implement', 'Implement', 'work', {
          summary: 'test-owned changes and checks',
        }),
        output(5, 'review', 'Review', 'changes', {
          findings: ['test-owned finding'],
          target: 'implement',
        }),
      ],
    };
    await render(bridge, '/card/fct-a');
    expect(stepStatuses()).toEqual(['done', 'done', 'done', 'waiting']);
    const plans = document.querySelectorAll('[data-output-step="plan"]');
    expect(plans[0].querySelector('details').open).toBe(false);
    expect(plans[1].querySelector('details').open).toBe(true);
    expect(plans[1].querySelector('h1').textContent).toBe('test-owned plan');
    expect(plans[1].querySelector('h4').textContent).toContain(
      t('factory.passNumber', { pass: 2 }),
    );
    const result = (id) => document.querySelector(`[data-output-step="${id}"]`);
    expect(result('implement').textContent).toContain(
      'test-owned changes and checks',
    );
    expect(result('review').textContent).toContain('test-owned finding');
    expect(result('review').textContent).toContain(
      t('factory.returnedTo', { step: 'Implement' }),
    );
    button(t('factory.sendBack')).click();
    await tick();
    expect(
      document.getElementById('factory-send-back-step').textContent,
    ).toContain(t('factory.sendBackDefault', { step: 'Implement' }));
    fill('factory-response', 'test-owned rework');
    await choose('factory-send-back-step', 'Plan review');
    button(t('factory.sendBack'), dialog()).click();
    await settle();
    expect(calls(operation, 'card.send_back').at(-1)[1]).toEqual({
      id: 'fct-a',
      comment: 'test-owned rework',
      step: 'planreview',
    });
    // A reset step no longer appears done, even if it preceded the waiting one.
    const steps = {
      plan: { status: 'done', passes: 2 },
      planreview: { status: 'running' },
      implement: { status: 'pending' },
      review: { status: 'pending' },
    };
    Object.assign(state.cards[0], { steps, revision: 3 });
    state.cards[0].runtime.steps = steps;
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 3 });
    await settle();
    expect(stepStatuses()).toEqual(['done', 'running', 'pending', 'pending']);
  });

  it('confirms destructive actions in navigation layers, deletes only final cards and repairs their route', async () => {
    const { bridge, operation, state } = createBridge([
      card('fct-a', 'waiting', { waiting_reason: 'no_agreement' }),
    ]);
    await render(bridge, '/card/fct-a');
    button(t('factory.discard')).click();
    await tick();
    bridge.back();
    await tick();
    expect(calls(operation, 'card.discard')).toHaveLength(0);
    button(t('factory.discard')).click();
    await tick();
    button(t('factory.discard'), dialog()).click();
    await settle();
    expect(calls(operation, 'card.discard').at(-1)[1]).toEqual({ id: 'fct-a' });
    button(t('factory.delete')).click();
    await tick();
    bridge.back();
    await tick();
    expect(dialog()).toBeNull();
    expect(calls(operation, 'card.delete')).toHaveLength(0);
    button(t('factory.delete')).click();
    await tick();
    state.handlers['card.delete'] = () =>
      Promise.reject(
        Object.assign(new Error('test-owned untranslated backend detail'), {
          code: 'card_not_final',
        }),
      );
    button(t('factory.delete'), dialog()).click();
    await settle();
    const alert = document.querySelector('[role="dialog"] [role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert.textContent).not.toContain(
      'test-owned untranslated backend detail',
    );
    delete state.handlers['card.delete'];
    button(t('factory.delete'), dialog()).click();
    await settle();
    expect(calls(operation, 'card.delete')).toHaveLength(2);
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
  });

  it('saves a waiting Card limits for a chosen step, then retries that step', async () => {
    const { bridge, state, operation } = createBridge([
      startedCard('fct-a', 'waiting', 'budget_exhausted', implementWorkflow, {
        implement: { status: 'waiting', reason: 'budget_exhausted' },
      }),
    ]);
    state.details['fct-a'] = {
      budget: {
        implement: {
          card: { limits: {}, usage: { tool_calls: 5 } },
          step: { limits: {}, usage: { tool_calls: 5 } },
        },
      },
    };
    await render(bridge, '/card/fct-a');
    expect(document.getElementById('factory-limit-step').textContent).toContain(
      'Implement',
    );
    fill('factory-card-limit-tokens', '1000');
    fill('factory-step-limit-tool_calls', '8');
    await tick();
    button(t('factory.limit.save')).click();
    await settle();
    expect(calls(operation, 'card.limits').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 1,
      card_limits: { tokens: 1000 },
      step: 'implement',
      step_limits: { tool_calls: 8 },
    });
    expect(state.cards[0].runtime.steps.implement.limits).toEqual({
      tool_calls: 8,
    });
    button(t('factory.retry'), waitingStep('implement')).click();
    await settle();
    expect(calls(operation, 'card.retry').at(-1)[1]).toEqual({
      id: 'fct-a',
      step: 'implement',
    });
  });
});

describe('Factory record editors', () => {
  it('debounces repeated backlog edits, preserves newer input through a slow save, and flushes before leaving', async () => {
    const { bridge, state, operation } = createBridge();
    await render(bridge, '/card/fct-a');
    vi.useFakeTimers();
    let finish;
    state.handlers['card.update'] = (args) =>
      new Promise((resolve) => {
        finish = () => {
          Object.assign(state.cards[0], { title: args.title, revision: 2 });
          resolve({ card: { ...state.cards[0] } });
        };
      });
    fill('factory-card-title', 'First');
    await vi.advanceTimersByTimeAsync(500);
    fill('factory-card-title', 'Second');
    await vi.advanceTimersByTimeAsync(500);
    expect(calls(operation, 'card.update')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(300);
    await tick();
    expect(calls(operation, 'card.update')).toHaveLength(1);
    const input = fill('factory-card-title', 'Third');
    input.focus();
    finish();
    await tick();
    flushSync();
    expect(document.getElementById('factory-card-title')).toBe(input);
    expect(input.value).toBe('Third');
    delete state.handlers['card.update'];
    button(t('factory.workflows')).click();
    await settle();
    expect(calls(operation, 'card.update').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 2,
      title: 'Third',
    });
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/workflows');
  });

  it('rebases concurrent title changes without overwriting them and retains an unrelated objective draft', async () => {
    const { bridge, state, operation } = createBridge();
    await render(bridge, '/card/fct-a');
    fill('factory-card-title', 'local title');
    fill('factory-card-description', 'local objective');
    state.cards[0].title = 'remote title';
    state.cards[0].revision = 2;
    expect(await bridge.autosave.flush()).toBe(true);
    await tick();
    expect(state.cards[0].title).toBe('remote title');
    expect(state.cards[0].description).toBe('local objective');
    expect(calls(operation, 'card.update').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 2,
      description: 'local objective',
    });
    expect(document.getElementById('factory-card-title').value).toBe(
      'remote title',
    );
  });

  it('autosaves Workflow limits and step Models, waits for number completion, retains failed drafts and leaves a Workflow deleted elsewhere', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, '/workflows/implement');
    fill('factory-workflow-limit-cost_usd', '2.5');
    await selectStep('implement');
    fill('factory-implement-limit-tool_calls', '50');
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    expect(state.workflows[0].limits).toEqual({ cost_usd: 2.5 });
    expect(state.workflows[0].steps[0].limits).toEqual({ tool_calls: 50 });
    expect(calls(operation, 'workflow.save').at(-1)[1]).toMatchObject({
      expected_revision: 1,
      workflow: { id: 'implement', project_id: 'project-a' },
    });
    await selectStep('review');
    expect(document.getElementById('factory-review-rounds').value).toBe('2');
    expect(editorText()).not.toContain(t('factory.sameModel'));
    await choose('factory-review-model', 'demo/worker');
    await bridge.autosave.flush();
    await tick();
    expect(editorText()).toContain(t('factory.sameModel'));
    expect(
      calls(operation, 'workflow.save').at(-1)[1].workflow.steps[1],
    ).toMatchObject({
      id: 'review',
      kind: 'agent',
      returns_to: 'implement',
      rounds: 2,
      source: 'team',
      agent: 'critic',
      model: 'demo/worker',
    });
    vi.useFakeTimers();
    let writes = calls(operation, 'workflow.save').length;
    const input = document.getElementById('factory-review-rounds');
    input.focus();
    fill('factory-review-rounds', '3');
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls(operation, 'workflow.save')).toHaveLength(writes);
    input.blur();
    await tick();
    await Promise.resolve();
    flushSync();
    expect(calls(operation, 'workflow.save')).toHaveLength(writes + 1);
    expect(state.workflows[0].steps[1].rounds).toBe(3);
    state.handlers['workflow.save'] = () =>
      Promise.reject(new Error('test-owned write failure'));
    fill('factory-review-task', 'test-owned changed task');
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
    await settle();
    expect(document.getElementById('factory-review-task').value).toBe(
      'test-owned changed task',
    );
    delete state.handlers['workflow.save'];
    expect(await bridge.autosave.flush()).toBe(true);
    expect(state.workflows[0].steps[1].task).toBe('test-owned changed task');
    writes = calls(operation, 'workflow.save').length;
    state.workflows = state.workflows.filter(
      (entry) => entry.id !== 'implement',
    );
    bridge.invalidate({ resource: 'workflows', ids: ['implement'] });
    await settle();
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/workflows');
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls(operation, 'workflow.save')).toHaveLength(writes);
  });

  it('edits steps through the diagram and repairs dependencies when reordering or removing them', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge, '/workflows/implement');
    const savedSteps = () =>
      calls(operation, 'workflow.save').at(-1)[1].workflow.steps;
    expect(stepIds()).toEqual(['implement', 'review', 'integrate']);
    expect(
      document.querySelectorAll('.factory-edge:not(.factory-edge--back)'),
    ).toHaveLength(2);
    expect(document.querySelectorAll('.factory-edge--back')).toHaveLength(1);
    expect(
      document.querySelector('[data-step-id="integrate"] .factory-node-gate'),
    ).not.toBeNull();
    button(t('factory.addStep')).click();
    await tick();
    const added = stepIds().at(-1);
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.stepAgent', { step: 'Step 4' }),
    );
    fill(`factory-${added}-name`, 'Notes');
    await choose(`factory-${added}-agent`, 'Builder');
    expect(await bridge.autosave.flush()).toBe(false);
    fill(`factory-${added}-task`, 'test-owned notes task');
    button(t('factory.moveStepUp', { step: 'Notes' })).click();
    await tick();
    expect(stepIds()).toEqual(['implement', 'review', added, 'integrate']);
    document.getElementById(`factory-${added}-after-review`).click();
    await tick();
    document.getElementById(`factory-${added}-approval`).click();
    await choose(`factory-${added}-returns`, 'Implement');
    expect(await bridge.autosave.flush()).toBe(true);
    expect(savedSteps()[2]).toEqual(
      step(added, 'Notes', {
        task: 'test-owned notes task',
        after: ['review'],
        returns_to: 'implement',
        approval: true,
      }),
    );
    await selectStep('integrate');
    document.getElementById(`factory-integrate-after-${added}`).click();
    await tick();
    document.getElementById('factory-integrate-after-review').click();
    await tick();
    await choose('factory-integrate-returns', 'Implement');
    await selectStep(added);
    fill(`factory-${added}-rounds`, '');
    // Removing an ancestor invalidates send-back targets of downstream steps too.
    document.getElementById(`factory-${added}-after-review`).click();
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    expect(savedSteps()[2].returns_to).toBe('');
    expect(savedSteps()[2].rounds).toBe(2);
    expect(savedSteps()[3].returns_to).toBe('');
    await selectStep('integrate');
    document.getElementById('factory-integrate-after-review').click();
    await tick();
    await choose('factory-integrate-returns', 'Notes');
    await selectStep(added);
    button(t('factory.removeStep', { step: 'Notes' })).click();
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    expect(savedSteps().map((item) => item.id)).toEqual([
      'implement',
      'review',
      'integrate',
    ]);
    expect(savedSteps()[2].after).toEqual(['review']);
    expect(savedSteps()[2].returns_to).toBe('');
  });

  it('creates a Workflow from a template with previewed Agent Models, deletes it, and copies a saved one', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, '/workflows');
    expect(
      [...document.querySelectorAll('.factory-workflow-choice')].map(
        (node) => node.dataset.workflowId,
      ),
    ).toEqual(['implement', 'triage']);
    await choose('factory-new-workflow', 'Plan');
    await settle();
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/workflows/new');
    expect(document.getElementById('factory-plan-task').value).toBe(
      'test-owned plan task',
    );
    vi.useFakeTimers();
    button(t('factory.createWorkflow')).click();
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.stepAgent', { step: 'Plan' }),
    );
    await choose('factory-plan-agent', 'Drafter');
    await vi.advanceTimersByTimeAsync(800);
    await tick();
    flushSync();
    expect(editorText()).toContain(t('factory.modelUnresolved'));
    await choose('factory-plan-agent', 'Planner');
    await vi.advanceTimersByTimeAsync(800);
    await tick();
    flushSync();
    expect(calls(operation, 'workflow.preview')).toHaveLength(2);
    expect(editorText()).not.toContain(t('factory.modelUnresolved'));
    expect(
      document
        .querySelector('[data-step-id="plan"] [data-effective-model]')
        .textContent.trim(),
    ).toBe(t('factory.effectiveModel', { model: 'demo/worker' }));
    // Team Agents become available once the Workflow has a Project.
    await choose('factory-workflow-project', 'Project A');
    await selectStep('review');
    await choose('factory-review-agent', 'Critic');
    fill('factory-workflow-name', 'Plan only');
    expect(calls(operation, 'workflow.save')).toHaveLength(0);
    button(t('factory.createWorkflow')).click();
    await settle();
    await settle();
    const created = calls(operation, 'workflow.save').at(-1)[1];
    expect(created).not.toHaveProperty('expected_revision');
    expect(created.workflow).toMatchObject({
      name: 'Plan only',
      project_id: 'project-a',
      final_approval: true,
      steps: [
        { id: 'plan', source: 'agent', agent: 'planner' },
        { id: 'review', source: 'team', agent: 'critic' },
      ],
      triggers: [],
    });
    const id = created.workflow.id;
    expect(state.workflows.map((entry) => entry.id)).toContain(id);
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith(`/workflows/${id}`);
    button(t('factory.deleteWorkflow')).click();
    await tick();
    button(t('factory.deleteWorkflow'), dialog()).click();
    await settle();
    expect(calls(operation, 'workflow.delete').at(-1)[1]).toEqual({ id });
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/workflows');
    await choose(
      'factory-new-workflow',
      t('factory.copyName', { name: 'Implement' }),
    );
    await settle();
    expect(document.getElementById('factory-workflow-name').value).toBe(
      t('factory.copyName', { name: 'Implement' }),
    );
    expect(stepIds()).toEqual(['implement', 'review', 'integrate']);
    button(t('common.cancel')).click();
    await settle();
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/workflows');
    expect(calls(operation, 'workflow.save')).toHaveLength(1);
  });

  it('adds schedule and webhook triggers, then shows the saved webhook URL and its secret', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, `/workflows/${triageWorkflow.id}`);
    button(t('factory.addSchedule')).click();
    await tick();
    button(t('factory.addWebhook')).click();
    await tick();
    const [schedule, webhook] = [
      ...document.querySelectorAll('[data-trigger-id]'),
    ].map((node) => node.dataset.triggerId);
    const section = (id) => document.querySelector(`[data-trigger-id="${id}"]`);
    expect(section(webhook).textContent).toContain(
      t('factory.trigger.saveForUrl'),
    );
    expect(
      document.getElementById(`factory-trigger-${webhook}-project`).textContent,
    ).toContain(t('factory.trigger.repositoryProject'));
    fill(`factory-trigger-${schedule}-schedule`, 'every 2h');
    fill(`factory-trigger-${schedule}-task`, 'test-owned scheduled task');
    expect(await bridge.autosave.flush()).toBe(true);
    await settle();
    expect(
      calls(operation, 'workflow.save').at(-1)[1].workflow.triggers,
    ).toEqual([
      {
        id: schedule,
        type: 'schedule',
        schedule: 'every 2h',
        enabled: true,
        project: '',
        title: '',
        task: 'test-owned scheduled task',
      },
      {
        id: webhook,
        type: 'webhook',
        schedule: '',
        enabled: true,
        project: '@repository',
        title: '',
        task: '',
      },
    ]);
    expect(
      section(webhook).querySelector('[data-webhook-url]').textContent,
    ).toBe(`https://vbot.test/hooks/triage/${webhook}`);
    expect(
      section(schedule).querySelector('[data-trigger-status]'),
    ).not.toBeNull();
    button(t('factory.trigger.showSecret'), section(webhook)).click();
    await settle();
    expect(calls(operation, 'workflow.secret').at(-1)[1]).toEqual({
      id: 'triage',
      trigger: webhook,
    });
    expect(
      section(webhook).querySelector('[data-webhook-secret]').textContent,
    ).toBe(state.secrets[`triage/${webhook}`]);
  });
});

describe('Factory owned Session activity', () => {
  it('reads and follows only the chosen participant, opens projected links through the bridge and releases streams on close', async () => {
    const { bridge, state } = createBridge([
      startedCard('fct-a', 'working', '', implementWorkflow, {
        implement: { status: 'running' },
      }),
    ]);
    state.details['fct-a'] = {
      participants: [
        {
          participant_id: 'implement-1',
          step: 'implement',
          address: {
            project_id: 'project-a',
            agent_id: 'owned-worker',
            session_id: 'ses-worker',
          },
          display_name: 'Factory Implement',
          model: 'demo/worker',
          run_active: true,
          lifecycle_run_id: 'run-worker',
        },
      ],
      runs: [
        {
          run_id: 'run-worker',
          participant_id: 'implement-1',
          step: 'implement',
          status: 'running',
        },
      ],
    };
    await render(bridge, '/card/fct-a');
    button(t('factory.openSession')).click();
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledWith(
      'fct-a.implement',
      'implement-1',
      { limit: 100, child_limit: 100 },
    );
    expect(bridge.subscribeRun).toHaveBeenCalledWith(
      'fct-a.implement',
      'run-worker',
      0,
      { timeline: true, childLimit: 100 },
    );
    bridge.emitRun('subscription-a', {
      type: 'timeline_snapshot',
      run_id: 'run-worker',
      sequence: 1,
      history_generation: 'gen-1',
      items: [
        assistantRun('worker', [outputItem('https://example.test/factory')], {
          status: 'running',
          source: 'history',
        }),
      ],
    });
    await tick();
    const link = document.querySelector('.factory-history-timeline a');
    expect(link).not.toBeNull();
    link.click();
    await tick();
    expect(bridge.openLink).toHaveBeenCalledWith(
      'https://example.test/factory',
    );
    button(
      t('common.close'),
      document.querySelector('.factory-session-history'),
    ).click();
    await tick();
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('subscription-a');
    bridge.emitRun('subscription-a', {
      type: 'timeline_snapshot',
      run_id: 'run-worker',
      sequence: 2,
      items: [],
    });
    await tick();
    expect(document.querySelector('.factory-session-history')).toBeNull();
  });
});
