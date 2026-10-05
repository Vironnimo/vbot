import { describe, expect, it } from 'vitest';
import {
  backgroundTasks,
  isSubAgentSendTool,
  isSubAgentSpawnTool,
  resolveSubAgentCancelPlan,
  subAgentAction,
  subAgentAgentId,
  subAgentDescription,
  subAgentDotStatus,
  subAgentEffectiveRunId,
  subAgentLastToolName,
  subAgentNavigationTarget,
  subAgentNeedsStatusVerification,
  subAgentPreview,
  subAgentStatusDetails,
  subAgentTask,
  subAgentToolStatusLabel,
} from '../chatTimelinePresentation.js';
import { t } from '../i18n.js';
import { formatMoment } from '../timeText.js';
import {
  backgroundCommandTool,
  reloadedSubAgentTool,
  runningSubAgentTool,
  sendSubAgentTool,
} from './chatTimelinePresentation.support.js';

const seconds = (value) => t('chat.durationSeconds', { seconds: value });

// The Sub-Agent of `runningSubAgentTool` finished the Run its spawn started
// and now runs again for a later message.
const workingAgain = {
  'run:run-child': 'completed',
  'session:worker::session-child': 'running',
};

describe('Sub-Agent actions', () => {
  it.each([
    ['a running spawn', () => runningSubAgentTool(), 'run'],
    [
      'a canonical run action',
      () => ({
        name: 'subagent',
        arguments: { action: 'run', content: 'canonical child task' },
      }),
      'run',
    ],
    [
      'a capitalized run action',
      () => ({
        name: 'subagent',
        arguments: { action: 'Run', content: 'Inspect the project' },
      }),
      'run',
    ],
    [
      'a spawn action carrying a started result',
      () =>
        runningSubAgentTool({
          arguments: { action: 'spawn', task: 'Inspect the project' },
        }),
      'run',
    ],
    [
      'another harness spelling carrying a started result',
      () =>
        runningSubAgentTool({
          arguments: {
            description: 'Inspect',
            prompt: 'Inspect the project',
            subagent_type: 'worker',
          },
        }),
      'run',
    ],
    [
      'an unfamiliar action without a result',
      () => ({
        name: 'subagent',
        arguments: { action: 'spawn', task: 'Inspect the project' },
      }),
      '',
    ],
    [
      'a stop action without a result',
      () => ({
        name: 'subagent',
        arguments: { action: 'stop', id: 'sub_child' },
      }),
      '',
    ],
    ['a send call', () => sendSubAgentTool(), 'send'],
    [
      'a message to an id without an action',
      () => ({
        name: 'subagent',
        arguments: { id: 'sub_child', content: 'Also check the tests' },
      }),
      'send',
    ],
    [
      'a run naming an existing Sub-Agent, by its result',
      () =>
        sendSubAgentTool({
          arguments: { action: 'run', id: 'sub_child', content: 'More' },
        }),
      'send',
    ],
    [
      'a run naming an existing Sub-Agent, by its live start event',
      () => ({
        name: 'subagent',
        status: 'running',
        arguments: { action: 'run', id: 'sub_child', content: 'More' },
        subAgentSession: {
          id: 'sub_child',
          agent_id: 'worker',
          session_id: 'session-child',
          status: 'running',
        },
      }),
      'send',
    ],
    [
      'a list call',
      () => ({ name: 'subagent', arguments: { action: 'list' } }),
      'list',
    ],
    [
      'a former status call carrying a started result',
      () =>
        runningSubAgentTool({
          arguments: { action: 'status', id: 'sub_child' },
        }),
      'list',
    ],
    [
      'a cancel call',
      () => ({
        name: 'subagent',
        arguments: { action: 'cancel', id: 'sub_child' },
      }),
      'cancel',
    ],
    [
      'a capitalized cancel call carrying a started result',
      () =>
        runningSubAgentTool({
          arguments: { action: 'Cancel', id: 'sub_child' },
        }),
      'cancel',
    ],
    [
      'another Tool',
      () => ({ name: 'bash', arguments: { command: 'ls' } }),
      '',
    ],
  ])('reads %s as %o', (_label, tool, action) => {
    expect(subAgentAction(tool())).toBe(action);
    expect(isSubAgentSpawnTool(tool())).toBe(action === 'run');
    expect(isSubAgentSendTool(tool())).toBe(action === 'send');
  });
});

describe('Sub-Agent rows', () => {
  it.each([
    [{ prompt: 'Review imports', subagent_type: 'worker' }, 'Review imports'],
    [{ goal: 'Fix tests' }, 'Fix tests'],
    [{ content: 'Canonical task', prompt: 'Other' }, 'Canonical task'],
  ])('previews the task from %o', (args, preview) => {
    expect(subAgentPreview(runningSubAgentTool({ arguments: args }))).toBe(
      preview,
    );
  });

  it.each([
    [
      'its description',
      runningSubAgentTool({
        arguments: { content: 'Inspect', description: 'Module check' },
      }),
      'Module check',
    ],
    [
      'another spelling of the description',
      runningSubAgentTool({
        arguments: { content: 'Inspect', label: 'Labelled check' },
      }),
      'Labelled check',
    ],
    ['nothing for a call without one', runningSubAgentTool(), ''],
    ['nothing for a message to an existing Sub-Agent', sendSubAgentTool(), ''],
  ])('titles a Sub-Agent row with %s', (_case, tool, description) => {
    expect(subAgentDescription(tool)).toBe(description);
  });

  it('keeps the complete task behind a shortened preview', () => {
    const task = `Review ${'every module '.repeat(12)}`.trim();
    const tool = runningSubAgentTool({ arguments: { content: task } });

    expect(subAgentPreview(tool)).toHaveLength(96);
    expect(subAgentTask(tool)).toBe(task);
  });

  it('projects started Sub-Agents and handed-off commands with active work first', () => {
    const running = runningSubAgentTool({ type: 'tool_call' });
    const completed = runningSubAgentTool({
      type: 'tool_call',
      id: 'tool-completed',
      arguments: {
        action: 'run',
        agent_id: 'reviewer',
        content: 'Review the implementation',
        description: 'Review pass',
      },
      subAgentSession: {
        id: 'sub_completed',
        agent_id: 'reviewer',
        session_id: 'session-reviewer',
        run_id: 'run-reviewer',
        status: 'running',
      },
      result: {
        ok: true,
        data: {
          id: 'sub_completed',
          agent_id: 'reviewer',
          session_id: 'session-reviewer',
          status: 'running',
        },
        artifacts: [],
      },
    });
    // A message to an existing Sub-Agent and a refused run start no work.
    const message = sendSubAgentTool({ type: 'tool_call', id: 'tool-send' });
    const refused = {
      type: 'tool_call',
      id: 'tool-refused',
      name: 'subagent',
      status: 'failed',
      arguments: { action: 'run', agent_id: 'missing', content: 'Do it' },
      resultEvent: { type: 'tool_call_result' },
      result: { ok: false, error: { code: 'not_found' }, data: null },
    };
    const backgroundBash = backgroundCommandTool();
    const foregroundBash = backgroundCommandTool({
      id: 'bash-foreground',
      arguments: { command: 'npm test' },
      result: {
        ok: true,
        data: { exit_code: 0, output: 'passed' },
        artifacts: [],
      },
    });

    const tasks = backgroundTasks(
      [
        { id: 'run-old', type: 'assistant_run', items: [running] },
        {
          id: 'run-new',
          type: 'assistant_run',
          items: [completed, message, refused, foregroundBash, backgroundBash],
        },
      ],
      { 'run:run-reviewer': 'completed' },
      { term_one: 'failed' },
    );

    expect(tasks.map((task) => [task.tool.id, task.dotStatus])).toEqual([
      [running.id, 'running'],
      [backgroundBash.id, 'failed'],
      [completed.id, 'success'],
    ]);
    expect(tasks[1]).toEqual(
      expect.objectContaining({
        kind: 'command',
        command: 'npm run dev',
        terminalId: 'term_one',
        target: null,
      }),
    );
    expect(tasks[2]).toEqual(
      expect.objectContaining({
        kind: 'subagent',
        agentId: 'reviewer',
        description: 'Review pass',
        target: { agentId: 'reviewer', sessionId: 'session-reviewer' },
      }),
    );
  });

  it('lists a legacy spawn without an action', () => {
    const legacy = {
      type: 'tool_call',
      id: 'tool-legacy',
      name: 'subagent',
      arguments: {
        agent_id: 'worker',
        background: true,
        content: 'Inspect the project',
      },
      resultEvent: { type: 'tool_call_result' },
      result: {
        ok: true,
        data: { agent_id: 'worker', session_id: 'session-legacy' },
      },
    };

    expect(
      backgroundTasks([
        { id: 'run-legacy', type: 'assistant_run', items: [legacy] },
      ]),
    ).toEqual([
      expect.objectContaining({
        kind: 'subagent',
        tool: legacy,
        target: { agentId: 'worker', sessionId: 'session-legacy' },
      }),
    ]);
  });
});

describe('Sub-Agent status', () => {
  it.each([
    [
      'settles an externally completed Run',
      () => ({
        name: 'subagent',
        status: 'running',
        arguments: {
          action: 'run',
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
      }),
      { 'run:run-child': 'completed' },
      'success',
    ],
    [
      // A previous Run of the reused child Session left its terminal status
      // under the Session key; this spawn's own Run has no status yet.
      'ignores a stale Session status for a row with a known Run id',
      runningSubAgentTool,
      { 'session:worker::session-child': 'completed' },
      'running',
    ],
    [
      'keeps a frozen running descriptor without live status',
      runningSubAgentTool,
      {},
      'running',
    ],
    [
      'follows the Session while the Sub-Agent works on a later message',
      runningSubAgentTool,
      workingAgain,
      'running',
    ],
    [
      'settles a reloaded row through the Run inspection found',
      reloadedSubAgentTool,
      { 'workRun:sub_reloaded': 'run-found', 'run:run-found': 'completed' },
      'success',
    ],
    [
      'settles a reloaded row without a known Run through its Session',
      reloadedSubAgentTool,
      { 'session:worker::session-child': 'completed' },
      'success',
    ],
    [
      'keeps a failed spawn call failed',
      () => runningSubAgentTool({ status: 'failed' }),
      { 'run:run-child': 'running' },
      'failed',
    ],
    [
      'reports a send row by its own call',
      sendSubAgentTool,
      { 'session:worker::session-child': 'running' },
      'success',
    ],
  ])('%s', (_label, tool, statuses, dotStatus) => {
    expect(subAgentDotStatus(tool(), statuses)).toBe(dotStatus);
  });

  it.each([
    ['a frozen running descriptor', runningSubAgentTool, 'running', {}, true],
    [
      'a Run-id-less row with a Session status',
      reloadedSubAgentTool,
      'running',
      { 'session:worker::session-child': 'running' },
      false,
    ],
    [
      // The Session entry may describe another Run of the reused child Session.
      'a known Run id with only a Session status',
      runningSubAgentTool,
      'running',
      { 'session:worker::session-child': 'completed' },
      true,
    ],
    [
      'a row whose Run status arrived',
      runningSubAgentTool,
      'running',
      { 'run:run-child': 'completed' },
      false,
    ],
    ['a successful dot', runningSubAgentTool, 'success', {}, false],
    ['a failed dot', runningSubAgentTool, 'failed', {}, false],
    ['a cancelled dot', runningSubAgentTool, 'cancelled', {}, false],
    ['a missing status map', runningSubAgentTool, 'running', null, true],
    [
      'an undefined status map',
      runningSubAgentTool,
      'running',
      undefined,
      true,
    ],
    ['a malformed status map', runningSubAgentTool, 'running', 'x', true],
  ])(
    'decides status verification for %s',
    (_label, tool, dotStatus, statuses, verify) => {
      expect(subAgentNeedsStatusVerification(tool(), dotStatus, statuses)).toBe(
        verify,
      );
    },
  );

  it('resolves the effective Run id from the start event or inspection', () => {
    expect(subAgentEffectiveRunId(runningSubAgentTool())).toBe('run-child');
    expect(subAgentEffectiveRunId(reloadedSubAgentTool())).toBe('');
    expect(
      subAgentEffectiveRunId(reloadedSubAgentTool(), {
        'workRun:sub_reloaded': 'run-from-inspection',
      }),
    ).toBe('run-from-inspection');
  });

  it.each([
    [
      'cancels the followed Run',
      runningSubAgentTool,
      undefined,
      { kind: 'run', runId: 'run-child' },
    ],
    [
      'cancels the Run inspection found for a reloaded row',
      reloadedSubAgentTool,
      { 'workRun:sub_reloaded': 'run-found' },
      { kind: 'run', runId: 'run-found' },
    ],
    [
      'inspects a reloaded row without a known Run',
      reloadedSubAgentTool,
      undefined,
      {
        kind: 'inspect',
        agentId: 'worker',
        sessionId: 'session-child',
        workId: 'sub_reloaded',
      },
    ],
    [
      // The Run working on the later message is not the followed one.
      'inspects a Sub-Agent working on a later message',
      runningSubAgentTool,
      workingAgain,
      {
        kind: 'inspect',
        agentId: 'worker',
        sessionId: 'session-child',
        workId: 'sub_child',
      },
    ],
    ['plans nothing for a missing row', () => null, undefined, null],
    [
      'plans nothing without Run or Session',
      () => ({ name: 'subagent', arguments: {} }),
      undefined,
      null,
    ],
  ])('%s', (_label, tool, statuses, plan) => {
    expect(resolveSubAgentCancelPlan(tool(), statuses)).toEqual(plan);
  });
});

describe('Sub-Agent addressing', () => {
  it('keeps a qualified target address for navigation, status keys and cancel', () => {
    // A historical result names the child by its bare id beside `project_id`.
    const tool = reloadedSubAgentTool({
      result: {
        ok: true,
        data: {
          id: 'sub_reloaded',
          agent_id: 'worker',
          project_id: 'vbot',
          session_id: 'session-child',
          status: 'running',
        },
      },
    });

    expect(subAgentNavigationTarget(tool)).toEqual({
      agentId: 'worker@vbot',
      sessionId: 'session-child',
    });
    expect(
      subAgentToolStatusLabel(tool, 'success', {
        'sessionDuration:worker@vbot::session-child': 8700,
      }),
    ).toBe(seconds('8.7'));
    expect(resolveSubAgentCancelPlan(tool)).toEqual({
      kind: 'inspect',
      agentId: 'worker@vbot',
      sessionId: 'session-child',
      workId: 'sub_reloaded',
    });
  });

  it.each([
    ['a current result with the qualified agent_id', 'worker@vbot', false],
    ['a historical result with a bare agent_id', 'worker', false],
    ['a current result merged over the live start event', 'worker@vbot', true],
  ])(
    'addresses a Project child exactly once from %s',
    (_label, resultAgentId, withLiveStart) => {
      const tool = runningSubAgentTool({
        subAgentSession: withLiveStart
          ? {
              id: 'sub_child',
              agent_id: 'worker',
              project_id: 'vbot',
              session_id: 'session-child',
              status: 'running',
            }
          : undefined,
        result: {
          ok: true,
          error: null,
          data: {
            id: 'sub_child',
            agent_id: resultAgentId,
            project_id: 'vbot',
            session_id: 'session-child',
            status: 'running',
          },
          artifacts: [],
        },
      });

      expect(subAgentNavigationTarget(tool)).toEqual({
        agentId: 'worker@vbot',
        sessionId: 'session-child',
      });
      expect(subAgentAgentId(tool)).toBe('worker@vbot');
      expect(
        subAgentLastToolName(tool, {
          'sessionTool:worker@vbot::session-child': 'read',
        }),
      ).toBe('read');
    },
  );

  it('links a send row to the Session of the Sub-Agent it addressed', () => {
    expect(subAgentNavigationTarget(sendSubAgentTool())).toEqual({
      agentId: 'worker',
      sessionId: 'session-child',
    });
  });
});

describe('Sub-Agent timing and last Tool', () => {
  const startedAt = '2026-09-04T12:00:00Z';

  it('details the child Run state, moments, runtime and latest Tool', () => {
    const nowMs = Date.parse(startedAt) + 4200;
    const moment = (value) => formatMoment(value, { nowMs, seconds: true });

    expect(
      subAgentStatusDetails(
        runningSubAgentTool(),
        'running',
        { 'runStarted:run-child': startedAt, 'runTool:run-child': 'bash' },
        nowMs,
      ),
    ).toEqual({
      title: t('chat.toolState.running'),
      rows: [
        { label: t('chat.details.started'), value: moment(startedAt) },
        { label: t('chat.details.runningFor'), value: seconds('4.2') },
        { label: t('chat.details.latestTool'), value: 'bash', mono: true },
      ],
    });
    expect(
      subAgentStatusDetails(
        runningSubAgentTool(),
        'success',
        {
          'runStarted:run-child': startedAt,
          'runDuration:run-child': 4200,
          'runTool:run-child': 'bash',
        },
        nowMs,
      ),
    ).toEqual({
      title: t('chat.toolState.success'),
      rows: [
        { label: t('chat.details.started'), value: moment(startedAt) },
        { label: t('chat.details.finished'), value: moment(nowMs) },
        { label: t('chat.details.duration'), value: seconds('4.2') },
      ],
    });
  });

  it.each([
    [
      'the child Run runtime',
      runningSubAgentTool,
      'success',
      { 'runDuration:run-child': 4200 },
      () => seconds('4.2'),
    ],
    [
      // The Session-scoped duration may belong to another Run of the reused
      // child Session.
      'no Session duration for a row with a known Run id',
      runningSubAgentTool,
      'success',
      { 'sessionDuration:worker::session-child': 8700 },
      () => '',
    ],
    [
      'no time for a finished spawn without a tracked runtime',
      runningSubAgentTool,
      'success',
      {},
      () => '',
    ],
    [
      'the Session duration for a row without Run id',
      reloadedSubAgentTool,
      'success',
      { 'sessionDuration:worker::session-child': 8700 },
      () => seconds('8.7'),
    ],
    [
      'the inspected Run duration over the Session duration',
      reloadedSubAgentTool,
      'success',
      {
        'workRun:sub_reloaded': 'run-found',
        'runDuration:run-found': 3100,
        'sessionDuration:worker::session-child': 8700,
      },
      () => seconds('3.1'),
    ],
    [
      'the cancelled label without a tracked runtime',
      runningSubAgentTool,
      'cancelled',
      {},
      () => t('chat.toolCancelled'),
    ],
    [
      'a live tick from the child Run start',
      runningSubAgentTool,
      'running',
      { 'runStarted:run-child': startedAt },
      () => seconds('4.2'),
    ],
    [
      'a live tick from the Run working on a later message',
      runningSubAgentTool,
      'running',
      {
        ...workingAgain,
        'runStarted:run-child': '2026-09-04T11:00:00Z',
        'sessionStarted:worker::session-child': startedAt,
      },
      () => seconds('4.2'),
    ],
    [
      'no time for a running spawn without a start',
      runningSubAgentTool,
      'running',
      {},
      () => '',
    ],
  ])('labels %s', (_label, tool, dotStatus, statuses, expected) => {
    expect(
      subAgentToolStatusLabel(
        tool(),
        dotStatus,
        statuses,
        Date.parse(startedAt) + 4200,
      ),
    ).toBe(expected());
  });

  it.each([
    [
      'by Run id when it is known',
      runningSubAgentTool,
      { 'runTool:run-child': 'bash' },
      'bash',
    ],
    [
      'never from the Session key for a known Run id',
      runningSubAgentTool,
      { 'sessionTool:worker::session-child': 'read' },
      '',
    ],
    ['as empty before the first child Tool', runningSubAgentTool, {}, ''],
    [
      'from the Session key without Run id',
      reloadedSubAgentTool,
      { 'sessionTool:worker::session-child': 'read' },
      'read',
    ],
    [
      'by the inspected Run id over the Session key',
      reloadedSubAgentTool,
      {
        'workRun:sub_reloaded': 'run-found',
        'runTool:run-found': 'bash',
        'sessionTool:worker::session-child': 'read',
      },
      'bash',
    ],
    [
      'as empty for a list call',
      () =>
        runningSubAgentTool({
          arguments: { action: 'list', id: 'sub_child' },
        }),
      { 'runTool:run-child': 'bash' },
      '',
    ],
    [
      'as empty for a send row',
      sendSubAgentTool,
      { 'sessionTool:worker::session-child': 'read' },
      '',
    ],
  ])('resolves the last child Tool %s', (_label, tool, statuses, name) => {
    expect(subAgentLastToolName(tool(), statuses)).toBe(name);
  });
});
