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
    const cancel = vi.fn();
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
    const foreground = subAgentTask({
      id: 'foreground',
      agentId: 'tester',
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
            items: [completed, foreground, running],
          },
        ],
        onNavigateToSubAgent: navigate,
        onCancelSubAgent: cancel,
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
    const cards = [...document.querySelectorAll('.chat-activity__task')];
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('Implement the sidebar');
    expect(cards[0].textContent).toContain('Running');
    expect(document.body.textContent).not.toContain('Run foreground checks');

    cards[0].querySelector('.chat-activity__open-task').click();
    expect(navigate).toHaveBeenCalledWith({
      agentId: 'builder',
      sessionId: 'session-running',
    });

    cards[0].querySelector('.chat-activity__cancel-task').click();
    expect(cancel).toHaveBeenCalledWith({ tool: running });

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

    expect(document.querySelector('.chat-activity__empty')).not.toBeNull();
    expect(document.body.textContent).toContain('No background tasks');
  });
});
