// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatActivityPanel } =
  await import('../ChatActivityPanel.svelte');

function subAgentTask({
  id,
  agentId,
  content = 'Review',
  status,
  delivery = 'automatic',
}) {
  const data = {
    id: `sub-${id}`,
    agent_id: agentId,
    session_id: `session-${id}`,
    status,
    delivery,
  };
  return {
    type: 'tool_call',
    id,
    name: 'subagent',
    status: 'success',
    arguments: { action: 'run', agent_id: agentId, content },
    subAgentSession: { ...data, run_id: `run-${id}` },
    result: { ok: true, error: null, data, artifacts: [] },
  };
}

function backgroundBashTask({ id, command, ...fields }) {
  return {
    type: 'tool_call',
    id,
    name: 'bash',
    status: 'success',
    resultEvent: { type: 'tool_call_result' },
    arguments: { command, mode: 'background' },
    result: {
      ok: true,
      error: null,
      data: {
        process_id: `process-${id}`,
        status: 'running',
        delivery: 'automatic',
      },
      artifacts: [],
    },
    ...fields,
  };
}

function lineChangeTool(name, path, added, removed) {
  return {
    type: 'tool_call',
    id: `tool-${name}`,
    name,
    status: 'success',
    arguments: { path },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: `call-${name}`, name } },
    },
    resultEvent: {
      type: 'tool_call_result',
      payload: {
        tool_call: { id: `call-${name}`, name },
        display: {
          version: 1,
          summary: path,
          hidden_argument_keys: [],
          primary: [],
          facts: [
            { kind: 'line_change', change: 'added', value: added },
            { kind: 'line_change', change: 'removed', value: removed },
          ],
        },
      },
    },
  };
}

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

  // Sub-Agent Runs in every state (the running one listed last), a foreground
  // Sub-Agent, and two Bash processes (one failed through its tracked status).
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
      foreground: subAgentTask({
        id: 'foreground',
        agentId: 'planner',
        content: 'Run foreground checks',
        status: 'completed',
        delivery: 'inline',
      }),
      running: subAgentTask({
        id: 'running',
        agentId: 'builder',
        content: 'Implement the sidebar',
        status: 'running',
      }),
      failedBash: backgroundBashTask({
        id: 'bash-failed',
        command: 'npm test',
      }),
      runningBash: backgroundBashTask({
        id: 'bash-running',
        command: 'npm run dev',
      }),
    };
    return {
      tasks,
      props: {
        timelineItems: [runItem(Object.values(tasks))],
        backgroundBashStatuses: { 'process-bash-failed': 'failed' },
      },
    };
  }

  it('opens the current Session tasks grouped by kind with their states', () => {
    const { props } = sessionTasks();
    mountPanel(props);
    expect(rail().getAttribute('aria-expanded')).toBe('false');
    expect(document.querySelector('.chat-activity__panel')).toBeNull();

    rail().click();
    flushSync();

    expect(rail().getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector('.chat-activity__title')).not.toBeNull();
    const subagents = document.querySelector(
      '.chat-activity__group--subagents',
    );
    const bash = document.querySelector('.chat-activity__group--bash');
    expect([...document.querySelectorAll('.chat-activity__group')]).toEqual([
      subagents,
      bash,
    ]);
    expect(subagents.querySelector('[data-status]').dataset.status).toBe(
      'running',
    );
    expect(bash.open).toBe(false);
    expect(bash.querySelector('.chat-activity__running-count')).not.toBeNull();
    bash.querySelector('summary').click();
    expect(bash.open).toBe(true);

    // Foreground Sub-Agents stay in the timeline only.
    expect(document.body.textContent).not.toContain('Run foreground checks');
    const subAgentRows = Object.fromEntries(
      [...subagents.querySelectorAll('.chat-activity__task-row')].map((row) => [
        row
          .querySelector('.chat-activity__task-name')
          .firstChild.textContent.trim(),
        [
          row
            .querySelector('.chat-activity__task-link')
            .getAttribute('aria-label'),
          row.querySelector('[data-status]').dataset.status,
        ],
      ]),
    );
    expect(subAgentRows).toEqual(
      Object.fromEntries(
        [
          ['builder', 'running', 'running'],
          ['reviewer', 'completed', 'success'],
          ['writer', 'cancelled', 'cancelled'],
          ['tester', 'failed', 'failed'],
        ].map(([agent, statusKey, dot]) => [
          agent,
          [
            t('chat.activity.taskAria', {
              agent,
              status: status(statusKey),
            }),
            dot,
          ],
        ]),
      ),
    );
    expect(
      rowContaining('builder').querySelector('.chat-activity__task-preview')
        .textContent,
    ).toBe('Implement the sidebar');
    expect(
      subagents.querySelector('.chat-activity__running-count').textContent,
    ).toBe(t('chat.activity.runningCount', { count: 1 }));
    // The complete task and each complete command sit in copy cards.
    const cards = Object.fromEntries(
      [...document.querySelectorAll('.copyable-value-card')].map((card) => [
        card.querySelector('.copyable-value-card__value').textContent,
        card.querySelector('button').getAttribute('aria-label'),
      ]),
    );
    expect(cards).toMatchObject({
      'Implement the sidebar': t('chat.subagent.copyTask'),
      'npm run dev': t('chat.copyCommand'),
      'npm test': t('chat.copyCommand'),
    });

    // Bash rows are plain rows that show only their command.
    for (const [command, statusKey, dot] of [
      ['npm run dev', 'running', 'running'],
      ['npm test', 'failed', 'failed'],
    ]) {
      const row = rowContaining(command);
      expect(row.tagName).toBe('DIV');
      expect(row.textContent.replace(/\s+/g, ' ').trim()).toBe(command);
      expect(row.getAttribute('aria-label')).toBe(
        t('chat.activity.bashTaskAria', {
          command,
          status: status(statusKey),
        }),
      );
      expect(row.querySelector(`[data-status="${dot}"]`)).not.toBeNull();
    }

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(document.querySelector('.chat-activity__panel')).toBeNull();
  });

  it('cancels only active work and navigates only from Sub-Agent links', async () => {
    const { tasks, props } = sessionTasks();
    const onNavigateToSubAgent = vi.fn();
    const onCancelSubAgent = vi.fn();
    const onCancelBackgroundProcess = vi.fn();
    openPanel({
      ...props,
      onNavigateToSubAgent,
      onCancelSubAgent,
      onCancelBackgroundProcess,
    });

    const subAgentCancel = document.querySelector(
      '[data-cancel-kind="subagent"]',
    );
    const bashCancel = document.querySelector('[data-cancel-kind="bash"]');
    expect(document.querySelectorAll('.chat-activity__cancel')).toHaveLength(2);
    expect(subAgentCancel.closest('.chat-activity__task-row')).toBe(
      rowContaining('builder'),
    );
    expect(subAgentCancel.getAttribute('aria-label')).toBe(
      t('chat.activity.cancelSubAgentAria', { agent: 'builder' }),
    );
    expect(bashCancel.getAttribute('aria-label')).toBe(
      t('chat.activity.cancelBashAria', { command: 'npm run dev' }),
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
    expect(onCancelBackgroundProcess).toHaveBeenCalledWith({
      processId: 'process-bash-running',
    });
    expect(onNavigateToSubAgent).toHaveBeenCalledTimes(1);
  });

  it('preserves the Bash disclosure choice while running work updates the clock', async () => {
    vi.useFakeTimers();
    openPanel({
      timelineItems: [
        runItem([
          subAgentTask({ id: 'a', agentId: 'alba', status: 'running' }),
          backgroundBashTask({ id: 'b', command: 'npm run dev' }),
        ]),
      ],
    });

    const bash = document.querySelector('details');
    expect(bash.open).toBe(false);
    for (const open of [true, false]) {
      bash.querySelector('summary').click();
      await vi.advanceTimersByTimeAsync(1100);
      flushSync();
      expect(bash.open).toBe(open);
    }
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
    openPanel({ timelineItems: [], reflectionTasks: [] });

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
    (processStatus, dot) => {
      const bash = backgroundBashTask({
        id: 'overlap',
        command: 'npm run build',
      });
      openPanel({
        timelineItems: [
          runItem([bash], 'history-run'),
          runItem([{ ...bash, id: 'live-tool' }], 'live-run'),
        ],
        backgroundBashStatuses: { 'process-overlap': processStatus },
      });

      expect(taskRows()).toHaveLength(1);
      expect(document.querySelector(`[data-status="${dot}"]`)).not.toBeNull();
      expect(
        document.querySelectorAll('[data-cancel-kind="bash"]'),
      ).toHaveLength(processStatus === 'running' ? 1 : 0);
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

  it('shows the aggregated Session change stats above the tasks', () => {
    openPanel({
      timelineItems: [
        runItem([lineChangeTool('edit', 'a.txt', 3, 2)]),
        runItem([lineChangeTool('write', 'b.txt', 5, 0)], 'assistant-run-2'),
      ],
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

    const reflections = document.querySelector(
      '.chat-activity__group--reflections',
    );
    expect(
      reflections.querySelectorAll('.chat-activity__task-row'),
    ).toHaveLength(2);
    expect(reflections.querySelector('[data-status]').dataset.status).toBe(
      'running',
    );
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
    expect(runningRow.textContent).toMatch(/·\s*\d+s/);
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

  it('shows Bash background runtimes from terminal data on panel rows', () => {
    const timing = {
      started_at: '2026-09-04T12:00:00+00:00',
      completed_at: '2026-09-04T12:00:01+00:00',
      duration_ms: 1000,
    };
    openPanel({
      timelineItems: [
        runItem([
          backgroundBashTask({ id: 'done', command: 'npm run build', timing }),
          backgroundBashTask({ id: 'ticking', command: 'npm run dev', timing }),
        ]),
      ],
      backgroundBashProcesses: {
        'process-done': {
          status: 'completed',
          exitCode: 0,
          cancelledByUser: false,
          startedAt: '2026-09-04T12:00:00+00:00',
          finishedAt: '2026-09-04T12:04:12+00:00',
          output: 'built',
          truncated: false,
          logFile: '',
        },
      },
      reflectionTasks: [],
    });

    const finishedRow = rowContaining('npm run build');
    expect(finishedRow.querySelector('[data-status="success"]')).not.toBeNull();
    expect(
      finishedRow.querySelector('.chat-activity__task-time').textContent,
    ).toContain('4m 12s');
    // The running row ticks from the panel's own clock, so only its presence
    // is asserted.
    const runningRow = rowContaining('npm run dev');
    expect(runningRow.querySelector('[data-status="running"]')).not.toBeNull();
    expect(
      runningRow.querySelector('.chat-activity__task-time'),
    ).not.toBeNull();
  });
});
