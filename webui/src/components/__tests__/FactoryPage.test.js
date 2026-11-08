// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  createBridge,
  card,
  render,
  settle,
  button,
  fill,
  choose,
  calls,
  actionButtons,
  setup,
  step,
  templates,
  t,
  tick,
  flushSync,
} from './FactoryPage.support.js';
import { assistantRun, outputItem } from './timelineItems.support.js';

const implementWorkflow = setup.workflows[0];
const planImplement = {
  id: 'planimpl',
  name: 'Plan, then implement',
  description: '',
  steps: [
    step('plan', 'Plan'),
    step('planreview', 'Plan review', { after: ['plan'], returns_to: 'plan' }),
    step('implement', 'Implement', { after: ['planreview'] }),
    step('review', 'Review', { after: ['implement'], returns_to: 'implement' }),
  ],
};
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
  document.querySelector('.factory-setup-editor').textContent;
const stepIds = () =>
  [...document.querySelectorAll('.factory-diagram [data-step-id]')].map(
    (node) => node.dataset.stepId,
  );
async function selectStep(id) {
  document
    .querySelector(`.factory-diagram [data-step-id="${id}"] button`)
    .click();
  await tick();
}

describe('Factory Board', () => {
  it('groups every state, hides empty Proposals and keeps discarded cards behind their filter', async () => {
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
        card(value, value, {
          position: index + 1,
          ...(value === 'working'
            ? {
                workflow_id: 'drafts',
                workflow: { id: 'drafts', name: 'Plan from drafts' },
                step: {
                  id: 'drafts',
                  name: 'Draft plans',
                },
              }
            : {}),
        }),
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
    expect(stepOf('working')).toContain('Plan from drafts');
    expect(stepOf('working')).toContain('Draft plans');
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

  it('filters Projects, reorders the backlog with controls and creates operator cards through a layered form', async () => {
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
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    fill('factory-create-title', 'Operator card');
    fill('factory-create-description', 'test-owned objective');
    expect(
      document.getElementById('factory-create-workflow').textContent,
    ).toContain(t('factory.defaultWorkflowTab', { name: 'Implement' }));
    button(t('factory.createCard')).click();
    await settle();
    expect(calls(operation, 'card.create').at(-1)[1]).toEqual({
      project_id: 'project-a',
      title: 'Operator card',
      description: 'test-owned objective',
      workflow: 'implement',
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
  it.each([
    ['proposal', '', ['approveProposal', 'discard']],
    ['queued', '', ['discard']],
    ['working', '', ['discard']],
    ['waiting', 'checkpoint', ['acceptCheckpoint', 'sendBack', 'discard']],
    [
      'waiting',
      'finished',
      ['acceptDone', 'sendBack', 'discard'],
      templates[0],
    ],
    ['waiting', 'no_agreement', ['acceptAnyway', 'sendBack', 'discard']],
    ['waiting', 'blocked', ['answer', 'discard']],
    ['waiting', 'run_failed', ['retry', 'sendBack', 'discard']],
    ['waiting', 'restart', ['retry', 'sendBack', 'discard']],
    ['waiting', 'budget_exhausted', ['retry', 'sendBack', 'discard']],
    ['done', '', ['delete']],
    ['discarded', '', ['delete']],
  ])(
    'offers only state-appropriate actions for %s / %s',
    async (state, reason, expected, workflow = implementWorkflow) => {
      const { bridge } = createBridge([
        card('fct-a', state, {
          waiting_reason: reason,
          runtime: { workflow },
          step: { id: 'integrate', name: 'Integrate' },
        }),
      ]);
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
    },
  );

  it('keeps step results, provenance, cost and permissions reachable and approves only the displayed revision', async () => {
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
      card('fct-a', 'waiting', {
        waiting_reason: 'checkpoint',
        runtime: { workflow: implementWorkflow },
        step: { id: 'integrate', name: 'Integrate' },
        done_steps: ['implement', 'review'],
        summary: 'test-owned summary',
        origin: 'user',
        origin_session: origin,
        origin_run: 'run-source',
      }),
    ]);
    state.details['fct-a'] = {
      approvals: [permission],
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
    expect(
      document.querySelector('[data-step-id="integrate"]').dataset.stepStatus,
    ).toBe('waiting');
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
    });
    expect(document.querySelector('[data-state]').dataset.state).toBe(
      'working',
    );
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
    ['blocked', 'answer', 'text'],
    ['finished', 'sendBack', 'comment'],
  ])(
    'submits a %s response without replacing its draft on invalidation',
    async (reason, action, field) => {
      const { bridge, operation } = createBridge([
        card('fct-a', 'waiting', {
          waiting_reason: reason,
          runtime: {
            question: 'test-owned question',
            workflow: implementWorkflow,
          },
        }),
      ]);
      await render(bridge, '/card/fct-a');
      button(t(`factory.${action}`)).click();
      await tick();
      const input = fill('factory-response', 'test-owned response');
      input.focus();
      bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
      await settle();
      expect(document.getElementById('factory-response')).toBe(input);
      expect(input.value).toBe('test-owned response');
      button(
        t(`factory.${action}`),
        document.querySelector('[role="dialog"]'),
      ).click();
      await settle();
      expect(
        calls(
          operation,
          `card.${action === 'sendBack' ? 'send_back' : 'answer'}`,
        ).at(-1)[1],
      ).toEqual({ id: 'fct-a', [field]: 'test-owned response' });
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    },
  );

  it('shows dependency progress and every result, and sends back to any chosen step', async () => {
    const { bridge, state, operation } = createBridge([
      card('fct-a', 'waiting', {
        waiting_reason: 'no_agreement',
        workflow_id: 'planimpl',
        runtime: { workflow: planImplement },
        step: { id: 'review', name: 'Review' },
        done_steps: ['plan', 'planreview', 'implement'],
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
    expect(
      [...document.querySelectorAll('[data-step-status]')].map(
        (node) => node.dataset.stepStatus,
      ),
    ).toEqual(['done', 'done', 'done', 'waiting']);
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
    button(
      t('factory.sendBack'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'card.send_back').at(-1)[1]).toEqual({
      id: 'fct-a',
      comment: 'test-owned rework',
      step: 'planreview',
    });
    // A reset dependency no longer appears done, even if it precedes the active step.
    state.cards[0].done_steps = ['plan'];
    state.cards[0].step = { id: 'planreview', name: 'Plan review' };
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 3 });
    await settle();
    expect(
      [...document.querySelectorAll('[data-step-status]')].map(
        (node) => node.dataset.stepStatus,
      ),
    ).toEqual(['done', 'current', 'pending', 'pending']);
  });

  it('confirms destructive actions in navigation layers, deletes only final cards and repairs their route', async () => {
    const { bridge, operation, state } = createBridge([
      card('fct-a', 'waiting', {
        waiting_reason: 'no_agreement',
      }),
    ]);
    await render(bridge, '/card/fct-a');
    button(t('factory.discard')).click();
    await tick();
    bridge.back();
    await tick();
    expect(calls(operation, 'card.discard')).toHaveLength(0);
    button(t('factory.discard')).click();
    await tick();
    button(
      t('factory.discard'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'card.discard').at(-1)[1]).toEqual({ id: 'fct-a' });
    button(t('factory.delete')).click();
    await tick();
    bridge.back();
    await tick();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(calls(operation, 'card.delete')).toHaveLength(0);
    button(t('factory.delete')).click();
    await tick();
    state.handlers['card.delete'] = () =>
      Promise.reject(
        Object.assign(new Error('test-owned untranslated backend detail'), {
          code: 'card_not_final',
        }),
      );
    button(
      t('factory.delete'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    const alert = document.querySelector('[role="dialog"] [role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert.textContent).not.toContain(
      'test-owned untranslated backend detail',
    );
    delete state.handlers['card.delete'];
    button(
      t('factory.delete'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'card.delete')).toHaveLength(2);
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
  });
});

describe('Factory existing-record editors', () => {
  it('saves independent optional Workflow and Step limits, and clears them to unlimited', async () => {
    const { bridge, state, operation } = createBridge();
    await render(bridge, '/setups/project-a');
    fill('factory-workflow-limit-cost_usd', '2.5');
    fill('factory-workflow-limit-tokens', '10000');
    await selectStep('implement');
    fill('factory-implement-limit-tool_calls', '50');
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    const saved = state.setups[0].workflows[0];
    expect(saved.limits).toEqual({ cost_usd: 2.5, tokens: 10000 });
    expect(saved.steps[0].limits).toEqual({ tool_calls: 50 });
    fill('factory-workflow-limit-cost_usd', '');
    fill('factory-implement-limit-tool_calls', '0');
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    expect(state.setups[0].workflows[0].limits).toEqual({ tokens: 10000 });
    expect(state.setups[0].workflows[0].steps[0].limits).toEqual({});
    expect(calls(operation, 'setup.update').at(-1)[1]).not.toHaveProperty(
      'turn_budget',
    );
  });

  it('flushes paused Card limits before Retry and retains consumption', async () => {
    const budget = { runs: ['run-one'], tool_calls: 5, step_tool_calls: 5 };
    const { bridge, state, operation } = createBridge([
      card('fct-a', 'waiting', {
        waiting_reason: 'budget_exhausted',
        runtime: { workflow: implementWorkflow, step: 'implement', budget },
      }),
    ]);
    await render(bridge, '/card/fct-a');
    fill('factory-card-limit-tokens', '1000');
    fill('factory-step-limit-tool_calls', '8');
    await tick();
    button(t('factory.retry')).click();
    await settle();
    expect(calls(operation, 'card.limits').at(-1)[1]).toMatchObject({
      expected_revision: 1,
      card_limits: { tokens: 1000 },
      step_limits: { tool_calls: 8 },
    });
    expect(calls(operation, 'card.retry')).toHaveLength(1);
    const actions = operation.mock.calls.map(([name]) => name);
    expect(actions.indexOf('card.limits')).toBeLessThan(
      actions.indexOf('card.retry'),
    );
    expect(state.cards[0].runtime.budget).toEqual(budget);
  });

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
    button(t('factory.setups')).click();
    await settle();
    expect(calls(operation, 'card.update').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 2,
      title: 'Third',
    });
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/setups');
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

  it('saves step Model choices, warns about equal Models, waits for number completion and retains failed drafts', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, '/setups/project-a');
    await selectStep('review');
    expect(document.getElementById('factory-review-rounds').value).toBe('2');
    expect(editorText()).not.toContain(t('factory.sameModel'));
    await choose('factory-review-model', 'demo/worker');
    await bridge.autosave.flush();
    await tick();
    expect(editorText()).toContain(t('factory.sameModel'));
    expect(calls(operation, 'setup.update').at(-1)[1]).toMatchObject({
      expected_revision: 1,
      default_workflow: 'implement',
      workflows: [
        {
          id: 'implement',
          steps: [
            { id: 'implement', agent: 'builder' },
            {
              id: 'review',
              returns_to: 'implement',
              rounds: 2,
              source: 'team',
              agent: 'critic',
              model: 'demo/worker',
              effort: '',
            },
            { id: 'integrate', approval: true },
          ],
        },
      ],
    });
    vi.useFakeTimers();
    const input = document.getElementById('factory-review-rounds');
    input.focus();
    fill('factory-review-rounds', '3');
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls(operation, 'setup.update')).toHaveLength(1);
    input.blur();
    await tick();
    await Promise.resolve();
    flushSync();
    expect(calls(operation, 'setup.update')).toHaveLength(2);
    expect(
      calls(operation, 'setup.update').at(-1)[1].workflows[0].steps[1].rounds,
    ).toBe(3);
    state.handlers['setup.update'] = () =>
      Promise.reject(new Error('test-owned write failure'));
    fill('factory-review-task', 'test-owned changed task');
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
    await settle();
    expect(document.getElementById('factory-review-task').value).toBe(
      'test-owned changed task',
    );
    expect(calls(operation, 'setup.get')).toHaveLength(1);
    delete state.handlers['setup.update'];
    expect(await bridge.autosave.flush()).toBe(true);
    expect(state.setups[0].workflows[0].steps[1].task).toBe(
      'test-owned changed task',
    );
    const writes = calls(operation, 'setup.update').length;
    state.setups = [];
    bridge.invalidate({ resource: 'setups', ids: ['project-a'], revision: 0 });
    await settle();
    expect(button(t('factory.createSetup'))).toBeDefined();
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls(operation, 'setup.update')).toHaveLength(writes);
  });

  it('edits steps through the diagram and repairs dependencies when reordering or removing them', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge, '/setups/project-a');
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
      t('factory.invalid.stepAgent', {
        location: t('factory.location.step', {
          workflow: 'Implement',
          step: 'Step 4',
        }),
      }),
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
    const saved = calls(operation, 'setup.update').at(-1)[1].workflows[0].steps;
    expect(saved[2]).toEqual(
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
    const repaired = calls(operation, 'setup.update').at(-1)[1].workflows[0]
      .steps;
    expect(repaired[2].returns_to).toBe('');
    expect(repaired[2].rounds).toBe(2);
    expect(repaired[3].returns_to).toBe('');
    await selectStep('integrate');
    document.getElementById('factory-integrate-after-review').click();
    await tick();
    await choose('factory-integrate-returns', 'Notes');
    await selectStep(added);
    button(t('factory.removeStep', { step: 'Notes' })).click();
    await tick();
    expect(await bridge.autosave.flush()).toBe(true);
    const remaining = calls(operation, 'setup.update').at(-1)[1].workflows[0]
      .steps;
    expect(remaining.map((item) => item.id)).toEqual([
      'implement',
      'review',
      'integrate',
    ]);
    expect(remaining[2].after).toEqual(['review']);
    expect(remaining[2].returns_to).toBe('');
    bridge.invalidate({ resource: 'setups', ids: ['project-a'], revision: 5 });
    await settle();
    expect(stepIds()).toEqual(['implement', 'review', 'integrate']);
  });

  it('adds Workflows from templates, previews unsaved steps and duplicates, renames, defaults and deletes Workflows', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge, '/setups/project-a');
    vi.useFakeTimers();
    const panel = () => document.getElementById('factory-workflow-panel');
    await choose('factory-add-workflow', 'Plan');
    expect(panel().dataset.workflowId).toBe('plan');
    expect(document.getElementById('factory-plan-task').value).toBe(
      'test-owned plan task',
    );
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.stepAgent', {
        location: t('factory.location.step', {
          workflow: 'Plan',
          step: 'Plan',
        }),
      }),
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
    expect(calls(operation, 'setup.preview')).toHaveLength(2);
    expect(editorText()).not.toContain(t('factory.modelUnresolved'));
    expect(
      document
        .querySelector('[data-step-id="plan"] [data-effective-model]')
        .textContent.trim(),
    ).toBe(t('factory.effectiveModel', { model: 'demo/worker' }));
    await selectStep('review');
    await choose('factory-review-agent', 'Critic');
    fill('factory-workflow-name', 'Plan only');
    button(t('factory.makeDefault')).click();
    await tick();
    button(t('factory.duplicateWorkflow')).click();
    await tick();
    expect(panel().dataset.workflowId).not.toBe('plan');
    expect(document.getElementById('factory-workflow-name').value).toBe(
      t('factory.copyName', { name: 'Plan only' }),
    );
    button(t('factory.deleteWorkflow')).click();
    await tick();
    button(
      t('factory.deleteWorkflow'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await tick();
    expect(panel().dataset.workflowId).toBe('plan');
    expect(await bridge.autosave.flush()).toBe(true);
    const payload = calls(operation, 'setup.update').at(-1)[1];
    expect(payload.default_workflow).toBe('plan');
    expect(payload.workflows.map(({ id, name }) => [id, name])).toEqual([
      ['implement', 'Implement'],
      ['plan', 'Plan only'],
    ]);
    expect(payload.workflows[1].steps).toEqual([
      {
        ...templates[0].steps[0],
        source: 'agent',
        agent: 'planner',
      },
      {
        ...templates[0].steps[1],
        source: 'team',
        agent: 'critic',
      },
    ]);
  });

  it('creates setups explicitly at revision zero from the first template and resets the editor after deletion', async () => {
    const { bridge, operation, state } = createBridge();
    state.setups = [];
    state.handlers['setup.get'] = ({ project_id }) => ({
      setup: state.setups.find((entry) => entry.project_id === project_id) ?? {
        project_id,
        revision: 0,
      },
      effective_models: {},
    });
    await render(bridge, '/setups/project-b');
    expect(button(t('factory.createSetup'))).toBeDefined();
    expect(calls(operation, 'setup.update')).toHaveLength(0);
    expect(
      document.getElementById('factory-workflow-panel').dataset.workflowId,
    ).toBe('plan');
    button(t('factory.createSetup')).click();
    await settle();
    expect(editorText()).toContain(
      t('factory.invalid.stepAgent', {
        location: t('factory.location.step', {
          workflow: 'Plan',
          step: 'Plan',
        }),
      }),
    );
    await choose('factory-plan-agent', 'Planner');
    await selectStep('review');
    await choose('factory-review-agent', 'Critic');
    expect(calls(operation, 'setup.update')).toHaveLength(0);
    button(t('factory.createSetup')).click();
    await settle();
    expect(calls(operation, 'setup.update').at(-1)[1]).toMatchObject({
      project_id: 'project-b',
      expected_revision: 0,
      enabled: true,
      default_workflow: 'plan',
      workflows: [
        {
          id: 'plan',
          steps: [
            { source: 'agent', agent: 'planner' },
            { source: 'team', agent: 'critic' },
          ],
        },
      ],
    });
    button(t('factory.deleteSetup')).click();
    await tick();
    button(
      t('factory.deleteSetup'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'setup.delete').at(-1)[1]).toEqual({
      project_id: 'project-b',
    });
    expect(button(t('factory.createSetup'))).toBeDefined();
  });
});

describe('Factory owned Session activity', () => {
  it('reads and follows only the chosen participant, opens projected links through the bridge and releases streams on close', async () => {
    const { bridge, state } = createBridge([card('fct-a', 'working')]);
    state.details['fct-a'] = {
      participants: [
        {
          participant_id: 'implement-1',
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
          status: 'running',
        },
      ],
    };
    await render(bridge, '/card/fct-a');
    button(t('factory.openSession')).click();
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledWith('fct-a', 'implement-1', {
      limit: 100,
      child_limit: 100,
    });
    expect(bridge.subscribeRun).toHaveBeenCalledWith('fct-a', 'run-worker', 0, {
      timeline: true,
      childLimit: 100,
    });
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
