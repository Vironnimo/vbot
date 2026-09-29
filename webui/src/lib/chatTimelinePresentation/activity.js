import {
  formatDurationMs,
  timestampToMs,
  elapsedSinceTimestamp,
  formatTime,
} from './time.js';
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';
import {
  toolNameForRunTool,
  toolArguments,
  toolStatus,
  isToolPreparing,
  toolStartedTimestamp,
} from './toolFacts.js';
import { trimmedString, parseJsonValue, truncateToolLabel } from './values.js';
import {
  subAgentRunStartedAt,
  isSubAgentSpawnTool,
  subAgentDotStatus,
  subAgentLastToolName,
  subAgentToolStatusLabel,
  isBackgroundSubAgentSpawn,
  subAgentAgentId,
  subAgentPreview,
  subAgentNavigationTarget,
  isStartingForegroundSubAgent,
} from './subagents.js';

const MAX_BACKGROUND_BASH_LABEL_LENGTH = 96;

export const visibleRunChildren = (assistantRun) =>
  (assistantRun.items ?? []).filter((child) => {
    if (child.type === 'tool_call') {
      return shouldRenderToolCall(child);
    }
    if (child.type === 'compaction_separator') {
      return true;
    }
    return Boolean(child.content);
  });

// `streaming` means a text section still comes from transient deltas and may
// later be replaced by its stable message. It does not mean that section is
// still the Run's current activity: once a later visible child exists, the
// stream has advanced and only that later child may carry the working caret.
export function isRunChildWorking(assistantRun, child) {
  if (assistantRun?.status !== 'running' || !child?.streaming || !child?.id) {
    return false;
  }

  const children = visibleRunChildren(assistantRun);
  const childIndex = children.findIndex(
    (candidate) => candidate.id === child.id,
  );
  return childIndex >= 0 && childIndex === children.length - 1;
}

// Duration label for one reasoning block header: the persisted first-to-last
// reasoning delta span once the stable boundary arrived, otherwise the frozen
// estimate from when the deltas stopped growing, otherwise a live estimate
// ticking with the shared nowMs clock while the deltas still stream.
// Returns '' when nothing is measurable (no timing, no streamed start time).
export function reasoningDurationLabel(child, nowMs = Date.now()) {
  const durationMs = child?.durationMs;
  if (Number.isFinite(durationMs) && durationMs >= 0) {
    return formatDurationMs(durationMs);
  }
  const estimateMs = child?.durationEstimateMs;
  if (Number.isFinite(estimateMs) && estimateMs >= 0) {
    return formatDurationMs(estimateMs);
  }
  if (!child?.streaming) {
    return '';
  }
  const start = timestampToMs(child.timestamp);
  if (start === null) {
    return '';
  }
  return formatDurationMs(Math.max(0, nowMs - start));
}

// Footer line for an assistant run: status · duration · iterations · end
// time. The duration ticks live while the run is running (driven by the
// shared nowMs clock); the end time appears once the run reaches a terminal
// state (completion, cancellation, failure) and uses the same clock-time
// formatting as the message header timestamp. This is the stable first line
// of run-level meta information; the message header shows only the start
// timestamp. File-change statistics are appended to this same line via
// `changeStatsParts`, while transient problem notices (provider liveness)
// render on a separate line below via `runFooterNotice` so they can never
// push the stable parts onto a wrap line.
export const runFooterParts = (assistantRun, nowMs = Date.now()) => {
  const parts = [];
  parts.push(runStatusLabel(assistantRun.status));
  const duration = formatRunDuration(assistantRun, nowMs);
  if (duration) {
    parts.push(duration);
  }
  const iterationLabel = labelForRunIterations(assistantRun);
  if (iterationLabel) {
    parts.push(iterationLabel);
  }
  const endTimeLabel = runEndTimeLabel(assistantRun);
  if (endTimeLabel) {
    parts.push(endTimeLabel);
  }
  return parts;
};

// SSE keepalive comments from gateways like OpenRouter arrive every few
// seconds even while Model deltas stream, so a heartbeat with a tiny idle
// value is normal streaming traffic, not a problem. The notice only appears
// once a full minute passes without Model output. Short reasoning pauses and
// the start of an ordinary request do not need a second status line.
const PROVIDER_IDLE_NOTICE_THRESHOLD_SECONDS = 60;

// Transient problem/liveness notice for an assistant run, rendered on its own
// line below the footer. Returns '' when there is nothing to report.
export const runFooterNotice = (assistantRun, nowMs = Date.now()) => {
  const request = assistantRun.providerRequestStatus;
  if (assistantRun.status === 'running' && request) {
    const waitingMs = elapsedSinceTimestamp(request.timestamp, nowMs);
    if (
      request.state !== 'retrying' &&
      !request.error_kind &&
      (waitingMs === null ||
        waitingMs < PROVIDER_IDLE_NOTICE_THRESHOLD_SECONDS * 1000)
    ) {
      return '';
    }
    const reason =
      request.error_kind === 'timeout'
        ? t('chat.requestTimeout')
        : request.error_kind === 'rate_limit'
          ? t('chat.requestRateLimit')
          : request.error_kind === 'network_error'
            ? t('chat.requestNetwork')
            : request.error_kind
              ? t('chat.requestFailed')
              : '';
    const status =
      request.state === 'retrying'
        ? t('chat.requestRetrying')
        : t('chat.requestWaiting');
    const attempt =
      Number.isInteger(request.attempt) &&
      Number.isInteger(request.max_attempts)
        ? t('chat.requestAttempt', {
            attempt: request.attempt,
            total: request.max_attempts,
          })
        : '';
    return [reason, status, attempt].filter(Boolean).join(' ');
  }
  const idleSeconds = assistantRun.providerHeartbeat?.idleSeconds;
  if (
    assistantRun.status === 'running' &&
    Number.isFinite(idleSeconds) &&
    idleSeconds >= PROVIDER_IDLE_NOTICE_THRESHOLD_SECONDS
  ) {
    return t('chat.providerWorking', { seconds: Math.round(idleSeconds) });
  }
  return '';
};

export const liveClockCadenceMs = (
  timelineItems,
  subAgentStatuses = {},
  nowMs = Date.now(),
  backgroundBashProcesses = {},
) => {
  const starts = [];
  for (const assistantRun of timelineItems ?? []) {
    if (assistantRun?.type !== 'assistant_run') {
      continue;
    }
    if (assistantRun.status === 'running') {
      starts.push(assistantRun.startTimestamp ?? assistantRun.timestamp);
    }
    for (const tool of assistantRun.items ?? []) {
      if (tool?.type !== 'tool_call') {
        continue;
      }
      if (isSubAgentSpawnTool(tool)) {
        if (subAgentDotStatus(tool, subAgentStatuses) === 'running') {
          starts.push(subAgentRunStartedAt(tool, subAgentStatuses));
        }
      } else {
        const bashRowState = backgroundBashRowState(
          tool,
          {},
          backgroundBashProcesses,
        );
        if (bashRowState) {
          if (
            bashRowState.dotStatus === 'running' &&
            timestampToMs(toolStartedTimestamp(tool)) !== null
          ) {
            starts.push(toolStartedTimestamp(tool));
          }
        } else if (toolStatus(tool) === 'running' && !isToolPreparing(tool)) {
          starts.push(toolStartedTimestamp(tool));
        }
      }
    }
  }
  const validStarts = starts
    .map(timestampToMs)
    .filter((value) => value !== null);
  if (validStarts.length === 0) {
    return 0;
  }
  return validStarts.some(
    (startedAt) => Math.max(0, nowMs - startedAt) < 10_000,
  )
    ? 100
    : 1000;
};

// Single source of truth for whether a tool/sub-agent row should render a
// per-row cancel control. The row shape is intentionally narrow so the rule
// stays testable and easy to extend (e.g. when other tools gain a cancel).
export const isRowCancellable = (row) => {
  if (!isPlainObject(row)) {
    return false;
  }
  if (row.kind === 'tool_call') {
    // A streaming row only previews a tool call the model is still writing;
    // there is no dispatched call to cancel yet.
    return (
      row.toolName === 'bash' &&
      row.toolStatus === 'running' &&
      row.streaming !== true
    );
  }
  if (row.kind === 'sub_agent') {
    return row.dotStatus === 'running';
  }
  return false;
};

export const backgroundTasks = (
  timelineItems,
  subAgentStatuses = {},
  backgroundBashStatuses = {},
  backgroundBashProcesses = {},
  nowMs = Date.now(),
) => {
  const tasks = [];
  let order = 0;
  for (const [itemIndex, item] of (timelineItems ?? []).entries()) {
    if (item?.type !== 'assistant_run') {
      continue;
    }
    for (const [childIndex, child] of visibleRunChildren(item).entries()) {
      if (child?.type !== 'tool_call') {
        continue;
      }
      if (isBackgroundSubAgentSpawn(child)) {
        const dotStatus = subAgentDotStatus(child, subAgentStatuses);
        tasks.push({
          id: `${item.id ?? itemIndex}:${child.id ?? child.toolCallId ?? childIndex}`,
          kind: 'subagent',
          tool: child,
          dotStatus,
          label: subAgentAgentId(child),
          agentId: subAgentAgentId(child),
          preview: subAgentPreview(child),
          target: subAgentNavigationTarget(child),
          lastToolName:
            dotStatus === 'running'
              ? subAgentLastToolName(child, subAgentStatuses)
              : '',
          timeLabel: subAgentToolStatusLabel(
            child,
            dotStatus,
            subAgentStatuses,
          ),
          order,
        });
        order += 1;
        continue;
      }
      const bashRowState = backgroundBashRowState(
        child,
        backgroundBashStatuses,
        backgroundBashProcesses,
      );
      if (!bashRowState) {
        continue;
      }
      tasks.push({
        id: `bash:${bashRowState.processId}`,
        kind: 'bash',
        tool: child,
        dotStatus: bashRowState.dotStatus,
        label: bashRowState.command,
        command: bashRowState.command,
        processId: bashRowState.processId,
        target: null,
        timeLabel: backgroundBashToolStatusLabel(child, bashRowState, nowMs),
        order,
      });
      order += 1;
    }
  }

  // A retained live Run can overlap its persisted History until reconciliation
  // confirms the full Run. Both describe the same background process; the panel
  // must show it once, otherwise its keyed rows cannot mount when opened.
  // Prefer the latest occurrence, keeping its current Tool projection and order.
  return [...new Map(tasks.map((task) => [task.id, task])).values()]
    .sort((left, right) => {
      const activeDifference =
        Number(right.dotStatus === 'running') -
        Number(left.dotStatus === 'running');
      return activeDifference || right.order - left.order;
    })
    .map(({ order: _order, ...task }) => task);
};

// A handed-off Bash Tool row represents the long-running process, not the
// ~instant delegation call. The row state resolves the process id plus the
// combined live/durable process status so the dot, the time label, and the
// displayed result can all follow the process instead of the handoff
// envelope. Returns null for every other Tool row.
export const backgroundBashRowState = (
  tool,
  backgroundBashStatuses = {},
  backgroundBashProcesses = {},
) => {
  if (toolNameForRunTool(tool) !== 'bash') {
    return null;
  }
  const envelope = parseJsonValue(tool.result);
  const data = isPlainObject(envelope?.data) ? envelope.data : {};
  const processId = trimmedString(data.process_id);
  if (data.delivery !== 'automatic' || !processId) {
    return null;
  }
  const terminal = isPlainObject(backgroundBashProcesses?.[processId])
    ? backgroundBashProcesses[processId]
    : null;
  const durableStatus = trimmedString(backgroundBashStatuses?.[processId]);
  const status =
    trimmedString(terminal?.status) ||
    durableStatus ||
    trimmedString(data.status) ||
    'running';
  return {
    processId,
    command: commandPreview(tool),
    dotStatus: backgroundBashDotStatus(status),
    terminal,
  };
};

function commandPreview(tool) {
  const args = parseJsonValue(toolArguments(tool));
  const command = truncateToolLabel(
    trimmedString(isPlainObject(args) ? args.command : '').replace(/\s+/g, ' '),
    MAX_BACKGROUND_BASH_LABEL_LENGTH,
  );
  return command || t('chat.activity.bashFallback');
}

// Status label for a handed-off Bash row. While the process runs the label
// ticks from the Tool call's own start timestamp (the command spawns at call
// start, and this internal timing never reaches the Model). Once the process
// is terminal the label becomes the real runtime measured by the terminal
// notification; without known times the label stays empty rather than showing
// the misleading ~0s delegation-call duration.
export const backgroundBashToolStatusLabel = (
  tool,
  rowState,
  nowMs = Date.now(),
) => {
  if (!isPlainObject(rowState)) {
    return '';
  }
  if (rowState.dotStatus === 'running') {
    return formatDurationMs(
      elapsedSinceTimestamp(toolStartedTimestamp(tool), nowMs),
    );
  }
  const durationMs = backgroundBashDurationMs(rowState.terminal);
  if (rowState.dotStatus === 'cancelled') {
    return [
      t('chat.toolCancelled'),
      durationMs !== null ? formatDurationMs(durationMs) : '',
    ]
      .filter(Boolean)
      .join(' · ');
  }
  if (durationMs !== null) {
    return formatDurationMs(durationMs);
  }
  return '';
};

function backgroundBashDurationMs(terminal) {
  if (!isPlainObject(terminal)) {
    return null;
  }
  const startedMs = timestampToMs(terminal.startedAt);
  const finishedMs = timestampToMs(terminal.finishedAt);
  if (startedMs === null || finishedMs === null || finishedMs < startedMs) {
    return null;
  }
  return finishedMs - startedMs;
}

// Returns the value to render in the Tool's Result row. With terminal process
// data the handoff envelope is replaced by the actual completion result, the
// same swap the Sub-Agent row makes once its final result was fetched.
export const backgroundBashDisplayResult = (tool, rowState) => {
  const terminal = rowState?.terminal;
  if (!isPlainObject(terminal)) {
    return tool.result;
  }
  const data = {
    status: terminal.status,
    exit_code: typeof terminal.exitCode === 'number' ? terminal.exitCode : null,
    output: terminal.output ?? '',
    truncated: terminal.truncated === true,
  };
  if (terminal.cancelledByUser) {
    data.cancelled_by_user = true;
  }
  if (terminal.logFile) {
    data.log_file = terminal.logFile;
  }
  return JSON.stringify({ ok: true, error: null, data });
};

function backgroundBashDotStatus(status) {
  if (status === 'completed' || status === 'success') {
    return 'success';
  }
  if (status === 'failed') {
    return 'failed';
  }
  if (status === 'killed' || status === 'cancelled') {
    return 'cancelled';
  }
  return 'running';
}

// Reflection reviews execute in a same-Agent fork whose lifecycle events carry
// the reviewed source session. These helpers map the run-kind vocabulary onto
// Activity-panel presentation and project the per-source tracking entries
// written by chatRunStream into sortable panel rows.
const REFLECTION_RUN_KIND_SCOPES = {
  memory_reflection: 'memory',
  skill_reflection: 'skill',
  reflection: 'combined',
};

export const isReflectionRunKind = (runKind) =>
  typeof runKind === 'string' && runKind in REFLECTION_RUN_KIND_SCOPES;

const reflectionScopeForRunKind = (runKind) =>
  isReflectionRunKind(runKind) ? REFLECTION_RUN_KIND_SCOPES[runKind] : '';

export const reflectionTaskRows = (sessionState) => {
  const entries = isPlainObject(sessionState?.reflectionTasks)
    ? sessionState.reflectionTasks
    : {};
  return Object.entries(entries)
    .filter(
      ([, entry]) =>
        isPlainObject(entry) &&
        typeof entry.sessionId === 'string' &&
        entry.sessionId.length > 0,
    )
    .map(([runId, entry]) => ({
      runId,
      sessionId: entry.sessionId,
      runKind: entry.runKind,
      scope: reflectionScopeForRunKind(entry.runKind),
      status:
        typeof entry.status === 'string' && entry.status
          ? entry.status
          : 'running',
      startedAt: typeof entry.startedAt === 'string' ? entry.startedAt : '',
    }))
    .sort((left, right) => {
      const activeDifference =
        Number(right.status === 'running') - Number(left.status === 'running');
      return (
        activeDifference ||
        (Date.parse(right.startedAt) || 0) - (Date.parse(left.startedAt) || 0)
      );
    });
};

// Coarse elapsed time for a running review; the panel re-renders it from a
// ticking clock only while the panel is open with running reflections.
export const reflectionElapsedLabel = (startedAt, nowMs) => {
  const startedMs = Date.parse(startedAt);
  if (Number.isNaN(startedMs) || !Number.isFinite(nowMs)) {
    return '';
  }
  const elapsedMs = Math.max(0, nowMs - startedMs);
  if (elapsedMs < 60_000) {
    return t('chat.activity.reflectionElapsedSeconds', {
      count: Math.floor(elapsedMs / 1000),
    });
  }
  return t('chat.activity.reflectionElapsedMinutes', {
    count: Math.floor(elapsedMs / 60_000),
  });
};

function shouldRenderToolCall(tool) {
  if (isSubAgentSpawnTool(tool)) {
    return Boolean(
      subAgentNavigationTarget(tool) ||
      tool.resultEvent ||
      isStartingForegroundSubAgent(tool),
    );
  }
  return Boolean(
    tool.startedEvent ||
    tool.resultEvent ||
    tool.stdout ||
    tool.stderr ||
    tool.streaming,
  );
}

function labelForRunIterations(assistantRun) {
  const iterationCount = assistantRun?.iterationCount;
  if (!Number.isInteger(iterationCount) || iterationCount < 0) {
    return '';
  }
  return t('chat.runIterations', {
    count: iterationCount,
  });
}

function runStatusLabel(status) {
  if (status === 'failed') {
    return t('chat.runStatus.failed');
  }
  if (status === 'cancelled') {
    return t('chat.runStatus.cancelled');
  }
  if (status === 'interrupted') {
    return t('chat.runStatus.interrupted');
  }
  if (status === 'completed' || status === 'success') {
    return t('chat.runStatus.completed');
  }
  return t('chat.runStatus.running');
}

// Clock time when the run ended (e.g. "7:20 PM"), shown once the run reached
// a terminal state — completion, user cancellation, or failure — with the
// same formatting as the message header timestamp. Empty while running or
// when no end timestamp is known.
function runEndTimeLabel(assistantRun) {
  if (assistantRun.status === 'running') {
    return '';
  }
  return formatTime(assistantRun.endTimestamp);
}

function formatRunDuration(assistantRun, nowMs = Date.now()) {
  const durationFromTiming = formatDurationMs(assistantRun.durationMs);
  if (durationFromTiming) {
    return durationFromTiming;
  }
  const start = timestampToMs(
    assistantRun.startTimestamp ?? assistantRun.timestamp,
  );
  const end = timestampToMs(assistantRun.endTimestamp);
  if (start === null) {
    return '';
  }
  if (assistantRun.status === 'running') {
    return formatDurationMs(Math.max(0, nowMs - start));
  }
  if (end === null || end < start) {
    return '';
  }
  return formatDurationMs(end - start);
}
