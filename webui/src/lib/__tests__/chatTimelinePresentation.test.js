import { beforeEach, describe, expect, it } from 'vitest';

import {
  compactToolValue,
  subAgentDotStatus,
  toolArgumentSummary,
} from '../chatTimelinePresentation.js';
import { init } from '../i18n.js';

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
});
