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

  it('keeps a narrow rail collapsed and opens the current Session tasks', () => {
    const navigate = vi.fn();
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

    mountedComponent = mount(ChatActivityPanel, {
      target: document.body,
      props: {
        timelineItems: [
          {
            id: 'assistant-run',
            type: 'assistant_run',
            items: [completed, cancelled, failed, foreground, running],
          },
        ],
        onNavigateToSubAgent: navigate,
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
    expect(document.querySelector('#chat-activity-title').textContent).toBe(
      'Background tasks',
    );
    const rows = [...document.querySelectorAll('.chat-activity__task-row')];
    expect(rows).toHaveLength(4);
    expect(rows[0].textContent.trim()).toBe('builder');
    expect(rows[0].getAttribute('aria-label')).toBe(
      'Open builder Session · Working',
    );
    expect(rows[0].querySelector('[data-status="running"]')).not.toBeNull();
    const completedRow = rows.find(
      (row) => row.textContent.trim() === 'reviewer',
    );
    expect(completedRow.getAttribute('aria-label')).toBe(
      'Open reviewer Session · Completed',
    );
    expect(
      completedRow.querySelector('[data-status="success"]'),
    ).not.toBeNull();
    const cancelledRow = rows.find(
      (row) => row.textContent.trim() === 'writer',
    );
    expect(cancelledRow.getAttribute('aria-label')).toBe(
      'Open writer Session · Cancelled',
    );
    expect(
      cancelledRow.querySelector('[data-status="cancelled"]'),
    ).not.toBeNull();
    const failedRow = rows.find((row) => row.textContent.trim() === 'tester');
    expect(failedRow.getAttribute('aria-label')).toBe(
      'Open tester Session · Failed',
    );
    expect(failedRow.querySelector('[data-status="failed"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain('Implement the sidebar');
    expect(document.body.textContent).not.toContain('View session');
    expect(document.body.textContent).not.toContain('Run foreground checks');

    rows[0].click();
    expect(navigate).toHaveBeenCalledWith({
      agentId: 'builder',
      sessionId: 'session-running',
    });

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

    expect(
      document.querySelector('.chat-activity__empty').textContent.trim(),
    ).toBe('No background tasks');
    expect(document.body.textContent).toContain('No background tasks');
  });
});
