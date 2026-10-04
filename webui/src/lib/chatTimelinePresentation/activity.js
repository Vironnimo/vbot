import {
  formatDurationMs,
  timestampToMs,
  elapsedSinceTimestamp,
  executionDetailRows,
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
  executionStateTitle,
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
  subAgentTask,
  subAgentNavigationTarget,
  isStartingForegroundSubAgent,
} from './subagents.js';

const MAX_BACKGROUND_COMMAND_LABEL_LENGTH = 96;

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

// Tooltip details behind the Run footer: the Run's state, what its "iter"
// count stands for (as the lead line, so the moment rows keep a narrow label
// column), when it started and finished, and how long it ran.
export const runFooterDetails = (assistantRun, nowMs = Date.now()) => {
  const running = assistantRun.status === 'running';
  const rows = executionDetailRows({
    startedAt: assistantRun.startTimestamp ?? assistantRun.timestamp,
    finishedAt: assistantRun.endTimestamp,
    durationMs: runDurationMs(assistantRun, nowMs),
    running,
    nowMs,
  });
  const iterationCount = assistantRun?.iterationCount;
  const text =
    !Number.isInteger(iterationCount) || iterationCount < 0
      ? ''
      : iterationCount === 1
        ? t('chat.details.modelResponseOne')
        : t('chat.details.modelResponseCount', { count: iterationCount });
  return { title: runStatusLabel(assistantRun.status), text, rows };
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
  backgroundCommandStatuses = {},
  commandStatuses = {},
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
        const commandRowState = backgroundCommandRowState(
          tool,
          backgroundCommandStatuses,
          commandStatuses,
        );
        if (commandRowState) {
          if (
            commandRowState.dotStatus === 'running' &&
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
  backgroundCommandStatuses = {},
  commandStatuses = {},
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
          taskText: subAgentTask(child),
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
      const commandRowState = backgroundCommandRowState(
        child,
        backgroundCommandStatuses,
        commandStatuses,
      );
      if (!commandRowState) {
        continue;
      }
      tasks.push({
        id: `command:${commandRowState.terminalId}`,
        kind: 'command',
        tool: child,
        dotStatus: commandRowState.dotStatus,
        label: commandRowState.command,
        command: commandRowState.command,
        fullCommand: commandRowState.fullCommand,
        terminalId: commandRowState.terminalId,
        rowState: commandRowState,
        target: null,
        timeLabel: backgroundCommandToolStatusLabel(
          child,
          commandRowState,
          nowMs,
        ),
        order,
      });
      order += 1;
    }
  }

  // A retained live Run can overlap its persisted History until reconciliation
  // confirms the full Run. Both describe the same background command; the panel
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

// Dot status of a handed-off command by its reported status.
const COMMAND_DOT_STATUSES = {
  running: 'running',
  completed: 'success',
  failed: 'failed',
  stopped: 'cancelled',
};

function knownCommandStatus(value) {
  const status = trimmedString(value);
  return Object.hasOwn(COMMAND_DOT_STATUSES, status) ? status : '';
}

// A `bash` Tool row whose command went on running in the background after the
// call returned represents that command, not the call. The row state resolves
// its terminal id and its status (live from `command_status_changed`, else
// durable from History) so the dot and the time label follow the command.
// Returns null for every other Tool row, and for a handed-off command whose
// status is unknown, such as one vBot no longer runs: that row stays a plain
// Tool row.
export const backgroundCommandRowState = (
  tool,
  backgroundCommandStatuses = {},
  commandStatuses = {},
) => {
  if (toolNameForRunTool(tool) !== 'bash') {
    return null;
  }
  const envelope = parseJsonValue(tool.result);
  const data = isPlainObject(envelope?.data) ? envelope.data : {};
  const terminalId = trimmedString(data.terminal_id);
  if (data.status !== 'running' || !terminalId) {
    return null;
  }
  const status =
    knownCommandStatus(commandStatuses?.[terminalId]) ||
    knownCommandStatus(backgroundCommandStatuses?.[terminalId]);
  if (!status) {
    return null;
  }
  const fullCommand = bashCommand(tool);
  return {
    terminalId,
    command: commandPreview(fullCommand),
    fullCommand,
    dotStatus: COMMAND_DOT_STATUSES[status],
  };
};

function bashCommand(tool) {
  const args = parseJsonValue(toolArguments(tool));
  return trimmedString(isPlainObject(args) ? args.command : '');
}

function commandPreview(command) {
  return (
    truncateToolLabel(
      command.replace(/\s+/g, ' '),
      MAX_BACKGROUND_COMMAND_LABEL_LENGTH,
    ) || t('chat.activity.commandFallback')
  );
}

// Tooltip details behind a handed-off command row's status: the command's
// state in words, its start and runtime while it runs, and its terminal id.
export const backgroundCommandStatusDetails = (
  tool,
  rowState,
  nowMs = Date.now(),
) => {
  if (!isPlainObject(rowState)) {
    return null;
  }
  const running = rowState.dotStatus === 'running';
  const startedAt = toolStartedTimestamp(tool);
  const rows = running
    ? executionDetailRows({
        startedAt,
        durationMs: elapsedSinceTimestamp(startedAt, nowMs),
        running,
        nowMs,
      })
    : [];
  rows.push({
    label: t('chat.details.terminal'),
    value: rowState.terminalId,
    mono: true,
  });
  return {
    title: running
      ? t('chat.toolState.background')
      : executionStateTitle(rowState.dotStatus),
    text: running ? t('chat.toolState.backgroundHint') : '',
    rows,
  };
};

// Status label for a handed-off command row. While the command runs the label
// ticks from the Tool call's own start timestamp (the command starts with the
// call, and this internal timing never reaches the Model). A stopped command
// reads as cancelled; otherwise the label stays empty, since the command's
// runtime is unknown and the call's own duration would mislead.
export const backgroundCommandToolStatusLabel = (
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
  if (rowState.dotStatus === 'cancelled') {
    return t('chat.toolCancelled');
  }
  return '';
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
    tool.startedEvent || tool.resultEvent || tool.output || tool.streaming,
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
  return formatDurationMs(runDurationMs(assistantRun, nowMs));
}

function runDurationMs(assistantRun, nowMs = Date.now()) {
  if (
    Number.isFinite(assistantRun.durationMs) &&
    assistantRun.durationMs >= 0
  ) {
    return assistantRun.durationMs;
  }
  const start = timestampToMs(
    assistantRun.startTimestamp ?? assistantRun.timestamp,
  );
  const end = timestampToMs(assistantRun.endTimestamp);
  if (start === null) {
    return null;
  }
  if (assistantRun.status === 'running') {
    return Math.max(0, nowMs - start);
  }
  if (end === null || end < start) {
    return null;
  }
  return end - start;
}
