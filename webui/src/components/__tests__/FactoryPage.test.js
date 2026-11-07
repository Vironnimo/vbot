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
  t,
  tick,
  flushSync,
} from './FactoryPage.support.js';
import { assistantRun, outputItem } from './timelineItems.support.js';

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
      states.map((value, index) => card(value, value, { position: index + 1 })),
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
    button(t('factory.createCard')).click();
    await settle();
    expect(calls(operation, 'card.create').at(-1)[1]).toEqual({
      project_id: 'project-a',
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
  it.each([
    ['proposal', '', ['approveProposal', 'discard']],
    ['queued', '', ['discard']],
    ['working', '', ['discard']],
    ['reviewing', '', ['discard']],
    ['integrating', '', ['discard']],
    ['waiting', 'approved', ['accept', 'sendBack', 'discard']],
    ['waiting', 'no_agreement', ['accept', 'sendBack', 'discard']],
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
    async (state, reason, expected) => {
      const { bridge } = createBridge([
        card('fct-a', state, { waiting_reason: reason }),
      ]);
      await render(bridge, '/card/fct-a');
      const labels = {
        approveProposal: t('factory.approveProposal'),
        discard: t('factory.discard'),
        accept: t('factory.accept'),
        sendBack: t('factory.sendBack'),
        answer: t('factory.answer'),
        retry: t('factory.retry'),
        delete: t('factory.delete'),
      };
      expect(actionButtons()).toEqual(expected.map((key) => labels[key]));
    },
  );

  it('foregrounds the Reviewer recommendation and keeps evidence, provenance, cost and permissions reachable', async () => {
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
        waiting_reason: 'approved',
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
      reviews: [
        {
          round: 1,
          kind: 'review',
          verdict: 'approve',
          findings: ['test-owned finding'],
          reviewed_head_sha: 'head456',
        },
      ],
      events: [
        {
          id: 1,
          event: 'review_submitted',
          actor: 'reviewer',
          at: '2026-11-07T09:00:00Z',
          detail: { state: 'waiting', previous_state: 'reviewing' },
        },
      ],
    };
    await render(bridge, '/card/fct-a');
    expect(
      document.querySelector('[data-recommendation]').textContent,
    ).toContain('test-owned recommendation');
    expect(document.querySelector('[data-checks]').textContent).toContain(
      'test-owned checks',
    );
    expect(document.querySelector('[data-submission]').textContent).toContain(
      'base123..head456',
    );
    expect(
      document.querySelector('[data-review-round="1"]').textContent,
    ).toContain('test-owned finding');
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
    button(t('factory.accept')).click();
    await settle();
    expect(calls(operation, 'card.accept').at(-1)[1]).toEqual({ id: 'fct-a' });
    expect(document.querySelector('[data-state]').dataset.state).toBe(
      'integrating',
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
    ['approved', 'sendBack', 'comment'],
  ])(
    'submits a %s response without replacing its draft on invalidation',
    async (reason, action, field) => {
      const { bridge, operation } = createBridge([
        card('fct-a', 'waiting', {
          waiting_reason: reason,
          runtime: { question: 'test-owned question' },
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

  it('confirms destructive actions in navigation layers, deletes only final cards and repairs their route', async () => {
    const { bridge, operation, state } = createBridge([card('fct-a', 'done')]);
    await render(bridge, '/card/fct-a');
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

  it('uses Team and Model pickers, warns about equal effective Models, waits for number completion and keeps failed setup drafts', async () => {
    const { bridge, operation, state } = createBridge();
    await render(bridge, '/setups/project-a');
    await choose('factory-reviewer_model', 'demo/worker');
    await bridge.autosave.flush();
    await tick();
    expect(
      document.querySelector('.factory-setup-editor').textContent,
    ).toContain(t('factory.sameModel'));
    expect(calls(operation, 'setup.update').at(-1)[1]).toMatchObject({
      expected_revision: 1,
      reviewer_model: 'demo/worker',
      worker_profile: 'builder',
      reviewer_profile: 'critic',
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

  it('creates setups explicitly at revision zero and resets the editor after deletion', async () => {
    const { bridge, operation, state } = createBridge();
    state.setups = [];
    await render(bridge, '/setups/project-b');
    expect(button(t('factory.createSetup'))).toBeDefined();
    expect(calls(operation, 'setup.update')).toHaveLength(0);
    vi.useFakeTimers();
    await choose('factory-worker_model', t('factory.inheritModel'));
    await choose('factory-reviewer_model', t('factory.inheritModel'));
    await choose('factory-reviewer_profile', 'Builder');
    await vi.advanceTimersByTimeAsync(800);
    await tick();
    flushSync();
    expect(calls(operation, 'setup.preview')).toHaveLength(1);
    expect(
      document.querySelector('.factory-setup-editor').textContent,
    ).toContain(t('factory.sameModel'));
    expect(calls(operation, 'setup.update')).toHaveLength(0);
    button(t('factory.createSetup')).click();
    await settle();
    expect(calls(operation, 'setup.update').at(-1)[1]).toMatchObject({
      project_id: 'project-b',
      expected_revision: 0,
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
          participant_id: 'worker',
          address: {
            project_id: 'project-a',
            agent_id: 'owned-worker',
            session_id: 'ses-worker',
          },
          model: 'demo/worker',
          run_active: true,
          lifecycle_run_id: 'run-worker',
        },
      ],
      runs: [
        { run_id: 'run-worker', participant_id: 'worker', status: 'running' },
      ],
    };
    await render(bridge, '/card/fct-a');
    button(t('factory.openSession')).click();
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledWith('fct-a', 'worker', {
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
