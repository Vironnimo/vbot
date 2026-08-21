// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatActivityPanel } =
  await import('../ChatActivityPanel.svelte');

function subAgentTask({
  id,
  agentId,
  content,
  status,
  delivery = 'automatic',
}) {
  return {
    type: 'tool_call',
    id,
    name: 'subagent',
    status: 'success',
    arguments: { action: 'run', agent_id: agentId, content },
    subAgentSession: {
      id: `sub-${id}`,
      agent_id: agentId,
      session_id: `session-${id}`,
      run_id: `run-${id}`,
      status,
      delivery,
    },
    result: {
      ok: true,
      error: null,
      data: {
        id: `sub-${id}`,
        agent_id: agentId,
        session_id: `session-${id}`,
        status,
        delivery,
      },
      artifacts: [],
    },
  };
}

function backgroundBashTask({ id, command, status = 'running' }) {
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
        session_id: `process-${id}`,
        status,
        delivery: 'automatic',
      },
      artifacts: [],
    },
  };
}

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
  });

  it('opens current Session tasks with cancel controls only for active work', async () => {
    const navigate = vi.fn();
    const cancelSubAgent = vi.fn();
    const cancelBackgroundProcess = vi.fn();
    const running = subAgentTask({
      id: 'running',
      agentId: 'builder',
      content: 'Implement the sidebar',
      status: 'running',
    });
    const completed = subAgentTask({
      id: 'completed',
      agentId: 'reviewer',
      content: 'Review the layout',
      status: 'completed',
    });
    const cancelled = subAgentTask({
      id: 'cancelled',
      agentId: 'writer',
      content: 'Write the release notes',
      status: 'cancelled',
    });
    const failed = subAgentTask({
      id: 'failed',
      agentId: 'tester',
      content: 'Run the browser checks',
      status: 'failed',
    });
    const foreground = subAgentTask({
      id: 'foreground',
      agentId: 'planner',
      content: 'Run foreground checks',
      status: 'completed',
      delivery: 'inline',
    });
    const runningBash = backgroundBashTask({
      id: 'bash-running',
      command: 'npm run dev',
    });
    const failedBash = backgroundBashTask({
      id: 'bash-failed',
      command: 'npm test',
    });

    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: {
        timelineItems: [
          {
            id: 'assistant-run',
            type: 'assistant_run',
            items: [
              completed,
              cancelled,
              failed,
              foreground,
              running,
              failedBash,
              runningBash,
            ],
          },
        ],
        backgroundBashStatuses: { 'process-bash-failed': 'failed' },
        onNavigateToSubAgent: navigate,
        onCancelSubAgent: cancelSubAgent,
        onCancelBackgroundProcess: cancelBackgroundProcess,
      },
    });
    flushSync();

    const rail = document.querySelector('.chat-activity__rail');
    expect(rail).not.toBeNull();
    expect(rail.getAttribute('aria-expanded')).toBe('false');
    expect(document.querySelector('.chat-activity__panel')).toBeNull();

    rail.click();
    flushSync();

    expect(rail.getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector('#chat-activity-title')).not.toBeNull();
    const rows = [...document.querySelectorAll('.chat-activity__task-row')];
    expect(rows).toHaveLength(6);
    const activeGroup = document.querySelector('.chat-activity__group--active');
    const finishedGroup = document.querySelector(
      '.chat-activity__group--finished',
    );
    expect(activeGroup.querySelector('h3')).not.toBeNull();
    expect(
      activeGroup.querySelectorAll('.chat-activity__task-row'),
    ).toHaveLength(2);
    expect(finishedGroup.querySelector('h3')).not.toBeNull();
    expect(
      finishedGroup.querySelectorAll('.chat-activity__task-row'),
    ).toHaveLength(4);
    const runningSubAgentRow = rows.find(
      (row) => row.textContent.trim() === 'builder',
    );
    const runningSubAgentLink = runningSubAgentRow.querySelector(
      '.chat-activity__task-link',
    );
    expect(runningSubAgentLink.getAttribute('aria-label')).toBe(
      'Open builder Session · Working',
    );
    expect(
      runningSubAgentRow.querySelector('[data-status="running"]'),
    ).not.toBeNull();
    const completedRow = rows.find(
      (row) => row.textContent.trim() === 'reviewer',
    );
    expect(
      completedRow
        .querySelector('.chat-activity__task-link')
        .getAttribute('aria-label'),
    ).toBe('Open reviewer Session · Completed');
    expect(
      completedRow.querySelector('[data-status="success"]'),
    ).not.toBeNull();
    const cancelledRow = rows.find(
      (row) => row.textContent.trim() === 'writer',
    );
    expect(
      cancelledRow
        .querySelector('.chat-activity__task-link')
        .getAttribute('aria-label'),
    ).toBe('Open writer Session · Cancelled');
    expect(
      cancelledRow.querySelector('[data-status="cancelled"]'),
    ).not.toBeNull();
    const failedRow = rows.find((row) => row.textContent.trim() === 'tester');
    expect(
      failedRow
        .querySelector('.chat-activity__task-link')
        .getAttribute('aria-label'),
    ).toBe('Open tester Session · Failed');
    expect(failedRow.querySelector('[data-status="failed"]')).not.toBeNull();
    const runningBashRow = rows.find((row) =>
      row.textContent.includes('npm run dev'),
    );
    expect(runningBashRow.tagName).toBe('DIV');
    expect(runningBashRow.textContent.replace(/\s+/g, '')).toBe('$npmrundev');
    expect(runningBashRow.getAttribute('aria-label')).toBe(
      'Bash · npm run dev · Working',
    );
    expect(
      runningBashRow.querySelector('[data-status="running"]'),
    ).not.toBeNull();
    const failedBashRow = rows.find((row) =>
      row.textContent.includes('npm test'),
    );
    expect(failedBashRow.tagName).toBe('DIV');
    expect(failedBashRow.getAttribute('aria-label')).toBe(
      'Bash · npm test · Failed',
    );
    expect(
      failedBashRow.querySelector('[data-status="failed"]'),
    ).not.toBeNull();
    expect(document.querySelectorAll('.chat-activity__cancel')).toHaveLength(2);
    expect(
      runningSubAgentRow
        .querySelector('[data-cancel-kind="subagent"]')
        .getAttribute('aria-label'),
    ).toBe('Cancel builder background task');
    expect(
      runningBashRow
        .querySelector('[data-cancel-kind="bash"]')
        .getAttribute('aria-label'),
    ).toBe('Cancel Bash background process · npm run dev');
    expect(cancelledRow.querySelector('.chat-activity__cancel')).toBeNull();
    expect(failedBashRow.querySelector('.chat-activity__cancel')).toBeNull();
    expect(document.body.textContent).not.toContain('Implement the sidebar');
    expect(document.body.textContent).not.toContain('View session');
    expect(document.body.textContent).not.toContain('Run foreground checks');

    runningBashRow.click();
    expect(navigate).not.toHaveBeenCalled();

    runningSubAgentLink.click();
    expect(navigate).toHaveBeenCalledWith({
      agentId: 'builder',
      sessionId: 'session-running',
    });

    runningSubAgentRow.querySelector('[data-cancel-kind="subagent"]').click();
    runningBashRow.querySelector('[data-cancel-kind="bash"]').click();
    await Promise.resolve();
    expect(cancelSubAgent).toHaveBeenCalledWith({ tool: running });
    expect(cancelBackgroundProcess).toHaveBeenCalledWith({
      processSessionId: 'process-bash-running',
    });
    expect(navigate).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(document.querySelector('.chat-activity__panel')).toBeNull();
  });

  it('shows a calm empty state when the Session has no background work', () => {
    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: { timelineItems: [] },
    });
    flushSync();

    document.querySelector('.chat-activity__rail').click();
    flushSync();

    expect(document.querySelector('.chat-activity__empty')).toBeTruthy();
  });

  it('omits an empty Active group when every task is finished', () => {
    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: {
        timelineItems: [
          {
            id: 'assistant-run',
            type: 'assistant_run',
            items: [
              subAgentTask({
                id: 'completed',
                agentId: 'reviewer',
                content: 'Review the result',
                status: 'completed',
              }),
            ],
          },
        ],
      },
    });
    flushSync();

    document.querySelector('.chat-activity__rail').click();
    flushSync();

    expect(document.querySelector('.chat-activity__group--active')).toBeNull();
    expect(
      document.querySelector('.chat-activity__group--finished h3'),
    ).toBeTruthy();
  });

  it('shows the aggregated Session change stats above the tasks', () => {
    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: {
        timelineItems: [
          {
            id: 'assistant-run',
            type: 'assistant_run',
            items: [
              {
                type: 'tool_call',
                id: 'tool-edit-1',
                name: 'edit',
                status: 'success',
                arguments: { path: 'a.txt' },
                startedEvent: {
                  type: 'tool_call_started',
                  payload: { tool_call: { id: 'call-edit-1', name: 'edit' } },
                },
                resultEvent: {
                  type: 'tool_call_result',
                  payload: {
                    tool_call: { id: 'call-edit-1', name: 'edit' },
                    display: {
                      version: 1,
                      summary: 'a.txt',
                      hidden_argument_keys: [],
                      primary: [],
                      facts: [
                        { kind: 'line_change', change: 'added', value: 3 },
                        { kind: 'line_change', change: 'removed', value: 2 },
                      ],
                    },
                  },
                },
              },
            ],
          },
          {
            id: 'assistant-run-2',
            type: 'assistant_run',
            items: [
              {
                type: 'tool_call',
                id: 'tool-write-1',
                name: 'write',
                status: 'success',
                arguments: { path: 'b.txt' },
                startedEvent: {
                  type: 'tool_call_started',
                  payload: { tool_call: { id: 'call-write-1', name: 'write' } },
                },
                resultEvent: {
                  type: 'tool_call_result',
                  payload: {
                    tool_call: { id: 'call-write-1', name: 'write' },
                    display: {
                      version: 1,
                      summary: 'b.txt',
                      hidden_argument_keys: [],
                      primary: [],
                      facts: [
                        { kind: 'line_change', change: 'added', value: 5 },
                        { kind: 'line_change', change: 'removed', value: 0 },
                      ],
                    },
                  },
                },
              },
            ],
          },
        ],
      },
    });
    flushSync();

    document.querySelector('.chat-activity__rail').click();
    flushSync();

    const statsValue = document.querySelector('.chat-activity__stats-value');
    expect(statsValue).toBeTruthy();
    expect(statsValue.textContent.trim()).toBe('2 files changed, +8 -2');
    expect(document.querySelector('.chat-activity__stats-empty')).toBeNull();
  });

  it('shows a calm empty state for the Session stats when nothing changed', () => {
    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: { timelineItems: [] },
    });
    flushSync();

    document.querySelector('.chat-activity__rail').click();
    flushSync();

    expect(
      document.querySelector('.chat-activity__stats-empty').textContent.trim(),
    ).toBe('No changes yet');
  });
});
