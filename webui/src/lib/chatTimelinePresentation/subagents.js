import { isPlainObject } from '$lib/values.js';
import { trimmedString, truncateToolLabel, parseJsonValue } from './values.js';
import { t } from '$lib/i18n.js';
import {
  toolNameForRunTool,
  toolDurationMs,
  toolStatus,
  toolArguments,
} from './toolFacts.js';
import { formatDurationMs, elapsedSinceTimestamp } from './time.js';
import { formatAgentAddress } from '$lib/agentAddress.js';
import { isTextContentBlock } from './messages.js';

const MAX_SUBAGENT_PREVIEW_LENGTH = 96;

// Real wall-clock runtime of the child run a sub-agent tool refers to, captured
// from the child run's terminal lifecycle event. Session-keyed fallback applies
// only when no run id is known: a child session can be reused by later spawns,
// so a session-scoped duration may describe a different run than this row's.
// Returns null when no child duration was tracked yet.
export const subAgentRunDurationMs = (tool, subAgentStatuses = {}) => {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    const durationMs = statuses[`runDuration:${runId}`];
    return Number.isFinite(durationMs) && durationMs >= 0 ? durationMs : null;
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    const durationMs = statuses[`sessionDuration:${agentId}::${sessionId}`];
    if (Number.isFinite(durationMs) && durationMs >= 0) {
      return durationMs;
    }
  }

  return null;
};

export const subAgentRunStartedAt = (tool, subAgentStatuses = {}) => {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return trimmedString(statuses[`runStarted:${runId}`]);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    return trimmedString(statuses[`sessionStarted:${agentId}::${sessionId}`]);
  }
  return '';
};

// Name of the most recent tool call the child run made, recorded from bridged
// child `tool_call_started` events. Resolved strictly by run id when one is
// known — a session-keyed entry may describe a different run of a reused child
// session (B6) — with the session key only as the run-id-less fallback.
// Returns '' when the child has made no tool call yet (or the entry was
// reset on run start / evicted from the capped projection).
export const subAgentLastToolName = (tool, subAgentStatuses = {}) => {
  if (!isSubAgentSpawnTool(tool)) {
    return '';
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return trimmedString(statuses[`runTool:${runId}`]);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (agentId && sessionId) {
    return trimmedString(statuses[`sessionTool:${agentId}::${sessionId}`]);
  }
  return '';
};

// Status label for a sub-agent tool row. Prefers the child run's real runtime
// over the spawn tool's own call duration, which is ~0s for a background spawn
// that returns the moment the child run starts.
export const subAgentToolStatusLabel = (
  tool,
  dotStatus,
  subAgentStatuses = {},
  nowMs = Date.now(),
) => {
  if (dotStatus === 'cancelled') {
    const duration = formatDurationMs(
      subAgentRunDurationMs(tool, subAgentStatuses),
      'chat.toolDurationSeconds',
    );
    return [t('chat.toolCancelled', 'cancelled'), duration]
      .filter(Boolean)
      .join(' · ');
  }
  if (dotStatus === 'running') {
    return formatDurationMs(
      elapsedSinceTimestamp(
        subAgentRunStartedAt(tool, subAgentStatuses),
        nowMs,
      ),
      'chat.toolDurationSeconds',
    );
  }

  const childDurationMs = subAgentRunDurationMs(tool, subAgentStatuses);
  if (childDurationMs !== null) {
    return formatDurationMs(childDurationMs, 'chat.toolDurationSeconds');
  }

  // A background spawn carries no inline result, so its own duration is just
  // the spawn-call time; show nothing rather than a misleading near-zero.
  if (
    toolNameForRunTool(tool) === 'subagent' &&
    !trimmedString(subAgentResultData(tool).result)
  ) {
    return '';
  }
  return formatDurationMs(toolDurationMs(tool), 'chat.toolDurationSeconds');
};

export const isSubAgentSpawnTool = (tool) => {
  if (toolNameForRunTool(tool) !== 'subagent') {
    return false;
  }
  const args = subAgentArguments(tool);
  if (args.action) {
    return args.action === 'run';
  }
  // Persisted history from before the action contract had no top-level action.
  return args.operation !== 'cancel';
};

export const isBackgroundSubAgentSpawn = (tool) => {
  if (!isSubAgentSpawnTool(tool)) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (delivery) {
    return delivery === 'automatic';
  }
  return subAgentArguments(tool).background === true;
};

export const isStartingForegroundSubAgent = (tool) => {
  if (!isSubAgentSpawnTool(tool) || !tool.startedEvent) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (delivery) {
    return delivery === 'inline';
  }
  return subAgentArguments(tool).background !== true;
};

const subAgentSessionId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.session_id) || trimmedString(args.session_id);
};

export const subAgentAgentId = (tool) => {
  return subAgentTargetAddress(tool) || t('common.unknown', 'Unknown');
};

const subAgentTargetAddress = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  const dataAgentId = trimmedString(data.agent_id);
  if (dataAgentId) {
    const projectId = trimmedString(data.project_id);
    return projectId ? formatAgentAddress(dataAgentId, projectId) : dataAgentId;
  }
  return trimmedString(args.agent_id);
};

const subAgentRunId = (tool) => {
  const args = subAgentArguments(tool);
  const data = subAgentResultData(tool);
  return trimmedString(data.run_id) || trimmedString(args.run_id);
};

// The child run id this spawn row refers to. A queued spawn's descriptor only
// carries a queue_item_id; once the queued run starts, the run stream records a
// `queueRun:<queue_item_id>` → run_id mapping (from the run_started payload),
// which resolves the row to its own run even though the persisted descriptor
// never learns the run id.
export const subAgentEffectiveRunId = (tool, subAgentStatuses = {}) => {
  const runId = subAgentRunId(tool);
  if (runId) {
    return runId;
  }
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  const workId = trimmedString(subAgentResultData(tool).id);
  if (workId) {
    const inspectedRunId = trimmedString(statuses[`workRun:${workId}`]);
    if (inspectedRunId) {
      return inspectedRunId;
    }
  }
  const queueItemId = subAgentQueueItemId(tool);
  if (!queueItemId) {
    return '';
  }
  return trimmedString(statuses[`queueRun:${queueItemId}`]);
};

export const subAgentQueueItemId = (tool) =>
  trimmedString(subAgentResultData(tool).queue_item_id);

// Decides how a sub-agent spawn row's cancel button acts. A resolvable child
// run id — from the frozen descriptor or, for a spawn that left the queue,
// the `queueRun:<item>` mapping (B6) — is cancelled as a run; a queued spawn
// without a resolvable run id is removed from the child session's queue.
// `null` means the row addresses nothing cancellable (the caller may still
// verify server-side).
export const resolveSubAgentCancelPlan = (tool, subAgentStatuses = {}) => {
  if (!isPlainObject(tool)) {
    return null;
  }
  const runId = subAgentEffectiveRunId(tool, subAgentStatuses);
  if (runId) {
    return { kind: 'run', runId };
  }
  const target = subAgentNavigationTarget(tool);
  const queueItemId = subAgentQueueItemId(tool);
  if (target && queueItemId) {
    return {
      kind: 'queue',
      queueItemId,
      agentId: target.agentId,
      sessionId: target.sessionId,
    };
  }
  return null;
};

export const subAgentPreview = (tool) => {
  const args = subAgentArguments(tool);
  const toolName = toolNameForRunTool(tool);
  if (toolName === 'subagent') {
    return truncateToolLabel(
      trimmedString(args.content),
      MAX_SUBAGENT_PREVIEW_LENGTH,
    );
  }
  return truncateToolLabel(
    trimmedString(args.session_id),
    MAX_SUBAGENT_PREVIEW_LENGTH,
  );
};

export const subAgentDotStatus = (tool, subAgentStatuses = {}) => {
  const parentStatus = toolStatus(tool);
  if (['failed', 'cancelled'].includes(parentStatus)) {
    return parentStatus;
  }

  const externalStatus = externalSubAgentStatus(tool, subAgentStatuses);
  if (externalStatus) {
    return externalStatus;
  }

  const childStatus = subAgentChildStatus(tool);
  if (['running', 'queued'].includes(childStatus)) {
    return 'running';
  }
  if (['failed', 'error'].includes(childStatus)) {
    return 'failed';
  }
  if (childStatus === 'cancelled') {
    return 'cancelled';
  }
  if (['completed', 'success'].includes(childStatus)) {
    return 'success';
  }

  return parentStatus;
};

export const subAgentNavigationTarget = (tool) => {
  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return null;
  }
  return { agentId, sessionId };
};

// Cache key for a spawn row's fetched result. Keyed by the child run when it is
// known so repeated spawns into the same child session each fetch their own
// final output; the session-level key is only the fallback for rows without a
// resolvable run id.
export const subAgentResultKey = (tool, subAgentStatuses = {}) => {
  const workId = trimmedString(subAgentResultData(tool).id);
  if (workId) {
    return `work:${workId}`;
  }
  const target = subAgentNavigationTarget(tool);
  if (!target) {
    return '';
  }
  const runId = subAgentEffectiveRunId(tool, subAgentStatuses);
  return runId
    ? `${target.agentId}::${target.sessionId}::${runId}`
    : `${target.agentId}::${target.sessionId}`;
};

// A failed result fetch must not be cached forever (a transient inspection
// error would otherwise permanently blank the Result row), but deleting the
// entry would retrigger the fetch effect in a tight loop. Instead failed
// entries carry `error`/`failedAt` and become fetchable again after a cooldown,
// so the next natural re-render retries.
const SUBAGENT_RESULT_RETRY_DELAY_MS = 15000;

export const subAgentResultEntryAllowsFetch = (entry, now = Date.now()) => {
  if (!entry) {
    return true;
  }
  if (entry.error && !entry.loading) {
    const failedAt = Number.isFinite(entry.failedAt) ? entry.failedAt : 0;
    return now - failedAt >= SUBAGENT_RESULT_RETRY_DELAY_MS;
  }
  return false;
};

// A background spawn returns a "running" descriptor as its tool result, so the
// final output never lands in tool.result. Foreground spawns already carry it as
// data.result. When the child run has finished and no inline result exists, the
// durable work result must be inspected to show the response.
export const subAgentShouldFetchResult = (tool, dotStatus) => {
  if (!isSubAgentSpawnTool(tool)) {
    return false;
  }
  if (dotStatus !== 'success') {
    return false;
  }
  if (trimmedString(subAgentResultData(tool).result)) {
    return false;
  }
  const delivery = trimmedString(subAgentResultData(tool).delivery);
  if (
    delivery !== 'automatic' &&
    !(delivery === '' && subAgentArguments(tool).background === true)
  ) {
    return false;
  }
  return Boolean(subAgentNavigationTarget(tool));
};

// The sub-agent dot falls back to the frozen persisted descriptor's `status`
// (subAgentResultData.status) when no external `run:`/`session:` status has
// arrived. That fallback is what produces a "running" dot forever after a missed
// terminal event, a rolled replay buffer, or a server restart. When neither the
// `run:<run_id>` nor the `session:<agent_id>::<session_id>` key has been seen
// at all, the only thing telling us the child is still running is that frozen
// descriptor, and the Chat controller should verify the durable work record.
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
  // status may belong to a different run of the same (reused) child session,
  // so it must neither settle this row nor suppress its verification.
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return !Object.prototype.hasOwnProperty.call(statuses, `run:${runId}`);
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (
    agentId &&
    sessionId &&
    Object.prototype.hasOwnProperty.call(
      statuses,
      `session:${agentId}::${sessionId}`,
    )
  ) {
    return false;
  }

  return true;
};

// Returns the value to render in the tool's Result row. With a fetched result it
// rebuilds the same tool_success envelope a foreground spawn produces, so the
// fetched output renders identically; otherwise the original tool.result stands.
export const subAgentDisplayResult = (tool, fetchedResult = null) => {
  const resultText = trimmedString(fetchedResult?.result);
  if (!resultText) {
    return tool.result;
  }
  const target = subAgentNavigationTarget(tool) ?? {};
  const data = subAgentResultData(tool);
  const payload = {
    id: trimmedString(data.id),
    agent_id: target.agentId ?? trimmedString(data.agent_id),
    session_id: target.sessionId ?? subAgentSessionId(tool),
    status: 'completed',
    result: resultText,
  };
  const projectId = trimmedString(data.project_id);
  if (projectId) {
    payload.project_id = projectId;
  }
  if (fetchedResult?.usage) {
    payload.usage = fetchedResult.usage;
  }
  return { ok: true, error: null, data: payload, artifacts: [] };
};

// Extracts one terminal sub-agent Run's final response. A visible Assistant turn
// is not final until its exact Run Summary follows it.
export const subAgentResultTextFromMessages = (messages, runId = '') => {
  if (!Array.isArray(messages)) {
    return '';
  }

  let summaryIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (
      !isPlainObject(message) ||
      message.role !== 'run_summary' ||
      (runId && message.run_id !== runId)
    ) {
      continue;
    }
    summaryIndex = index;
    break;
  }
  if (summaryIndex < 0) {
    return '';
  }

  if (
    !runId &&
    messages
      .slice(summaryIndex + 1)
      .some((message) =>
        ['user', 'assistant', 'tool', 'error'].includes(message?.role),
      )
  ) {
    return '';
  }

  let segmentStart = 0;
  for (let index = summaryIndex - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'run_summary') {
      segmentStart = index + 1;
      break;
    }
  }

  for (let index = summaryIndex - 1; index >= segmentStart; index -= 1) {
    const message = messages[index];
    if (!isPlainObject(message) || message.role !== 'assistant') {
      continue;
    }
    const text = assistantMessageText(message.content);
    if (text) {
      return text;
    }
  }
  return '';
};

function assistantMessageText(content) {
  if (typeof content === 'string') {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .filter((block) => isTextContentBlock(block))
      .map((block) => block.text.trim())
      .join('\n\n')
      .trim();
  }
  return '';
}

function subAgentArguments(tool) {
  const parsedArguments = parseJsonValue(toolArguments(tool));
  if (!isPlainObject(parsedArguments)) {
    return {};
  }
  return isPlainObject(parsedArguments.request)
    ? parsedArguments.request
    : parsedArguments;
}

function subAgentResultEnvelope(tool) {
  const parsedResult = parseJsonValue(tool.result);
  return isPlainObject(parsedResult) ? parsedResult : {};
}

export function subAgentResultData(tool) {
  const sessionData = isPlainObject(tool.subAgentSession)
    ? tool.subAgentSession
    : {};
  const resultEnvelope = subAgentResultEnvelope(tool);
  if (isPlainObject(resultEnvelope.data)) {
    return { ...sessionData, ...resultEnvelope.data };
  }
  if (isPlainObject(resultEnvelope)) {
    return { ...sessionData, ...resultEnvelope };
  }
  return sessionData;
}

export function subAgentToolLabel(toolName, args) {
  const action = trimmedString(args.action);
  if (action && action !== 'run') {
    return [action, trimmedString(args.id)].filter(Boolean).join(' · ');
  }
  const agentId = trimmedString(args.agent_id);
  const preview = truncateToolLabel(
    trimmedString(args.content),
    MAX_SUBAGENT_PREVIEW_LENGTH,
  );
  return [agentId, preview].filter(Boolean).join(' · ');
}

function externalSubAgentStatus(tool, subAgentStatuses) {
  const statuses = isPlainObject(subAgentStatuses) ? subAgentStatuses : {};
  // With a known run id only the run-scoped status applies. The session-keyed
  // entry may describe an earlier or later run of the same reused child
  // session, so falling back to it would settle this row with another run's
  // terminal state.
  const runId = subAgentEffectiveRunId(tool, statuses);
  if (runId) {
    return subAgentStatusToDotStatus(
      trimmedString(statuses[`run:${runId}`]).toLowerCase(),
    );
  }

  const queueItemId = subAgentQueueItemId(tool);
  if (
    queueItemId &&
    Object.prototype.hasOwnProperty.call(statuses, `queue:${queueItemId}`)
  ) {
    return subAgentStatusToDotStatus(
      trimmedString(statuses[`queue:${queueItemId}`]).toLowerCase(),
    );
  }

  const agentId = subAgentTargetAddress(tool);
  const sessionId = subAgentSessionId(tool);
  if (!agentId || !sessionId) {
    return '';
  }
  return subAgentStatusToDotStatus(
    trimmedString(statuses[`session:${agentId}::${sessionId}`]).toLowerCase(),
  );
}

function subAgentChildStatus(tool) {
  const data = subAgentResultData(tool);
  const status = trimmedString(data.status).toLowerCase();
  if (status) {
    return status;
  }
  return trimmedString(tool.subAgentSession?.status).toLowerCase();
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
