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
import { toolStatusDetails, toolStatusLabel } from './toolRows.js';
import {
  subAgentRunStartedAt,
  isSubAgentSpawnTool,
  isSubAgentTargetTool,
  subAgentDotStatus,
  subAgentLastToolName,
  subAgentToolStatusLabel,
  subAgentAgentId,
  subAgentDescription,
  subAgentResultData,
  subAgentNavigationTarget,
} from './subagents.js';

const MAX_BACKGROUND_COMMAND_LABEL_LENGTH = 96;

export const visibleRunChildren = (assistantRun) =>
  (assistantRun.items ?? []).filter((child) => {
    if (child.type === 'tool_call') {
      return shouldRenderToolCall(child);
    }
    if (
      child.type === 'compaction_separator' ||
      child.type === 'history_gap' ||
      child.type === 'goal_event' ||
      child.type === 'subagent_message'
    ) {
      return true;
    }
    return Boolean(child.content);
  });

// The running Run's latest visible child, or null once the Run ended. A
// viewport part of a long Run (`chatTimeline.js` `timelineRowsForViewport`)
// holds only some children and carries the whole Run's as `runItems`, so a
// part never mistakes its own last child for the Run's current activity.
export function latestRunActivity(assistantRun) {
  if (assistantRun?.status !== 'running') return null;
  return (
    visibleRunChildren({
      items: assistantRun.runItems ?? assistantRun.items,
    }).at(-1) ?? null
  );
}

// `streaming` means a text section still comes from transient deltas and may
// later be replaced by its stable message. It does not mean that section is
// still the Run's current activity: once a later visible child exists, the
// stream has advanced and only that later child may carry the working caret.
export function isRunChildWorking(assistantRun, child) {
  if (assistantRun?.status !== 'running' || !child?.streaming || !child?.id) {
    return false;
  }
  return latestRunActivity(assistantRun)?.id === child.id;
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
          // Processes left running show no ticking time.
          if (
            commandRowState.dotStatus === 'running' &&
            !commandRowState.stillRunning &&
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

// A Sub-Agent's task stays running while background commands it or its own
// Sub-Agents started still run after its Run (`subAgentCommands`, from
// `runningSubAgentCommands`); `workRunning` marks that only those run, and
// `runDotStatus` is the state of its Run alone.
export const backgroundTasks = (
  timelineItems,
  subAgentStatuses = {},
  backgroundCommandStatuses = {},
  commandStatuses = {},
  subAgentCommands = {},
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
      // A refused `run` started no Sub-Agent; only started ones are work.
      if (isSubAgentSpawnTool(child) && subAgentNavigationTarget(child)) {
        const runDotStatus = subAgentDotStatus(child, subAgentStatuses);
        const subAgentId = trimmedString(subAgentResultData(child).id);
        const runningCommands =
          (subAgentId && subAgentCommands?.[subAgentId]) || [];
        const workRunning =
          runDotStatus !== 'running' && runningCommands.length > 0;
        tasks.push({
          subAgentId,
          id: subAgentId
            ? `subagent:${subAgentId}`
            : `${item.id ?? itemIndex}:${child.id ?? child.toolCallId ?? childIndex}`,
          kind: 'subagent',
          tool: child,
          dotStatus: workRunning ? 'running' : runDotStatus,
          runDotStatus,
          workRunning,
          runningCommands,
          label: subAgentAgentId(child),
          agentId: subAgentAgentId(child),
          description: subAgentDescription(child),
          target: subAgentNavigationTarget(child),
          lastToolName:
            runDotStatus === 'running'
              ? subAgentLastToolName(child, subAgentStatuses)
              : '',
          timeLabel: workRunning
            ? t('chat.subAgentCommandsRunning')
            : subAgentToolStatusLabel(child, runDotStatus, subAgentStatuses),
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
        id: `command:${commandRowState.commandId}`,
        kind: 'command',
        tool: child,
        dotStatus: commandRowState.dotStatus,
        label: commandRowState.description || commandRowState.command,
        description: commandRowState.description,
        command: commandRowState.command,
        fullCommand: commandRowState.fullCommand,
        commandId: commandRowState.commandId,
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
  // confirms the full Run. Both describe the same Sub-Agent or background
  // command; the panel must show it once, otherwise its keyed rows cannot mount
  // when opened. Prefer the latest occurrence, keeping its current Tool
  // projection and order.
  return [...new Map(tasks.map((task) => [task.id, task])).values()]
    .sort((left, right) => {
      const activeDifference =
        Number(right.dotStatus === 'running') -
        Number(left.dotStatus === 'running');
      return activeDifference || right.order - left.order;
    })
    .map(({ order: _order, ...task }) => task);
};

// The Session-info panel lists all background work in two sections: what still
// runs, then what finished. Sub-Agents, commands and Reflections mix within
// each, newest start first; entries without a known start sink to the end in
// their incoming order.
export const activitySections = (tasks = [], reflectionRows = []) => {
  const entries = [
    ...(tasks ?? []).map((task) => ({
      key: task.id,
      kind: task.kind,
      task,
      running: task.dotStatus === 'running',
      startedMs: timestampToMs(toolStartedTimestamp(task.tool)),
    })),
    ...(reflectionRows ?? []).map((row) => ({
      key: `reflection:${row.runId}`,
      kind: 'reflection',
      row,
      running: row.status === 'running',
      startedMs: timestampToMs(row.startedAt),
    })),
  ].sort(
    (left, right) =>
      (right.startedMs ?? -Infinity) - (left.startedMs ?? -Infinity) || 0,
  );
  return {
    running: entries.filter((entry) => entry.running),
    finished: entries.filter((entry) => !entry.running),
  };
};

// How far a background command got: still running, ended in an unknown way
// (`ended`, the WebUI's own state), or ended with a known status. A command
// never runs again once it ended, and processes it left running never start
// again, so of two reports of one command the further one is newer.
const COMMAND_PROGRESS = {
  running: 0,
  ended: 1,
  completed: 2,
  failed: 2,
  stopped: 2,
};
const COMMAND_ENDED_WITH_STATUS = 2;

// Dot status of a background command by its status.
const COMMAND_DOT_STATUSES = {
  running: 'running',
  ended: 'ended',
  completed: 'success',
  failed: 'failed',
  stopped: 'cancelled',
};
// A background command without a reported status has ended: the server
// reports every command it still runs or knows the end of, so one it no longer
// knows (an old command, or one vBot forgot after its end) never runs again.
// Only how it ended is unknown.
const COMMAND_DOT_ENDED = 'ended';
const COMMAND_STATE_ENDED = { status: 'ended', stillRunning: false };
const COMMAND_STATE_RUNNING = { status: 'running', stillRunning: false };

function commandState(value) {
  if (!isPlainObject(value)) {
    return null;
  }
  const status = trimmedString(value.status);
  if (!Object.hasOwn(COMMAND_PROGRESS, status)) {
    return null;
  }
  return {
    status,
    stillRunning:
      COMMAND_PROGRESS[status] === COMMAND_ENDED_WITH_STATUS &&
      value.stillRunning === true,
  };
}

// The newer of two reports of one background command's state
// (`{ status, stillRunning }`, where `stillRunning` marks processes it
// started that still run after its shell exited), or null when neither is
// one. Live events, the History's statuses and the replies of stop and
// output reads arrive in no common order; the state that got further wins.
export function laterCommandState(left, right) {
  const leftState = commandState(left);
  const rightState = commandState(right);
  if (!leftState || !rightState) {
    return leftState ?? rightState;
  }
  const progress =
    COMMAND_PROGRESS[leftState.status] - COMMAND_PROGRESS[rightState.status];
  if (progress !== 0) {
    return progress > 0 ? leftState : rightState;
  }
  return {
    status: leftState.status,
    stillRunning: leftState.stillRunning && rightState.stillRunning,
  };
}

// The background commands a Session's Sub-Agents (at every depth) still run,
// by the id of the Session's own Sub-Agent above them, as `{ kind: 'command',
// id, label }`: those its last History read listed (`{ subAgentId,
// sessionId, commandId, label }`, all running or leaving processes running
// then) that no live state of their own Session (`commandStatuses`, by
// Session id) shows ended since.
export const runningSubAgentCommands = (entries, commandStatuses = {}) => {
  const bySubAgent = {};
  for (const entry of Array.isArray(entries) ? entries : []) {
    const state = laterCommandState(
      COMMAND_STATE_RUNNING,
      commandStatuses?.[entry?.sessionId]?.[entry?.commandId],
    );
    if (state.status === 'running' || state.stillRunning) {
      (bySubAgent[entry.subAgentId] ??= []).push({
        kind: 'command',
        id: entry.commandId,
        label: entry.label,
      });
    }
  }
  return bySubAgent;
};

// The end a `bash` result shows itself: a call that returned once its shell
// exited names a command only when processes it started still ran.
function callEndState(data) {
  if (data.status === 'stopped') {
    return { status: 'stopped', stillRunning: false };
  }
  if (data.status === 'exited') {
    return {
      status: data.exit_code === 0 ? 'completed' : 'failed',
      stillRunning: false,
    };
  }
  return null;
}

// A `bash` Tool row that names a background command represents that command,
// not only the call: a command that went on running in the background after
// the call returned, or one whose shell exited while processes it started
// still ran. The row state resolves the command id and the command's state
// from the live statuses (`command_status_changed` and the replies of stop
// and output reads) and the durable ones from History, whichever got further,
// so the dot and the time label follow the command. Processes still running
// show as `dotStatus` `running` with `stillRunning`; a handed-off command
// with no status has ended (`dotStatus` `ended`). Returns null for every
// other Tool row, and in a view that follows no command statuses (both maps
// null, such as the Swarm page's Activity): there the row stays a plain Tool
// row. Both maps hold `{ status, stillRunning }` by command id.
export const backgroundCommandRowState = (
  tool,
  backgroundCommandStatuses = {},
  commandStatuses = {},
) => {
  if (
    toolNameForRunTool(tool) !== 'bash' ||
    (!isPlainObject(backgroundCommandStatuses) &&
      !isPlainObject(commandStatuses))
  ) {
    return null;
  }
  const envelope = parseJsonValue(tool.result);
  const data = isPlainObject(envelope?.data) ? envelope.data : {};
  const commandId = trimmedString(data.command_id);
  const callEnd = callEndState(data);
  if (!commandId || (data.status !== 'running' && !callEnd)) {
    return null;
  }
  const known = laterCommandState(
    commandStatuses?.[commandId],
    backgroundCommandStatuses?.[commandId],
  );
  const state =
    known && COMMAND_PROGRESS[known.status] === COMMAND_ENDED_WITH_STATUS
      ? known
      : (callEnd ?? known ?? COMMAND_STATE_ENDED);
  const fullCommand = bashArgument(tool, 'command');
  return {
    commandId,
    command: commandPreview(fullCommand),
    fullCommand,
    description: bashArgument(tool, 'description').replace(/\s+/g, ' '),
    dotStatus: state.stillRunning
      ? 'running'
      : COMMAND_DOT_STATUSES[state.status],
    stillRunning: state.stillRunning,
    callEnded: Boolean(callEnd),
  };
};

function bashArgument(tool, name) {
  const args = parseJsonValue(toolArguments(tool));
  return trimmedString(isPlainObject(args) ? args[name] : '');
}

function commandPreview(command) {
  return (
    truncateToolLabel(
      command.replace(/\s+/g, ' '),
      MAX_BACKGROUND_COMMAND_LABEL_LENGTH,
    ) || t('chat.activity.commandFallback')
  );
}

// Tooltip details behind a background command row's status: the command's
// state in words, its start and runtime while it or processes it started
// run, the call's own timing once a call that ran to its end has nothing left
// running, and the command id.
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
    : rowState.callEnded
      ? [...(toolStatusDetails(tool, nowMs).rows ?? [])]
      : [];
  rows.push({
    label: t('chat.details.commandId'),
    value: rowState.commandId,
    mono: true,
  });
  return {
    title: rowState.stillRunning
      ? t('chat.toolState.stillRunning')
      : running
        ? t('chat.toolState.background')
        : executionStateTitle(rowState.dotStatus),
    text: rowState.stillRunning
      ? t('chat.toolState.stillRunningHint')
      : running
        ? t('chat.toolState.backgroundHint')
        : rowState.dotStatus === COMMAND_DOT_ENDED
          ? t('chat.toolState.endedHint')
          : '',
    rows,
  };
};

// Status label for a background command row. While the command runs the label
// ticks from the Tool call's own start timestamp (the command starts with the
// call, and this internal timing never reaches the Model); processes left
// running after the shell exited say so instead. A call that ran to its end
// shows its own label once nothing of it runs. A handed-off command that was
// stopped reads as cancelled; otherwise the label stays empty, since the
// command's runtime is unknown and the call's own duration would mislead.
export const backgroundCommandToolStatusLabel = (
  tool,
  rowState,
  nowMs = Date.now(),
) => {
  if (!isPlainObject(rowState)) {
    return '';
  }
  if (rowState.stillRunning) {
    return t('chat.toolStillRunning');
  }
  if (rowState.dotStatus === 'running') {
    return formatDurationMs(
      elapsedSinceTimestamp(toolStartedTimestamp(tool), nowMs),
    );
  }
  if (rowState.callEnded) {
    return toolStatusLabel(tool, nowMs);
  }
  if (rowState.dotStatus === 'cancelled') {
    return t('chat.toolCancelled');
  }
  return '';
};

// A Sub-Agent row appears once it names its Sub-Agent's Session (the live
// start event or the result), or once the call ended without one.
function shouldRenderToolCall(tool) {
  if (isSubAgentTargetTool(tool)) {
    return Boolean(
      subAgentNavigationTarget(tool) ||
      tool.resultEvent ||
      ['failed', 'cancelled', 'interrupted'].includes(toolStatus(tool)),
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
