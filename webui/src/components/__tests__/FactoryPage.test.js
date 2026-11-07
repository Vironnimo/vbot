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
  templates,
  t,
  tick,
  flushSync,
} from './FactoryPage.support.js';
import { assistantRun, outputItem } from './timelineItems.support.js';

const implementWorkflow = setup.workflows[0];
const lane = { source: 'team', agent: 'builder', model: '', effort: '' };
const stage = (id, name, kind, extra) => ({
  id,
  name,
  kind,
  instructions: '',
  lanes: [lane],
  approval: false,
  ...extra,
});
const planImplement = {
  id: 'planimpl',
  name: 'Plan, then implement',
  description: '',
  stages: [
    stage('plan', 'Plan', 'work', { output: 'document' }),
    stage('planreview', 'Plan review', 'review', {
      target: 'plan',
      rounds: 2,
    }),
    stage('implement', 'Implement', 'work', { output: 'changes' }),
    stage('review', 'Review', 'review', { target: 'implement', rounds: 2 }),
  ],
};
const output = (id, stageId, name, kind, extra = {}) => ({
  id,
  stage_id: stageId,
  stage_name: name,
  kind,
  lane: 0,
  pass: 1,
  participant_id: `${stageId}-1`,
  summary: '',
  result: '',
  checks: '',
  branch: '',
  base: '',
  base_sha: '',
  head_sha: '',
  verdict: '',
  recommendation: '',
  findings: [],
  created_at: '2026-11-07T09:00:00Z',
  ...extra,
});
const editorText = () =>
  document.querySelector('.factory-setup-editor').textContent;
const stageIds = () =>
  [...document.querySelectorAll('#factory-workflow-panel [data-stage-id]')].map(
    (node) => node.dataset.stageId,
  );

describe('Factory Board', () => {
  it('groups every state, hides empty Proposals and keeps discarded cards behind their filter', async () => {
    const states = [
      'proposal',
      'queued',
      'working',
      'integrating',
      'reviewing',
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
                stage: {
                  index: 0,
                  id: 'drafts',
                  name: 'Draft plans',
                  kind: 'work',
                  lane: 1,
                  lanes: 3,
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
    expect(ids('progress')).toEqual(['working', 'integrating']);
    expect(ids('review')).toEqual(['reviewing']);
    expect(ids('waiting')).toEqual(['waiting']);
    expect(ids('done')).toEqual(['done']);
    const stageOf = (id) =>
      document.querySelector(`[data-card-id="${id}"] [data-card-stage]`)
        .textContent;
    expect(stageOf('working')).toContain('Plan from drafts');
    expect(stageOf('working')).toContain(
      t('factory.stageLane', { stage: 'Draft plans', lane: 2, lanes: 3 }),
    );
    expect(stageOf('queued')).toBe('Implement');
    expect(document.querySelector('.factory-wip').textContent).toBe(
      t('factory.running', { running: 3, limit: 2 }),
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
    ['reviewing', '', ['discard']],
    ['integrating', '', ['discard']],
    ['waiting', 'checkpoint', ['acceptCheckpoint', 'sendBack', 'discard']],
    ['waiting', 'finished', ['acceptIntegrate', 'sendBack', 'discard']],
    [
      'waiting',
      'finished',
      ['acceptDone', 'sendBack', 'discard'],
      templates[0],
    ],
    ['waiting', 'no_agreement', ['acceptAnyway', 'sendBack', 'discard']],
    ['waiting', 'blocked', ['answer', 'discard']],
    ['waiting', 'run_failed', ['retry', 'sendBack', 'discard']],
    ['waiting', 'review_failed', ['retry', 'sendBack', 'discard']],
    ['waiting', 'restart', ['retry', 'sendBack', 'discard']],
    ['waiting', 'budget_exhausted', ['retry', 'sendBack', 'discard']],
    ['waiting', 'integration_failed', ['retry', 'sendBack', 'discard']],
    ['done', '', ['delete']],
    ['discarded', '', ['delete']],
  ])(
    'offers only state-appropriate actions for %s / %s',
    async (state, reason, expected, workflow = implementWorkflow) => {
      const { bridge } = createBridge([
        card('fct-a', state, { waiting_reason: reason, runtime: { workflow } }),
      ]);
      await render(bridge, '/card/fct-a');
      const labels = {
        approveProposal: t('factory.approveProposal'),
        discard: t('factory.discard'),
        acceptCheckpoint: t('factory.acceptCheckpoint'),
        acceptIntegrate: t('factory.acceptIntegrate'),
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

  it('foregrounds the review recommendation and keeps evidence, provenance, cost and permissions reachable', async () => {
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
        waiting_reason: 'finished',
        runtime: { workflow: implementWorkflow },
        recommendation: 'test-owned recommendation',
        summary: 'test-owned summary',
        checks: 'test-owned checks',
        branch: 'feature',
        base: 'main',
        base_sha: 'base123',
        head_sha: 'head456',
        origin: 'user',
        origin_session: origin,
        origin_run: 'run-source',
      }),
    ]);
    state.details['fct-a'] = {
      approvals: [permission],
      outputs: [
        output(1, 'review', 'Review', 'review', {
          verdict: 'approve',
          findings: ['test-owned finding'],
          head_sha: 'head456',
        }),
      ],
      events: [
        {
          id: 1,
          event: 'reviewed',
          actor: 'review-1',
          at: '2026-11-07T09:00:00Z',
          detail: { state: 'waiting', previous_state: 'reviewing' },
        },
      ],
    };
    await render(bridge, '/card/fct-a');
    expect(document.querySelector('[data-integration-landing]')).toBeNull();
    expect(
      document.querySelector('[data-recommendation]').textContent,
    ).toContain('test-owned recommendation');
    expect(document.querySelector('[data-checks]').textContent).toContain(
      'test-owned checks',
    );
    expect(document.querySelector('[data-submission]').textContent).toContain(
      'base123..head456',
    );
    const review = document.querySelector('[data-output-stage="review"]');
    expect(review.textContent).toContain('test-owned finding');
    expect(review.querySelector('[data-verdict]').textContent).toContain(
      t('factory.reviewApproved'),
    );
    expect(
      document.querySelector('[aria-labelledby="factory-timeline-heading"]')
        .textContent,
    ).toContain(t('factory.event.reviewed'));
    expect(document.querySelector('[data-usage] dd').textContent).toBe('150');
    expect(document.querySelector('.factory-cost').textContent).not.toBe(
      t('factory.costUnavailable'),
    );
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
    button(t('factory.acceptIntegrate')).click();
    await settle();
    expect(calls(operation, 'card.accept').at(-1)[1]).toEqual({
      id: 'fct-a',
      expected_revision: 1,
    });
    expect(document.querySelector('[data-state]').dataset.state).toBe(
      'integrating',
    );
  });

  it.each([
    ['pending', false, false],
    ['approved', false, false],
    ['changes_requested', false, false],
    ['changes_requested', true, false],
    ['pending', false, true],
  ])(
    'keeps a %s landing visible with its evidence and no automatic revert, historical=%s, baseUnknown=%s',
    async (status, historical, baseUnknown) => {
      const landing = {
        head_sha: 'test-owned-captured-head',
        base_sha: baseUnknown ? '' : 'test-owned-captured-base',
        landing_sha: 'test-owned-landing-commit',
        branch_head_sha: null,
        status,
        extra_paths: status === 'approved' ? [] : ['src/additional.py'],
        merge_paths: status === 'changes_requested' ? ['src/merged.py'] : [],
      };
      const currentState = baseUnknown
        ? 'integrating'
        : historical
          ? 'working'
          : status === 'pending'
            ? 'reviewing'
            : 'waiting';
      const { bridge, state, operation } = createBridge([
        card('fct-a', currentState, {
          head_sha: baseUnknown
            ? 'test-owned-previous-reviewed-head'
            : historical
              ? 'test-owned-new-submission'
              : landing.head_sha,
          waiting_reason: status === 'approved' ? 'finished' : 'no_agreement',
          runtime: { workflow: implementWorkflow },
          integration_landing: landing,
        }),
      ]);
      await render(bridge);
      const labels = {
        pending: t('factory.landing.pending'),
        approved: t('factory.landing.approved'),
        changes_requested: t('factory.landing.changesRequested'),
      };
      const tile = document.querySelector('[data-card-id="fct-a"]');
      expect(tile.querySelector('[data-landing-status]').textContent).toBe(
        historical ? t('factory.landing.alreadyLanded') : labels[status],
      );
      tile.click();
      await settle();
      const notice = document.querySelector('[data-integration-landing]');
      expect(notice.dataset.integrationLanding).toBe(status);
      expect(notice.getAttribute('role')).toBe('status');
      expect(notice.querySelector('h3').textContent).toBe(labels[status]);
      expect(notice.textContent).toContain(t('factory.landing.alreadyLanded'));
      expect(notice.textContent).toContain(
        t('factory.landing.noAutomaticRevert'),
      );
      expect(Boolean(notice.querySelector('[data-landing-historical]'))).toBe(
        historical,
      );
      expect(
        notice.querySelector('[data-landing-commit]').textContent,
      ).toContain(landing.landing_sha);
      for (const [attribute, expected] of [
        ['data-landing-extra-paths', landing.extra_paths],
        ['data-landing-merge-paths', landing.merge_paths],
      ]) {
        expect(
          [...notice.querySelectorAll(`[${attribute}] li`)].map(
            (entry) => entry.textContent,
          ),
        ).toEqual(expected);
        expect(Boolean(notice.querySelector(`[${attribute}]`))).toBe(
          expected.length > 0,
        );
      }
      if (landing.merge_paths.length)
        expect(
          notice.querySelector('[data-landing-merge-paths]').textContent,
        ).toContain(t('factory.landing.mergeHelp'));
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      button(t('factory.landing.copyCommit'), notice).click();
      await tick();
      expect(writeText).toHaveBeenLastCalledWith(landing.landing_sha);
      if (landing.extra_paths.length) {
        button(t('factory.landing.copyExtraPaths'), notice).click();
        await tick();
        expect(writeText).toHaveBeenLastCalledWith(
          landing.extra_paths.join('\n'),
        );
      }
      expect(actionButtons()).toEqual(
        historical || status === 'pending'
          ? [t('factory.discard')]
          : [
              status === 'approved'
                ? t('factory.acceptIntegrate')
                : t('factory.acceptAnyway'),
              t('factory.sendBack'),
              t('factory.discard'),
            ],
      );
      state.cards[0].revision += 1;
      bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
      await settle();
      expect(
        document.querySelector('[data-integration-landing]'),
      ).not.toBeNull();
      expect(calls(operation, 'card.accept')).toHaveLength(0);
      expect(calls(operation, 'card.send_back')).toHaveLength(0);
      expect(calls(operation, 'card.discard')).toHaveLength(0);
    },
  );

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

  it('shows Workflow progress and every stage result, and sends back to a chosen work stage', async () => {
    const { bridge, state, operation } = createBridge([
      card('fct-a', 'waiting', {
        waiting_reason: 'no_agreement',
        workflow_id: 'planimpl',
        runtime: { workflow: planImplement },
        stage: {
          index: 3,
          id: 'review',
          name: 'Review',
          kind: 'review',
          lane: 0,
          lanes: 1,
        },
      }),
    ]);
    state.details['fct-a'] = {
      outputs: [
        output(1, 'plan', 'Plan', 'work', {
          summary: 'test-owned plan summary',
          result: '# test-owned plan\n\nFirst step',
        }),
        output(2, 'planreview', 'Plan review', 'review', {
          verdict: 'approve',
        }),
        output(3, 'implement', 'Implement', 'work', {
          branch: 'feature',
          base: 'main',
          base_sha: 'base1',
          head_sha: 'head2',
          checks: 'test-owned checks',
        }),
        output(4, 'review', 'Review', 'review', {
          verdict: 'changes_requested',
          recommendation: 'test-owned recommendation',
          findings: ['test-owned finding'],
          head_sha: 'head2',
        }),
      ],
    };
    await render(bridge, '/card/fct-a');
    expect(
      [
        ...document.querySelectorAll(
          '[data-workflow-progress="planimpl"] [data-stage-status]',
        ),
      ].map((node) => node.dataset.stageStatus),
    ).toEqual(['done', 'done', 'done', 'waiting']);
    const result = (id) =>
      document.querySelector(`[data-output-stage="${id}"]`);
    expect(result('plan').dataset.outputKind).toBe('work');
    expect(result('plan').textContent).toContain('test-owned plan summary');
    expect(
      result('plan').querySelector('.factory-document h1').textContent,
    ).toBe('test-owned plan');
    expect(
      result('implement').querySelector('[data-output-change]').textContent,
    ).toContain('base1..head2');
    expect(result('implement').textContent).toContain('test-owned checks');
    expect(result('planreview').textContent).toContain(
      t('factory.reviewApproved'),
    );
    expect(result('planreview').textContent).toContain(t('factory.noFindings'));
    expect(
      result('review').querySelector('[data-verdict]').dataset.verdict,
    ).toBe('changes_requested');
    expect(result('review').textContent).toContain(t('factory.reviewChanges'));
    expect(result('review').textContent).toContain('test-owned finding');
    expect(actionButtons()).toEqual([
      t('factory.acceptAnyway'),
      t('factory.sendBack'),
      t('factory.discard'),
    ]);
    button(t('factory.sendBack')).click();
    await tick();
    expect(
      document.getElementById('factory-send-back-stage').textContent,
    ).toContain(t('factory.sendBackDefault', { stage: 'Implement' }));
    fill('factory-response', 'test-owned rework');
    await choose('factory-send-back-stage', 'Plan');
    button(
      t('factory.sendBack'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'card.send_back').at(-1)[1]).toEqual({
      id: 'fct-a',
      comment: 'test-owned rework',
      stage: 'plan',
    });
  });

  it('confirms destructive actions in navigation layers, deletes only final cards and repairs their route', async () => {
    const { bridge, operation, state } = createBridge([
      card('fct-a', 'waiting', {
        waiting_reason: 'no_agreement',
        head_sha: 'test-owned-head',
        integration_landing: {
          head_sha: 'test-owned-head',
          base_sha: 'test-owned-base',
          landing_sha: 'test-owned-landing',
          branch_head_sha: null,
          status: 'changes_requested',
          extra_paths: [],
          merge_paths: [],
        },
      }),
    ]);
    await render(bridge, '/card/fct-a');
    button(t('factory.discard')).click();
    await tick();
    expect(
      document.querySelector('[role="dialog"] [data-landing-discard-warning]')
        .textContent,
    ).toBe(t('factory.landing.discardWarning'));
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
    expect(document.querySelector('[data-integration-landing]')).not.toBeNull();
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

  it('saves lane Model choices, warns about equal effective Models, waits for number completion and keeps failed setup drafts', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, '/setups/project-a');
    expect(document.getElementById('factory-review-rounds').value).toBe('2');
    expect(editorText()).not.toContain(t('factory.sameModel'));
    await choose('factory-review-0-model', 'demo/worker');
    await bridge.autosave.flush();
    await tick();
    expect(editorText()).toContain(t('factory.sameModel'));
    expect(calls(operation, 'setup.update').at(-1)[1]).toMatchObject({
      expected_revision: 1,
      default_workflow: 'implement',
      workflows: [
        {
          id: 'implement',
          stages: [
            { id: 'implement', lanes: [{ agent: 'builder' }] },
            {
              id: 'review',
              target: 'implement',
              rounds: 2,
              lanes: [
                {
                  source: 'team',
                  agent: 'critic',
                  model: 'demo/worker',
                  effort: '',
                },
              ],
            },
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
      calls(operation, 'setup.update').at(-1)[1].workflows[0].stages[1].rounds,
    ).toBe(3);
    state.handlers['setup.update'] = () =>
      Promise.reject(new Error('test-owned write failure'));
    fill('factory-base-branch', 'release');
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    bridge.invalidate({ resource: 'cards', ids: ['fct-a'], revision: 2 });
    await settle();
    expect(document.getElementById('factory-base-branch').value).toBe(
      'release',
    );
    expect(calls(operation, 'setup.get')).toHaveLength(1);
    delete state.handlers['setup.update'];
    expect(await bridge.autosave.flush()).toBe(true);
    expect(state.setups[0].base_branch).toBe('release');
    const writes = calls(operation, 'setup.update').length;
    state.setups = [];
    bridge.invalidate({ resource: 'setups', ids: ['project-a'], revision: 0 });
    await settle();
    expect(button(t('factory.createSetup'))).toBeDefined();
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls(operation, 'setup.update')).toHaveLength(writes);
  });

  it('edits stages and lanes, refuses what the server would refuse and keeps the saved Workflow', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge, '/setups/project-a');
    const location = (stageName) =>
      t('factory.location.stage', { workflow: 'Implement', stage: stageName });
    const stageNode = (id) =>
      document.querySelector(`#factory-workflow-panel [data-stage-id="${id}"]`);
    expect(stageIds()).toEqual(['implement', 'review']);
    button(t('factory.addLane'), stageNode('implement')).click();
    await tick();
    await choose('factory-implement-1-agent', 'Critic');
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.integratorLanes', { location: location('Implement') }),
    );
    button(t('factory.removeLane', { lane: 2, stage: 'Implement' })).click();
    await tick();
    document.getElementById('factory-implement-approval').click();
    button(t('factory.addLane'), stageNode('review')).click();
    await tick();
    await choose('factory-review-1-agent', 'Planner');
    button(t('factory.addWorkStage')).click();
    await tick();
    const added = stageIds()[2];
    button(
      t('factory.moveStageUp', { stage: t('factory.newWorkStage') }),
    ).click();
    await tick();
    expect(stageIds()).toEqual(['implement', added, 'review']);
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.laneAgent', {
        location: location(t('factory.newWorkStage')),
      }),
    );
    fill(`factory-${added}-name`, 'Notes');
    await choose(`factory-${added}-0-agent`, 'Builder');
    await choose(`factory-${added}-output`, t('factory.output.document'));
    await choose('factory-review-target', 'Notes');
    expect(await bridge.autosave.flush()).toBe(true);
    const [implement, review] = implementWorkflow.stages;
    expect(
      calls(operation, 'setup.update').at(-1)[1].workflows[0].stages,
    ).toEqual([
      { ...implement, approval: true },
      {
        id: added,
        name: 'Notes',
        kind: 'work',
        instructions: '',
        lanes: [{ source: 'team', agent: 'builder', model: '', effort: '' }],
        approval: false,
        output: 'document',
      },
      {
        ...review,
        target: added,
        lanes: [
          ...review.lanes,
          { source: 'agent', agent: 'planner', model: '', effort: '' },
        ],
      },
    ]);
    bridge.invalidate({ resource: 'setups', ids: ['project-a'], revision: 2 });
    await settle();
    expect(stageIds()).toEqual(['implement', added, 'review']);
    expect(
      document.getElementById('factory-review-target').textContent.trim(),
    ).toBe('Notes');
    expect(
      document
        .getElementById('factory-implement-approval')
        .getAttribute('aria-checked'),
    ).toBe('true');
    button(t('factory.removeStage', { stage: 'Notes' })).click();
    await tick();
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.reviewTarget', { location: location('Review') }),
    );
  });

  it('adds Workflows from templates, previews unsaved lanes and duplicates, renames, defaults and deletes Workflows', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge, '/setups/project-a');
    vi.useFakeTimers();
    const panel = () => document.getElementById('factory-workflow-panel');
    await choose('factory-add-workflow', 'Plan');
    expect(panel().dataset.workflowId).toBe('plan');
    expect(document.getElementById('factory-plan-instructions').value).toBe(
      'test-owned plan instructions',
    );
    expect(await bridge.autosave.flush()).toBe(false);
    await tick();
    expect(editorText()).toContain(
      t('factory.invalid.laneAgent', {
        location: t('factory.location.stage', {
          workflow: 'Plan',
          stage: 'Plan',
        }),
      }),
    );
    await choose('factory-plan-0-agent', 'Drafter');
    await vi.advanceTimersByTimeAsync(800);
    await tick();
    flushSync();
    expect(editorText()).toContain(t('factory.modelUnresolved'));
    await choose('factory-plan-0-agent', 'Planner');
    await vi.advanceTimersByTimeAsync(800);
    await tick();
    flushSync();
    expect(calls(operation, 'setup.preview')).toHaveLength(2);
    expect(editorText()).not.toContain(t('factory.modelUnresolved'));
    expect(
      document
        .querySelector('[data-stage-id="plan"] [data-effective-model]')
        .textContent.trim(),
    ).toBe(t('factory.effectiveModel', { model: 'demo/worker' }));
    await choose('factory-review-0-agent', 'Critic');
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
    expect(payload.workflows[1].stages).toEqual([
      {
        ...templates[0].stages[0],
        lanes: [{ source: 'agent', agent: 'planner', model: '', effort: '' }],
      },
      {
        ...templates[0].stages[1],
        lanes: [{ source: 'team', agent: 'critic', model: '', effort: '' }],
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
    expect(
      document.getElementById(
        document
          .getElementById('factory-review-rounds')
          .getAttribute('aria-describedby'),
      ),
    ).not.toBeNull();
    button(t('factory.createSetup')).click();
    await settle();
    expect(editorText()).toContain(
      t('factory.invalid.laneAgent', {
        location: t('factory.location.stage', {
          workflow: 'Plan',
          stage: 'Plan',
        }),
      }),
    );
    await choose('factory-plan-0-agent', 'Planner');
    await choose('factory-review-0-agent', 'Critic');
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
          stages: [
            { lanes: [{ source: 'agent', agent: 'planner' }] },
            { lanes: [{ source: 'team', agent: 'critic' }] },
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
