import { isPlainObject } from '$lib/values.js';
import { trimmedString, truncateToolLabel, parseJsonValue } from './values.js';
import { t } from '$lib/i18n.js';
import {
  toolNameForRunTool,
  toolStatus,
  toolArguments,
  executionStateTitle,
} from './toolFacts.js';
import {
  formatDurationMs,
  elapsedSinceTimestamp,
  executionDetailRows,
  timestampToMs,
} from './time.js';
import { qualifyAgentAddress } from '$lib/agentAddress.js';

const MAX_SUBAGENT_PREVIEW_LENGTH = 96;

// The `subagent` Tool's actions: `run` starts a new Sub-Agent in the
// background, `send` gives an existing one another message, `list` shows them,
// `cancel` stops one. Persisted calls may still say `status`, the former name
// of `list`.
const SUBAGENT_ACTIONS = new Set(['run', 'send', 'list', 'cancel']);

// Result statuses of a message delivered to an existing Sub-Agent: steered
// into its active Run, starting a new Run, or queued for its next one.
const SUBAGENT_SEND_STATUSES = new Set(['steered', 'started', 'queued']);

// Persisted calls keep the Model's own spelling. The server also runs a task
// sent under another harness's name for it, so the preview reads those too.
const SUBAGENT_TASK_FIELDS = [
  'content',
  'prompt',
  'task',
  'goal',
  'message',
  'instructions',
];

const subAgentTaskText = (args) =>
  SUBAGENT_TASK_FIELDS.map((key) => trimmedString(args[key])).find(Boolean) ??
  '';

// The short title a `run` gives its Sub-Agent, under the spellings the server
// accepts for `description`.
const SUBAGENT_DESCRIPTION_FIELDS = [
  'description',
  'title',
  'label',
  'summary',
  'task_name',
];

const subAgentDescriptionText = (args) =>
  SUBAGENT_DESCRIPTION_FIELDS.map((key) => trimmedString(args[key])).find(
    Boolean,
  ) ?? '';

/**
 * The action a `subagent` Tool row performed: 'run', 'send', 'list',
 * 'cancel', or '' when it cannot be told (another Tool, or a call the server
 * refused before it knew what it meant).
 *
 * The call's own action decides, with two exceptions the server resolves
 * itself: a `run` naming an existing Sub-Agent delivers a message (its result
 * or live start event says so), and another harness's action word (`spawn`,
 * `stop`, ...) reads as what the result or the live start event shows the
 * call did.
 */
export const subAgentAction = (tool) => {
  if (toolNameForRunTool(tool) !== 'subagent') {
    return '';
  }
  const args = subAgentArguments(tool);
  const data = subAgentResultEnvelopeData(tool);
  const status = trimmedString(data.status).toLowerCase();
  // The live start event names the Run of a new Sub-Agent; a message to an
  // existing one starts no Run of its own.
  const started = isPlainObject(tool.subAgentSession)
    ? tool.subAgentSession
    : {};
  const startedAction = trimmedString(started.session_id)
    ? trimmedString(started.run_id)
      ? 'run'
      : 'send'
    : '';
  let action = trimmedString(args.action).toLowerCase();
  if (action === 'status') {
    action = 'list';
  }
  if (!action) {
    // The server implies send for an id with a message, refuses an id alone,
    // and starts a new Sub-Agent otherwise.
    if (trimmedString(args.id)) {
      action = subAgentTaskText(args) ? 'send' : '';
    } else {
      action = 'run';
    }
  }
  if (
    action === 'run' &&
    (SUBAGENT_SEND_STATUSES.has(status) ||
      (trimmedString(args.id) && startedAction === 'send'))
  ) {
    return 'send';
  }
  if (SUBAGENT_ACTIONS.has(action)) {
    return action;
  }
  if (Array.isArray(data.subagents)) {
    return 'list';
  }
  if (status === 'running') {
    return 'run';
  }
  if (SUBAGENT_SEND_STATUSES.has(status)) {
    return 'send';
  }
  if (status === 'cancelled') {
    return 'cancel';
  }
  return startedAction;
};

// A row that started a new Sub-Agent: it follows that Sub-Agent's work and
// can cancel it.
export const isSubAgentSpawnTool = (tool) => subAgentAction(tool) === 'run';

// A row that gave an existing Sub-Agent another message.
export const isSubAgentSendTool = (tool) => subAgentAction(tool) === 'send';

// Rows that address one Sub-Agent and link to its Session. `list` and
// `cancel` render as ordinary Tool rows.
export const isSubAgentTargetTool = (tool) => {
  const action = subAgentAction(tool);
  return action === 'run' || action === 'send';
};

const TERMINAL_CHILD_STATUSES = new Set([
  'completed',
  'success',
  'failed',
  'error',
  'cancelled',
  'interrupted',
]);

// Which status entries describe a spawn row's Sub-Agent right now: its own
// Run while that is known and has not ended. Once it ended, a Run running
// again in the Sub-Agent's Session (it got another message) is the Sub-Agent's
// current work. Without a known Run the Session entries are the only source.
// A Session entry never settles a row whose own Run is still running or
// unknown: it may describe another Run of the same Session.
function subAgentStatusScope(tool, statuses) {
  const runId = subAgentEffectiveRunId(tool, statuses);
  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  const session = agentId && sessionId ? `${agentId}::${sessionId}` : '';
  if (!runId) {
    return { runId: '', session };
  }
  const own = trimmedString(statuses[`run:${runId}`]).toLowerCase();
  if (
    session &&
    TERMINAL_CHILD_STATUSES.has(own) &&
    trimmedString(statuses[`session:${session}`]).toLowerCase() === 'running'
  ) {
    return { runId: '', session };
  }
  return { runId, session: '' };
}

function statusEntry(statuses, kind, scope) {
  if (scope.runId) {
    return statuses[`${kind}:${scope.runId}`];
  }
  if (scope.session) {
    return statuses[`${kind}:${scope.session}`];
  }
  return undefined;
}

const SCOPED_KEYS = {
  status: ['run', 'session'],
  started: ['runStarted', 'sessionStarted'],
  duration: ['runDuration', 'sessionDuration'],
  tool: ['runTool', 'sessionTool'],
};

function scopedValue(tool, subAgentStatuses, entry) {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const scope = subAgentStatusScope(tool, statuses);
  const [runKind, sessionKind] = SCOPED_KEYS[entry];
  return statusEntry(statuses, scope.runId ? runKind : sessionKind, scope);
}

// Real wall-clock runtime of the Sub-Agent Run the row follows, captured from
// its terminal lifecycle event or inspection. Null while none was tracked.
const subAgentRunDurationMs = (tool, subAgentStatuses = {}) => {
  const durationMs = scopedValue(tool, subAgentStatuses, 'duration');
  return Number.isFinite(durationMs) && durationMs >= 0 ? durationMs : null;
};

export const subAgentRunStartedAt = (tool, subAgentStatuses = {}) =>
  trimmedString(scopedValue(tool, subAgentStatuses, 'started'));

// Name of the most recent Tool call the followed Run made, recorded from
// bridged child `tool_call_started` events. '' when the Run has made no Tool
// call yet (or the entry was reset on Run start / evicted from the capped
// projection).
export const subAgentLastToolName = (tool, subAgentStatuses = {}) => {
  if (!isSubAgentSpawnTool(tool)) {
    return '';
  }
  return trimmedString(scopedValue(tool, subAgentStatuses, 'tool'));
};

// Status label for a spawn row: the followed Run's real runtime. The spawn
// call itself returns the moment the Sub-Agent starts, so its own ~0s
// duration would mislead; without a tracked runtime the label stays empty.
export const subAgentToolStatusLabel = (
  tool,
  dotStatus,
  subAgentStatuses = {},
  nowMs = Date.now(),
) => {
  if (dotStatus === 'cancelled') {
    const duration = formatDurationMs(
      subAgentRunDurationMs(tool, subAgentStatuses),
    );
    return [t('chat.toolCancelled'), duration].filter(Boolean).join(' · ');
  }
  if (dotStatus === 'running') {
    return formatDurationMs(
      elapsedSinceTimestamp(
        subAgentRunStartedAt(tool, subAgentStatuses),
        nowMs,
      ),
    );
  }
  return formatDurationMs(subAgentRunDurationMs(tool, subAgentStatuses));
};

const RUNNING_WORK_LABELS = {
  subagent: () => t('chat.details.runningSubAgent'),
  command: () => t('chat.details.runningCommand'),
  terminal: () => t('chat.details.openTerminal'),
};

/**
 * Tooltip details behind a spawn row's status: the followed Run's state in
 * words, when it started and finished, how long it ran, the Tool it is
 * calling while it runs, and the inspected work the Sub-Agent still runs
 * besides its Run (`remainingWork`, from its inspection; its Sub-Agents by title,
 * commands and terminals by command line).
 */
export const subAgentStatusDetails = (
  tool,
  dotStatus,
  subAgentStatuses = {},
  nowMs = Date.now(),
  remainingWork = [],
) => {
  const running = dotStatus === 'running';
  const startedAt = subAgentRunStartedAt(tool, subAgentStatuses);
  const durationMs = running
    ? elapsedSinceTimestamp(startedAt, nowMs)
    : subAgentRunDurationMs(tool, subAgentStatuses);
  const startedMs = timestampToMs(startedAt);
  const rows = executionDetailRows({
    startedAt,
    finishedAt:
      !running && startedMs !== null && durationMs !== null
        ? startedMs + durationMs
        : '',
    durationMs,
    running,
    nowMs,
  });
  const lastToolName = running
    ? subAgentLastToolName(tool, subAgentStatuses)
    : '';
  if (lastToolName) {
    rows.push({
      label: t('chat.details.latestTool'),
      value: lastToolName,
      mono: true,
    });
  }
  for (const work of Array.isArray(remainingWork) ? remainingWork : []) {
    const label = RUNNING_WORK_LABELS[work?.kind];
    if (label) {
      rows.push({
        label: label(),
        value: work.label || work.id,
        mono: work.kind !== 'subagent',
      });
    }
  }
  return { title: executionStateTitle(dotStatus), rows };
};

const subAgentSessionId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.session_id) || trimmedString(args.session_id);
};

export const subAgentAgentId = (tool) => {
  return subAgentTargetAddress(tool) || t('common.unknown');
};

// The Sub-Agent's outside address. Current results carry `agent@projekt` in
// `agent_id`; historical results and live start events carry the bare id
// beside `project_id`. Both yield the same address.
const subAgentTargetAddress = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  const dataAddress = qualifyAgentAddress(data.agent_id, data.project_id);
  if (dataAddress) {
    return dataAddress;
  }
  return trimmedString(args.agent_id);
};

// The Sub-Agent Run a spawn row follows: the Run its start event (or a
// historical result) names, else the Run inspection found for the row's
// Sub-Agent id.
export const subAgentEffectiveRunId = (tool, subAgentStatuses = {}) => {
  const runId = trimmedString(subAgentResultData(tool).run_id);
  if (runId) {
    return runId;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const workId = trimmedString(subAgentResultData(tool).id);
  return workId ? trimmedString(statuses[`workRun:${workId}`]) : '';
};

// How a spawn row's cancel button acts. The followed Run is cancelled when it
// is known and still the Sub-Agent's current work; otherwise the controller
// inspects the Sub-Agent first and cancels the Run it finds running. `null`
// means the row addresses nothing cancellable.
export const resolveSubAgentCancelPlan = (tool, subAgentStatuses = {}) => {
  if (!isPlainObject(tool)) {
    return null;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const scope = subAgentStatusScope(tool, statuses);
  if (scope.runId) {
    return { kind: 'run', runId: scope.runId };
  }
  const target = subAgentNavigationTarget(tool);
  if (!target) {
    return null;
  }
  return {
    kind: 'inspect',
    agentId: target.agentId,
    sessionId: target.sessionId,
    workId: trimmedString(subAgentResultData(tool).id),
  };
};

// The complete task (run) or message (send) a row carries, behind its
// shortened preview.
export const subAgentTask = (tool) =>
  toolNameForRunTool(tool) === 'subagent'
    ? subAgentTaskText(subAgentArguments(tool))
    : '';

export const subAgentPreview = (tool) =>
  truncateToolLabel(subAgentTask(tool), MAX_SUBAGENT_PREVIEW_LENGTH);

// The title a new Sub-Agent got from its `run` call; '' for other rows and
// for calls made before the title was required.
export const subAgentDescription = (tool) =>
  isSubAgentSpawnTool(tool)
    ? truncateToolLabel(
        subAgentDescriptionText(subAgentArguments(tool)),
        MAX_SUBAGENT_PREVIEW_LENGTH,
      )
    : '';

export const subAgentDotStatus = (tool, subAgentStatuses = {}) => {
  const parentStatus = toolStatus(tool);
  if (
    !isSubAgentSpawnTool(tool) ||
    ['failed', 'cancelled'].includes(parentStatus)
  ) {
    return parentStatus;
  }

  const externalStatus = subAgentStatusToDotStatus(
    trimmedString(scopedValue(tool, subAgentStatuses, 'status')).toLowerCase(),
  );
  if (externalStatus) {
    return externalStatus;
  }

  const childStatus = subAgentStatusToDotStatus(
    trimmedString(subAgentResultData(tool).status).toLowerCase(),
  );
  return childStatus || parentStatus;
};

export const subAgentNavigationTarget = (tool) => {
  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return null;
  }
  return { agentId, sessionId };
};

// The spawn row's dot falls back to the frozen persisted result's `status`
// when no live status has arrived. That fallback reads "running" forever after
// a missed terminal event, a rolled replay buffer, or a server restart. When
// neither the followed Run's nor the Sub-Agent Session's status has been seen
// at all, the Chat controller verifies the durable work record.
export const subAgentNeedsStatusVerification = (
  tool,
  dotStatus,
  subAgentStatuses = {},
) => {
  if (dotStatus !== 'running') {
    return false;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};

  // With a known run id only the run-scoped key counts: a session-scoped
  // status may belong to a different Run of the same Session, so it must
  // neither settle this row nor suppress its verification.
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return !Object.hasOwn(statuses, `run:${runId}`);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  return !(
    agentId &&
    sessionId &&
    Object.hasOwn(statuses, `session:${agentId}::${sessionId}`)
  );
};

function subAgentArguments(tool) {
  const parsedArguments = parseJsonValue(toolArguments(tool));
  if (!isPlainObject(parsedArguments)) {
    return {};
  }
  return isPlainObject(parsedArguments.request)
    ? parsedArguments.request
    : parsedArguments;
}

function subAgentResultEnvelopeData(tool) {
  const envelope = parseJsonValue(tool?.result);
  if (!isPlainObject(envelope)) {
    return {};
  }
  return isPlainObject(envelope.data) ? envelope.data : envelope;
}

// What the row knows about its Sub-Agent: the live start event's data
// overlaid with the Tool result's.
export function subAgentResultData(tool) {
  const sessionData = isPlainObject(tool?.subAgentSession)
    ? tool.subAgentSession
    : {};
  return { ...sessionData, ...subAgentResultEnvelopeData(tool) };
}

export function subAgentToolLabel(toolName, args) {
  let action = trimmedString(args.action);
  if (action.toLowerCase() === 'status') {
    action = 'list';
  }
  if (action && action !== 'run') {
    return [action, trimmedString(args.id)].filter(Boolean).join(' · ');
  }
  const agentId = trimmedString(args.agent_id);
  const preview = truncateToolLabel(
    subAgentDescriptionText(args) || subAgentTaskText(args),
    MAX_SUBAGENT_PREVIEW_LENGTH,
  );
  return [agentId, preview].filter(Boolean).join(' · ');
}

function subAgentStatusToDotStatus(status) {
  if (['running', 'queued'].includes(status)) {
    return 'running';
  }
  if (['failed', 'error'].includes(status)) {
    return 'failed';
  }
  if (status === 'cancelled') {
    return 'cancelled';
  }
  if (['completed', 'success'].includes(status)) {
    return 'success';
  }
  return '';
}
