import { beforeEach, describe, expect, it } from 'vitest';

import {
  compactToolValue,
  subAgentDisplayResult,
  subAgentDotStatus,
  subAgentResultKey,
  subAgentResultTextFromMessages,
  subAgentShouldFetchResult,
  toolArgumentSummary,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';

function runningSubAgentTool(overrides = {}) {
  return {
    name: 'subagent',
    status: 'success',
    arguments: { agent_id: 'worker', content: 'Inspect the project' },
    result: {
      ok: true,
      error: null,
      data: {
        agent_id: 'worker',
        session_id: 'session-child',
        run_id: 'run-child',
        status: 'running',
      },
      artifacts: [],
    },
    ...overrides,
  };
}

describe('chatTimelinePresentation', () => {
  beforeEach(() => {
    init('en');
  });

  it('unwraps successful read content and hides envelope metadata', () => {
    const value = compactToolValue(
      {
        ok: true,
        data: { content: 'file contents' },
        artifacts: [{ id: 'internal' }],
      },
      { preferPayload: true, toolName: 'read' },
    );

    expect(value).toBe('file contents');
  });

  it('uses the path summary without exposing edit replacement text', () => {
    const summary = toolArgumentSummary({
      name: 'edit',
      arguments: {
        path: 'notes/plan.md',
        oldString: 'before',
        newString: 'after',
      },
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('projects an externally completed sub-agent run as successful', () => {
    const tool = {
      name: 'subagent',
      status: 'running',
      arguments: {
        agent_id: 'worker',
        content: 'Inspect the project',
      },
      subAgentSession: {
        agent_id: 'worker',
        session_id: 'session-child',
        run_id: 'run-child',
        status: 'running',
      },
      startedEvent: {},
    };

    const status = subAgentDotStatus(tool, null, {
      'run:run-child': 'completed',
    });

    expect(status).toBe('success');
  });

  it('keys a sub-agent result by its target agent and session', () => {
    expect(subAgentResultKey(runningSubAgentTool())).toBe(
      'worker::session-child',
    );
    expect(subAgentResultKey({ name: 'subagent', arguments: {} })).toBe('');
  });

  it('requests a result only for a finished non-blocking spawn without inline output', () => {
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'success')).toBe(
      true,
    );
    // Still running -> no fetch yet.
    expect(subAgentShouldFetchResult(runningSubAgentTool(), 'running')).toBe(
      false,
    );
    // subagent_result lookups carry their own result already.
    expect(
      subAgentShouldFetchResult(
        { ...runningSubAgentTool(), name: 'subagent_result' },
        'success',
      ),
    ).toBe(false);
  });

  it('does not request a result when a blocking spawn already carries one', () => {
    const blockingTool = runningSubAgentTool({
      result: {
        ok: true,
        error: null,
        data: {
          agent_id: 'worker',
          session_id: 'session-child',
          run_id: 'run-child',
          status: 'completed',
          result: 'Final answer from the worker.',
        },
        artifacts: [],
      },
    });

    expect(subAgentShouldFetchResult(blockingTool, 'success')).toBe(false);
  });

  it('renders a fetched result the same way a blocking spawn result renders', () => {
    const tool = runningSubAgentTool();
    const displayValue = subAgentDisplayResult(tool, {
      loading: false,
      result: 'Final answer from the worker.',
    });
    const rendered = compactToolValue(displayValue, {
      preferPayload: true,
      toolName: 'subagent',
      tool,
    });

    expect(rendered).toContain('result: Final answer from the worker.');
    expect(rendered).toContain('status: completed');
  });

  it('keeps the original tool result when no fetched output exists', () => {
    const tool = runningSubAgentTool();
    expect(subAgentDisplayResult(tool, null)).toBe(tool.result);
    expect(subAgentDisplayResult(tool, { loading: true, result: '' })).toBe(
      tool.result,
    );
  });

  it('extracts the last assistant message text from session history', () => {
    const messages = [
      { role: 'user', content: 'Do the work' },
      { role: 'assistant', content: 'Working on it' },
      { role: 'tool', content: 'tool output' },
      { role: 'assistant', content: 'All done.' },
    ];

    expect(subAgentResultTextFromMessages(messages)).toBe('All done.');
  });

  it('extracts text from assistant content blocks and ignores empty input', () => {
    const messages = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'First part.' },
          { type: 'media', attachment_id: 'a1' },
          { type: 'text', text: 'Second part.' },
        ],
      },
    ];

    expect(subAgentResultTextFromMessages(messages)).toBe(
      'First part.\n\nSecond part.',
    );
    expect(subAgentResultTextFromMessages([])).toBe('');
    expect(subAgentResultTextFromMessages(null)).toBe('');
  });
});
