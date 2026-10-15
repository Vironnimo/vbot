// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { reflectionTaskRows } from '../../../lib/chatTimelinePresentation.js';
import { init, t } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatActivityPanel } =
  await import('../ChatActivityPanel.svelte');

function subAgentTask({
  id,
  agentId,
  content = 'Review',
  description = '',
  status,
}) {
  const data = {
    id: `sub-${id}`,
    agent_id: agentId,
    session_id: `session-${id}`,
    status,
  };
  return {
    type: 'tool_call',
    id,
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: agentId,
      content,
      ...(description ? { description } : {}),
    },
    subAgentSession: { ...data, run_id: `run-${id}` },
    result: { ok: true, error: null, data, artifacts: [] },
  };
}

// A `bash` call whose command `cmd_<id>` went on running in the background.
function backgroundCommandTask({ id, command, ...fields }) {
  return {
    type: 'tool_call',
    id,
    name: 'bash',
    status: 'success',
    resultEvent: { type: 'tool_call_result' },
    arguments: { command },
    result: {
      ok: true,
      error: null,
      data: {
        status: 'running',
        command_id: `cmd_${id}`,
        output: '',
        next: 'The result arrives as a new message.',
      },
      artifacts: [],
    },
    ...fields,
  };
}

// The state of a background command as the Chat holds it.
const commandState = (status, stillRunning = false) => ({
  status,
  stillRunning,
});

function runItem(items, id = 'assistant-run') {
  return { id, type: 'assistant_run', items };
}

function rail() {
  return document.querySelector('.chat-activity__rail');
}

function taskRows() {
  return [...document.querySelectorAll('.chat-activity__task-row')];
}

function rowContaining(text) {
  return taskRows().find((row) => row.textContent.includes(text));
}

const status = (key) => t(`chat.activity.status.${key}`);

// Panel rows of finished reviews, as Chat projects them from its Session
// state: tracking entries with their outcome, and per-review details.
function reviewRows(reviews) {
  const reflectionTasks = {};
  const reflectionDetails = {};
  for (const [index, review] of reviews.entries()) {
    reflectionTasks[review.runId] = {
      sessionId: `fork-${review.runId}`,
      runKind: 'reflection',
      status: review.status ?? 'completed',
      startedAt: `2026-10-02T10:0${index}:00.000Z`,
      ...(review.outcome ? { outcome: review.outcome } : {}),
    };
    if (review.details) {
      reflectionDetails[review.runId] = review.details;
    }
  }
  return reflectionTaskRows({ reflectionTasks, reflectionDetails });
}

function outcomeToggle(row) {
  return [...document.querySelectorAll('.chat-activity__outcome-toggle')].find(
    (button) => button.id.endsWith(`-${row.runId}-outcome`),
  );
}

function reviewChanges(row) {
  return document.getElementById(
    outcomeToggle(row).getAttribute('aria-controls'),
  );
}

const SAVED_CHANGES = [
  {
    store: 'memory',
    kind: 'added',
    revisions: [4],
    undone: false,
    scope: 'user',
    text: 'Prefers short answers.',
  },
  {
    store: 'skill',
    kind: 'created',
    revisions: [7, 8],
    undone: false,
    skill: 'deploy',
    files: ['SKILL.md', 'references/hosts.md'],
  },
  {
    store: 'skill',
    kind: 'archived',
    revisions: [9],
    undone: true,
    skill: 'old-deploy',
    files: [],
    absorbed_into: 'deploy',
    followed: [{ kind: 'calendar', id: 'act-1', name: 'Weekly review' }],
  },
];

describe('ChatActivityPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  function mountPanel(props = {}) {
    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props,
    });
    flushSync();
  }

  function openPanel(props) {
    mountPanel(props);
    rail().click();
    flushSync();
  }

  // Sub-Agent Runs in every state (the running one listed last), a message to
  // one of them, and three background commands (one failed through its
  // tracked status, one the server no longer reports).
  function sessionTasks() {
    const tasks = {
      completed: subAgentTask({
        id: 'completed',
        agentId: 'reviewer',
        status: 'completed',
      }),
      cancelled: subAgentTask({
        id: 'cancelled',
        agentId: 'writer',
        status: 'cancelled',
      }),
      failed: subAgentTask({
        id: 'failed',
        agentId: 'tester',
        status: 'failed',
      }),
      message: {
        type: 'tool_call',
        id: 'message',
        name: 'subagent',
        status: 'success',
        arguments: {
          action: 'send',
          id: 'sub-completed',
          content: 'Also check the docs',
        },
        result: {
          ok: true,
          error: null,
          data: {
            id: 'sub-completed',
            agent_id: 'reviewer',
            session_id: 'session-completed',
            status: 'queued',
          },
          artifacts: [],
        },
      },
      running: subAgentTask({
        id: 'running',
        agentId: 'builder',
        content: 'Implement the sidebar',
        description: 'Sidebar work',
        status: 'running',
      }),
      endedBash: backgroundCommandTask({
        id: 'bash-ended',
        command: 'npm run lint',
      }),
      failedBash: backgroundCommandTask({
        id: 'bash-failed',
        command: 'npm test',
      }),
      runningBash: backgroundCommandTask({
        id: 'bash-running',
        command: 'npm run dev',
      }),
    };
    return {
      tasks,
      props: {
        timelineItems: [runItem(Object.values(tasks))],
        backgroundCommandStatuses: {
          'cmd_bash-failed': commandState('failed'),
          'cmd_bash-running': commandState('running'),
        },
      },
    };
  }

  it('lists running work above finished work, each row naming its kind and state', () => {
    const { props } = sessionTasks();
    mountPanel(props);
    expect(rail().getAttribute('aria-expanded')).toBe('false');
    expect(document.querySelector('.chat-activity__panel')).toBeNull();

    rail().click();
    flushSync();

    expect(rail().getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector('.chat-activity__title')).not.toBeNull();
    const running = document.querySelector('.chat-activity__group--running');
    const finished = document.querySelector('.chat-activity__group--finished');
    expect([...document.querySelectorAll('.chat-activity__group')]).toEqual([
      running,
      finished,
    ]);
    const sectionRows = (section) =>
      [...section.querySelectorAll('.chat-activity__task-row')].map((row) => [
        row.querySelector('.chat-activity__task-title').textContent.trim(),
        row.querySelector('.chat-activity__task-kind').textContent,
        row.querySelector('[data-status]').dataset.status,
      ]);
    const subagent = t('chat.activity.kind.subagent');
    const bash = t('chat.activity.kind.command');
    // Work started later comes first; kinds mix within a section.
    expect(sectionRows(running)).toEqual([
      ['npm run dev', bash, 'running'],
      ['Sidebar work', subagent, 'running'],
    ]);
    expect(sectionRows(finished)).toEqual([
      ['npm test', bash, 'failed'],
      // A command without a reported status has ended, never runs.
      ['npm run lint', bash, 'ended'],
      ['tester', subagent, 'failed'],
      ['writer', subagent, 'cancelled'],
      ['reviewer', subagent, 'success'],
    ]);

    // A message to a Sub-Agent stays in the timeline only.
    expect(document.body.textContent).not.toContain('Also check the docs');
    for (const [agent, statusKey] of [
      ['builder', 'running'],
      ['reviewer', 'completed'],
      ['writer', 'cancelled'],
      ['tester', 'failed'],
    ]) {
      expect(
        rowContaining(agent)
          .querySelector('.chat-activity__task-link')
          .getAttribute('aria-label'),
      ).toBe(t('chat.activity.taskAria', { agent, status: status(statusKey) }));
    }
    // A Sub-Agent shows its title, never its task; its Agent moves to the
    // second line.
    const builder = rowContaining('Sidebar work');
    expect(
      builder.querySelector('.chat-activity__task-description').textContent,
    ).toBe('Sidebar work');
    expect(builder.querySelector('.chat-activity__task-name').textContent).toBe(
      'builder',
    );
    expect(
      rowContaining('reviewer').querySelector(
        '.chat-activity__task-description',
      ),
    ).toBeNull();
    expect(document.body.textContent).not.toContain('Implement the sidebar');
    // Each complete command sits in a copy card.
    const cards = Object.fromEntries(
      [...document.querySelectorAll('.copyable-value-card')].map((card) => [
        card.querySelector('.copyable-value-card__value').textContent,
        card.querySelector('button').getAttribute('aria-label'),
      ]),
    );
    expect(cards).toMatchObject({
      'npm run dev': t('chat.copyCommand'),
      'npm test': t('chat.copyCommand'),
      'npm run lint': t('chat.copyCommand'),
    });

    // Bash rows are plain rows that show their command and kind.
    for (const [command, statusKey] of [
      ['npm run dev', 'running'],
      ['npm test', 'failed'],
      ['npm run lint', 'ended'],
    ]) {
      const row = rowContaining(command);
      expect(row.tagName).toBe('DIV');
      expect(row.getAttribute('aria-label')).toBe(
        t('chat.activity.commandTaskAria', {
          command,
          status: status(statusKey),
        }),
      );
    }

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(document.querySelector('.chat-activity__panel')).toBeNull();
  });

  it('closes from its header button and moves focus between rail and panel', () => {
    mountPanel();
    rail().focus();
    rail().click();
    flushSync();

    const close = document.querySelector('.chat-activity__close');
    expect(document.activeElement).toBe(close);
    expect(close.getAttribute('aria-label')).toBe(t('chat.activity.close'));
    close.click();
    flushSync();
    expect(document.querySelector('.chat-activity__panel')).toBeNull();
    expect(document.activeElement).toBe(rail());
  });

  it('cancels only active work and navigates only from Sub-Agent links', async () => {
    const { tasks, props } = sessionTasks();
    const onNavigateToSubAgent = vi.fn();
    const onCancelSubAgent = vi.fn();
    const onStopBackgroundCommand = vi.fn();
    openPanel({
      ...props,
      onNavigateToSubAgent,
      onCancelSubAgent,
      onStopBackgroundCommand,
    });

    const subAgentCancel = document.querySelector(
      '[data-cancel-kind="subagent"]',
    );
    const bashCancel = document.querySelector('[data-cancel-kind="command"]');
    expect(document.querySelectorAll('.chat-activity__cancel')).toHaveLength(2);
    expect(subAgentCancel.closest('.chat-activity__task-row')).toBe(
      rowContaining('builder'),
    );
    expect(subAgentCancel.getAttribute('aria-label')).toBe(
      t('chat.activity.cancelSubAgentAria', { agent: 'builder' }),
    );
    expect(bashCancel.getAttribute('aria-label')).toBe(
      t('chat.activity.cancelCommandAria', { command: 'npm run dev' }),
    );

    rowContaining('npm run dev').click();
    expect(onNavigateToSubAgent).not.toHaveBeenCalled();
    rowContaining('builder').querySelector('.chat-activity__task-link').click();
    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'builder',
      sessionId: 'session-running',
    });

    subAgentCancel.click();
    bashCancel.click();
    await Promise.resolve();
    expect(onCancelSubAgent).toHaveBeenCalledWith({ tool: tasks.running });
    expect(onStopBackgroundCommand).toHaveBeenCalledWith({
      commandId: 'cmd_bash-running',
    });
    expect(onNavigateToSubAgent).toHaveBeenCalledTimes(1);
  });

  it('keeps panel identities separate in split Chat and restores focus on Escape', async () => {
    mountPanel();
    const second = mount(ChatActivityPanel, { target: document.body });
    try {
      flushSync();
      const toggles = [...document.querySelectorAll('.chat-activity__rail')];
      toggles.forEach((button) => button.click());
      flushSync();
      const panels = [...document.querySelectorAll('.chat-activity__panel')];
      expect(new Set(panels.map((panel) => panel.id)).size).toBe(2);
      panels.forEach((panel, index) => {
        expect(toggles[index].getAttribute('aria-controls')).toBe(panel.id);
        expect(
          document.getElementById(panel.getAttribute('aria-labelledby')),
        ).not.toBeNull();
      });
      panels[1].setAttribute('tabindex', '-1');
      panels[1].focus();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      flushSync();
      expect(document.activeElement).toBe(toggles[1]);
    } finally {
      await unmount(second);
    }
  });

  it('shows calm empty states when the Session has no work or changes', () => {
    openPanel({ timelineItems: [], reflectionTasks: [], sessionStats: null });

    expect(
      document.querySelector('.chat-activity__empty').textContent,
    ).toContain(t('chat.activity.empty'));
    expect(
      document.querySelector('.chat-activity__stats-empty').textContent.trim(),
    ).toBe(t('chat.activity.statsEmpty'));
    expect(
      document.querySelector(
        '.chat-activity__group, .chat-activity__parent-link',
      ),
    ).toBeNull();
  });

  it.each([
    ['running', 'running'],
    ['completed', 'success'],
  ])(
    'lists overlapping History and live background work once in %s state',
    (commandStatus, dot) => {
      const bash = backgroundCommandTask({
        id: 'overlap',
        command: 'npm run build',
      });
      openPanel({
        timelineItems: [
          runItem([bash], 'history-run'),
          runItem([{ ...bash, id: 'live-tool' }], 'live-run'),
        ],
        backgroundCommandStatuses: {
          cmd_overlap: commandState(commandStatus),
        },
      });

      expect(taskRows()).toHaveLength(1);
      expect(document.querySelector(`[data-status="${dot}"]`)).not.toBeNull();
      expect(
        document.querySelectorAll('[data-cancel-kind="command"]'),
      ).toHaveLength(commandStatus === 'running' ? 1 : 0);
      rail().click();
      flushSync();
      expect(document.querySelector('.chat-activity__panel')).toBeNull();
    },
  );

  it('shows the resolved Parent Session as a navigation link', async () => {
    const onNavigateToParentSession = vi.fn();
    const target = {
      agentAddress: 'alpha',
      sessionId: 'source-session',
      isSubAgentSession: false,
    };
    openPanel({
      timelineItems: [],
      parentSession: { displayName: 'Original research', target },
      onNavigateToParentSession,
    });

    expect(
      document.querySelector('.chat-activity__parent h3').textContent.trim(),
    ).toBe(t('chat.activity.parentSession'));
    const parentLink = document.querySelector('.chat-activity__parent-link');
    expect(parentLink.textContent.trim()).toBe('Original research');
    expect(parentLink.getAttribute('aria-label')).toBe(
      t('chat.activity.openParentSession', {
        session: 'Original research',
      }),
    );

    parentLink.click();
    await Promise.resolve();
    expect(onNavigateToParentSession).toHaveBeenCalledWith(target);
  });

  it('asks for the Session change stats while open and shows them', () => {
    const onSessionStatsWanted = vi.fn();
    mountPanel({ onSessionStatsWanted });
    flushSync();
    expect(onSessionStatsWanted).toHaveBeenLastCalledWith(false);

    rail().click();
    flushSync();
    expect(onSessionStatsWanted).toHaveBeenLastCalledWith(true);
    // Until the first read answers, the section claims neither changes nor none.
    expect(document.querySelector('.chat-activity__stats-value')).toBeNull();
    expect(document.querySelector('.chat-activity__stats-empty')).toBeNull();
    rail().click();
    flushSync();
    expect(onSessionStatsWanted).toHaveBeenLastCalledWith(false);
  });

  it('shows the Session change stats above the tasks', () => {
    openPanel({
      sessionStats: {
        files: 2,
        added: 8,
        removed: 2,
        fileStats: [
          { path: 'a.txt', added: 3, removed: 2 },
          { path: 'b.txt', added: 5, removed: 0 },
        ],
      },
    });

    const filesChanged = `${t('chat.changeStats.filesMany', { count: 2 })},`;
    const statsValue = document.querySelector('.chat-activity__stats-value');
    expect(
      [...statsValue.querySelectorAll('.change-stats__part')].map(
        (part) => part.textContent,
      ),
    ).toEqual([filesChanged, '+8', '-2']);
    expect(document.querySelector('.chat-activity__stats-empty')).toBeNull();
  });

  it('lists reflection reviews with navigation and no cancel control', async () => {
    const onOpenReflection = vi.fn();
    const onNavigateToSubAgent = vi.fn();
    const runningReview = {
      runId: 'run-refl-running',
      sessionId: 'session-fork-running',
      runKind: 'memory_reflection',
      scope: 'memory',
      status: 'running',
      startedAt: new Date(Date.now() - 30_000).toISOString(),
    };
    const finishedReview = {
      runId: 'run-refl-done',
      sessionId: 'session-fork-done',
      runKind: 'skill_reflection',
      scope: 'skill',
      status: 'completed',
      startedAt: '2026-08-24T10:00:00.000Z',
    };
    openPanel({
      timelineItems: [],
      reflectionTasks: [finishedReview, runningReview],
      onOpenReflection,
      onNavigateToSubAgent,
    });

    expect(taskRows()).toHaveLength(2);
    expect(
      document
        .querySelector('.chat-activity__group--running')
        .querySelector('[data-status]').dataset.status,
    ).toBe('running');
    const memoryScope = t('chat.activity.reflectionScope.memory');
    const runningRow = rowContaining(memoryScope);
    const runningLink = runningRow.querySelector('.chat-activity__task-link');
    expect(runningLink.getAttribute('aria-label')).toBe(
      t('chat.activity.reflectionOpenAria', {
        scope: memoryScope,
        status: status('running'),
      }),
    );
    expect(runningRow.querySelector('[data-status="running"]')).not.toBeNull();
    // Running reviews show coarse elapsed time from their start timestamp.
    expect(runningRow.textContent).toMatch(/\d+s/);
    const finishedRow = rowContaining(t('chat.activity.reflectionScope.skill'));
    expect(finishedRow.textContent).not.toMatch(/\d+s/);
    expect(finishedRow.querySelector('[data-status="success"]')).not.toBeNull();
    // Reviews are server-owned background Runs: navigation, but no cancel.
    expect(document.querySelector('.chat-activity__cancel')).toBeNull();

    runningLink.click();
    await Promise.resolve();
    expect(onOpenReflection).toHaveBeenCalledWith(runningReview);
    expect(onNavigateToSubAgent).not.toHaveBeenCalled();
    finishedRow.querySelector('.chat-activity__task-link').click();
    await Promise.resolve();
    expect(onOpenReflection).toHaveBeenCalledWith(finishedReview);
  });

  it('summarizes what each finished review changed in one quiet line', () => {
    openPanel({
      timelineItems: [],
      reflectionTasks: reviewRows([
        { runId: 'running', status: 'running' },
        { runId: 'nothing', outcome: { memory: 0, skills: 0, undone: false } },
        { runId: 'saved', outcome: { memory: 2, skills: 1, undone: false } },
        { runId: 'skills', outcome: { memory: 0, skills: 1, undone: false } },
        { runId: 'undone', outcome: { memory: 1, skills: 0, undone: true } },
        { runId: 'unknown' },
      ]),
    });

    const lines = [...document.querySelectorAll('.chat-activity__outcome')];
    // Newest first; a running review and one without a reported outcome
    // show no summary.
    expect(lines.map((line) => line.textContent.trim())).toEqual([
      t('chat.activity.outcome.undone'),
      '1 Skill changed',
      '2 Memory entries, 1 Skill changed',
      t('chat.activity.outcome.nothing'),
    ]);
    // A review that saved nothing has nothing to open or undo.
    expect(lines.at(-1).tagName).toBe('SPAN');
    expect(
      lines.slice(0, -1).map((line) => line.getAttribute('aria-expanded')),
    ).toEqual(['false', 'false', 'false']);
    expect(document.querySelector('.chat-activity__changes')).toBeNull();
  });

  it('opens a review to its changes and undoes it after a confirmation', async () => {
    const onLoadReflectionChanges = vi.fn();
    const onUndoReflection = vi.fn().mockResolvedValue(true);
    const rows = reviewRows([
      {
        runId: 'saved',
        outcome: { memory: 1, skills: 2, undone: false },
        details: { changes: SAVED_CHANGES },
      },
      {
        runId: 'undone',
        outcome: { memory: 1, skills: 2, undone: true },
        details: { changes: SAVED_CHANGES },
      },
      {
        runId: 'blocked',
        outcome: { memory: 1, skills: 0, undone: false },
        details: {
          changes: SAVED_CHANGES.slice(0, 1),
          undoError: {
            conflict: {
              store: 'memory',
              revision: 4,
              scope: 'user',
              text: 'Prefers short answers.',
              later: { revision: 6, at: '2026-10-02T10:30:00Z', actor: 'rpc' },
            },
            message: 'Nothing was undone.',
          },
        },
      },
    ]);
    const [saved, undone, blocked] = ['saved', 'undone', 'blocked'].map(
      (runId) => rows.find((row) => row.runId === runId),
    );
    openPanel({
      timelineItems: [],
      reflectionTasks: rows,
      onLoadReflectionChanges,
      onUndoReflection,
    });

    outcomeToggle(saved).click();
    flushSync();

    expect(outcomeToggle(saved).getAttribute('aria-expanded')).toBe('true');
    expect(onLoadReflectionChanges).toHaveBeenCalledExactlyOnceWith(saved);
    const changes = reviewChanges(saved);
    expect(
      [...changes.querySelectorAll('.chat-activity__change')].map((item) =>
        item.textContent.replace(/\s+/g, ' ').trim(),
      ),
    ).toEqual([
      'Added User Memory Prefers short answers.',
      'Created deploy SKILL.md, references/hosts.md',
      'Archived old-deploy Undone Merged into deploy · Planned moves: calendar event “Weekly review”. Individual moves can fail.',
    ]);

    // The undo asks first; Keep returns to the summary without undoing.
    const undoButton = () => changes.querySelector('.chat-activity__undo');
    undoButton().click();
    flushSync();
    expect(undoButton()).toBeNull();
    const confirmation = changes.querySelector('.chat-activity__undo-confirm');
    expect(confirmation.getAttribute('aria-label')).toBe(
      t('chat.activity.undoConfirm'),
    );
    const [, keep] = confirmation.querySelectorAll('button');
    keep.click();
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(outcomeToggle(saved)),
    );
    expect(onUndoReflection).not.toHaveBeenCalled();

    undoButton().click();
    flushSync();
    changes.querySelector('.chat-activity__undo-confirm button').click();
    await vi.waitFor(() =>
      expect(changes.querySelector('.chat-activity__undo-confirm')).toBeNull(),
    );
    expect(onUndoReflection).toHaveBeenCalledExactlyOnceWith(saved);

    // An undone review lists what it took back and offers no second undo.
    outcomeToggle(undone).click();
    flushSync();
    expect(
      reviewChanges(undone).querySelector('.chat-activity__undo'),
    ).toBeNull();

    // A refused undo says which later change blocks it, in place.
    outcomeToggle(blocked).click();
    flushSync();
    const refusal = reviewChanges(blocked).querySelector('[role="alert"]');
    expect(refusal.textContent).toContain(
      'Nothing was undone: the User Memory entry “Prefers short answers.” was changed again later by you (',
    );
  });

  it("shows a Sub-Agent's full title, status details and remaining work when hovering anywhere on its row", async () => {
    const onSubAgentWorkWanted = vi.fn();
    openPanel({
      onSubAgentWorkWanted,
      subAgentWork: {
        'sub-a': [{ kind: 'subagent', id: 'sub_inner', label: 'Inner worker' }],
      },
      timelineItems: [
        runItem([
          subAgentTask({
            id: 'a',
            agentId: 'alba',
            description: 'Module check',
            status: 'running',
          }),
        ]),
      ],
      subAgentStatuses: { 'runTool:run-a': 'bash' },
    });

    vi.useFakeTimers();
    rowContaining('alba')
      .querySelector('.chat-activity__task-description')
      .dispatchEvent(new Event('pointerenter', { bubbles: true }));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);

    const tooltipElement = document.getElementById('app-tooltip');
    expect(
      tooltipElement.querySelector('.app-tooltip__title')?.textContent,
    ).toBe(t('chat.toolState.running'));
    // The one-line title shows in full on hover.
    expect(
      tooltipElement.querySelector('.app-tooltip__text')?.textContent,
    ).toBe('Module check');
    const rows = Array.from(tooltipElement.querySelectorAll('dt'), (term) => [
      term.textContent,
      term.nextElementSibling.textContent,
    ]);
    expect(rows).toContainEqual([t('chat.details.latestTool'), 'bash']);
    expect(rows).toContainEqual([
      t('chat.details.runningSubAgent'),
      'Inner worker',
    ]);
    // Looking at the row asks for its current work.
    expect(onSubAgentWorkWanted).toHaveBeenCalledWith({
      tool: expect.objectContaining({ id: 'a' }),
    });
  });

  it('keeps a Sub-Agent whose Run ended running while commands below it run, and stops them from its row', async () => {
    const finished = subAgentTask({
      id: 'a',
      agentId: 'alba',
      status: 'completed',
    });
    const onStopSubAgentWork = vi.fn();
    const onCancelSubAgent = vi.fn();
    const devServer = { kind: 'command', id: 'cmd_dev', label: 'npm run dev' };
    openPanel({
      timelineItems: [runItem([finished])],
      subAgentCommands: { 'sub-a': [devServer] },
      // Its last inspection found the same command and a terminal.
      subAgentWork: {
        'sub-a': [devServer, { kind: 'terminal', id: 'term_1', label: 'bash' }],
      },
      onStopSubAgentWork,
      onCancelSubAgent,
    });

    const row = rowContaining('alba');
    expect(row.closest('.chat-activity__group--running')).not.toBeNull();
    expect(row.querySelector('[data-status="running"]')).not.toBeNull();
    expect(row.querySelector('.chat-activity__task-time').textContent).toBe(
      `· ${t('chat.subAgentCommandsRunning')}`,
    );
    expect(
      row.querySelector('.chat-activity__task-link').getAttribute('aria-label'),
    ).toBe(
      t('chat.activity.taskAria', {
        agent: 'alba',
        status: status('commandsRunning'),
      }),
    );

    vi.useFakeTimers();
    row.dispatchEvent(new Event('pointerenter', { bubbles: true }));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    const tooltipElement = document.getElementById('app-tooltip');
    expect(
      tooltipElement.querySelector('.app-tooltip__title')?.textContent,
    ).toBe(status('commandsRunning'));
    const details = Array.from(
      tooltipElement.querySelectorAll('dt'),
      (term) => [term.textContent, term.nextElementSibling.textContent],
    );
    expect(details).toContainEqual([t('chat.details.openTerminal'), 'bash']);
    // The command its inspection found too shows once.
    expect(
      details.filter(([label]) => label === t('chat.details.runningCommand')),
    ).toEqual([[t('chat.details.runningCommand'), 'npm run dev']]);
    vi.useRealTimers();

    const stop = row.querySelector('[data-cancel-kind="subagent"]');
    expect(stop.getAttribute('aria-label')).toBe(
      t('chat.activity.stopSubAgentWorkAria', { agent: 'alba' }),
    );
    stop.click();
    await Promise.resolve();
    expect(onStopSubAgentWork).toHaveBeenCalledWith({ tool: finished });
    expect(onCancelSubAgent).not.toHaveBeenCalled();
  });

  it('shows the further of the live and durable command states, times and stop controls on panel rows', () => {
    const timing = {
      started_at: '2026-09-04T12:00:00+00:00',
      completed_at: '2026-09-04T12:00:01+00:00',
      duration_ms: 1000,
    };
    const onStopBackgroundCommand = vi.fn();
    openPanel({
      timelineItems: [
        runItem([
          backgroundCommandTask({
            id: 'stopped',
            command: 'npm run build',
            timing,
          }),
          backgroundCommandTask({
            id: 'ticking',
            command: 'npm run dev',
            timing,
          }),
          backgroundCommandTask({
            id: 'settled',
            command: 'npm run lint',
            timing,
          }),
          // The shell exited while a process it started still runs.
          backgroundCommandTask({
            id: 'lingering',
            command: 'npm run serve &',
            timing,
            result: {
              ok: true,
              error: null,
              data: {
                status: 'exited',
                exit_code: 0,
                command_id: 'cmd_lingering',
                still_running: ['node (PID 4242)'],
                output: '',
              },
              artifacts: [],
            },
          }),
        ]),
      ],
      backgroundCommandStatuses: {
        cmd_stopped: commandState('running'),
        cmd_ticking: commandState('running'),
        cmd_settled: commandState('completed'),
        cmd_lingering: commandState('completed', true),
      },
      // An end beats a running, whichever of the two reports it.
      commandStatuses: {
        cmd_stopped: commandState('stopped'),
        cmd_settled: commandState('running'),
      },
      reflectionTasks: [],
      onStopBackgroundCommand,
    });

    const stoppedRow = rowContaining('npm run build');
    expect(
      stoppedRow.querySelector('[data-status="cancelled"]'),
    ).not.toBeNull();
    expect(
      stoppedRow.querySelector('.chat-activity__task-time').textContent,
    ).toContain(t('chat.toolCancelled'));
    expect(
      rowContaining('npm run lint').querySelector('[data-status="success"]'),
    ).not.toBeNull();
    // The running row ticks from the panel's own clock, so only its presence
    // is asserted.
    const runningRow = rowContaining('npm run dev');
    expect(runningRow.querySelector('[data-status="running"]')).not.toBeNull();
    expect(
      runningRow.querySelector('.chat-activity__task-time'),
    ).not.toBeNull();
    // Processes left running count as running work and can be stopped.
    const lingeringRow = rowContaining('npm run serve &');
    expect(
      lingeringRow.closest('.chat-activity__group--running'),
    ).not.toBeNull();
    expect(lingeringRow.getAttribute('aria-label')).toBe(
      t('chat.activity.commandTaskAria', {
        command: 'npm run serve &',
        status: status('stillRunning'),
      }),
    );
    expect(
      lingeringRow.querySelector('.chat-activity__task-time').textContent,
    ).toBe(`· ${t('chat.toolStillRunning')}`);
    expect(
      [...document.querySelectorAll('[data-cancel-kind="command"]')].map(
        (button) => button.closest('.chat-activity__task-row'),
      ),
    ).toEqual(expect.arrayContaining([runningRow, lingeringRow]));
    expect(
      document.querySelectorAll('[data-cancel-kind="command"]'),
    ).toHaveLength(2);
    lingeringRow.querySelector('[data-cancel-kind="command"]').click();
    expect(onStopBackgroundCommand).toHaveBeenCalledWith({
      commandId: 'cmd_lingering',
    });
  });

  describe('command output', () => {
    const REFRESH_MS = 2000;
    const runningCommand = (props = {}) => ({
      timelineItems: [
        runItem([backgroundCommandTask({ id: 'dev', command: 'npm run dev' })]),
      ],
      backgroundCommandStatuses: { cmd_dev: commandState('running') },
      ...props,
    });
    const outputToggle = () =>
      [...document.querySelectorAll('button')].find(
        (button) =>
          button.getAttribute('aria-label') ===
          t('chat.activity.commandOutput.aria', { command: 'npm run dev' }),
      );
    const outputView = () => {
      const id = outputToggle().getAttribute('aria-controls');
      return id ? document.getElementById(id) : null;
    };
    const shownOutput = () =>
      outputView()?.querySelector('pre')?.textContent ?? null;
    const settle = async (ms = 0) => {
      await vi.advanceTimersByTimeAsync(ms);
      flushSync();
    };

    beforeEach(() => {
      vi.useFakeTimers();
    });

    it('reads the output when opened and every 2 seconds while the command runs, until closed or unmounted', async () => {
      const load = vi.fn(async () => ({
        ok: true,
        status: 'running',
        output: `line ${load.mock.calls.length}`,
      }));
      openPanel(runningCommand({ onLoadCommandOutput: load }));

      expect(outputToggle().getAttribute('aria-expanded')).toBe('false');
      expect(outputView()).toBeNull();
      outputToggle().click();
      await settle();

      expect(outputToggle().getAttribute('aria-expanded')).toBe('true');
      expect(load).toHaveBeenCalledWith('cmd_dev');
      expect(shownOutput()).toBe('line 1');
      await settle(REFRESH_MS);
      expect(load).toHaveBeenCalledTimes(2);
      expect(shownOutput()).toBe('line 2');

      outputToggle().click();
      await settle(REFRESH_MS * 3);
      expect(outputView()).toBeNull();
      expect(load).toHaveBeenCalledTimes(2);

      outputToggle().click();
      await settle();
      expect(load).toHaveBeenCalledTimes(3);
      await unmount(mountedComponent);
      mountedComponent = null;
      await vi.advanceTimersByTimeAsync(REFRESH_MS * 3);
      expect(load).toHaveBeenCalledTimes(3);
    });

    it('stops reading once nothing of the command runs and keeps its final output', async () => {
      const load = vi
        .fn()
        .mockResolvedValueOnce({ ok: true, status: 'running', output: '' })
        .mockResolvedValueOnce({
          ok: true,
          status: 'completed',
          stillRunning: true,
          output: 'serving',
        })
        .mockResolvedValue({ ok: true, status: 'completed', output: 'done' });
      openPanel(runningCommand({ onLoadCommandOutput: load }));

      outputToggle().click();
      await settle();
      expect(outputView().textContent).toContain(
        t('chat.activity.commandOutput.empty'),
      );
      await settle(REFRESH_MS);
      expect(shownOutput()).toBe('serving');
      await settle(REFRESH_MS);
      await settle(REFRESH_MS * 3);

      expect(load).toHaveBeenCalledTimes(3);
      expect(shownOutput()).toBe('done');
    });

    it('shows a failed read in place and stops for a command the server no longer knows', async () => {
      const load = vi
        .fn()
        .mockResolvedValueOnce({ ok: false, error: 'Offline', gone: false })
        .mockResolvedValue({ ok: false, error: 'Unknown', gone: true });
      openPanel(runningCommand({ onLoadCommandOutput: load }));

      outputToggle().click();
      await settle();
      expect(outputView().querySelector('[role="alert"]').textContent).toBe(
        t('chat.activity.commandOutput.loadError', { message: 'Offline' }),
      );

      // While the command runs, the next read tries again.
      await settle(REFRESH_MS);
      await settle(REFRESH_MS * 3);
      expect(load).toHaveBeenCalledTimes(2);
      expect(outputView().querySelector('[role="alert"]')).toBeNull();
      expect(outputView().textContent).toContain(
        t('chat.activity.commandOutput.gone'),
      );
    });
  });
});
