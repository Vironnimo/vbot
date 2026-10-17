import { formatDurationMs, timestampToMs } from './time.js';
import { t } from '$lib/i18n.js';
import { toolDurationMs, toolStartedTimestamp } from './toolFacts.js';

// Compact Work details fold each contiguous span of at least two Thinking or
// Tool rows into one Working line; Assistant output, user messages and every
// other row stay in place, and a single Thinking or Tool row stays inline.
export function groupRunChildren(children, workingMode) {
  if (workingMode !== 'compact') {
    return children.map((child) => ({
      id: `child:${child.id}`,
      type: 'child',
      child,
    }));
  }

  const groups = [];
  let span = [];
  const flush = () => {
    if (span.length === 1) {
      groups.push({ id: `child:${span[0].id}`, type: 'child', child: span[0] });
    } else if (span.length > 1) {
      groups.push({
        id: `working:${span[0].id}`,
        type: 'working',
        children: span,
      });
    }
    span = [];
  };
  for (const child of children) {
    if (child.type === 'reasoning' || child.type === 'tool_call') {
      span.push(child);
      continue;
    }
    flush();
    groups.push({ id: `child:${child.id}`, type: 'child', child });
  }
  flush();
  return groups;
}

const MAX_NAMED_TOOLS = 3;

// The worst state among the group's rows decides its dot, in the order a
// reader must notice it: still running, failed, cut short, then done.
const STATUS_PRIORITY = [
  'running',
  'failed',
  'partial',
  'interrupted',
  'cancelled',
  'ended',
  'success',
];

// What one Working line says about its rows. `rowStatus(child)` is the dot
// state the Tool row itself shows and `toolName(child)` its displayed name;
// `active` marks the group holding a running Run's latest activity, whose
// duration keeps ticking with `nowMs`.
export function workingGroupSummary(
  group,
  { active = false, nowMs = Date.now(), rowStatus, toolName },
) {
  const tools = group.children.filter((child) => child.type === 'tool_call');
  const statuses = tools.map((tool) => rowStatus(tool));
  const names = [...new Set(tools.map((tool) => toolName(tool)))];
  const runningCount = statuses.filter((status) => status === 'running').length;
  const failedCount = statuses.filter((status) => status === 'failed').length;
  const status = active
    ? 'running'
    : tools.length === 0
      ? ''
      : (STATUS_PRIORITY.find((candidate) => statuses.includes(candidate)) ??
        'success');
  // The line names the step it stands for: the running Run's latest Tool, or
  // a finished group's only Tool, shown with its arguments like its own row.
  const latestTool = active
    ? tools.at(-1)
    : tools.length === 1
      ? tools[0]
      : null;
  const durationMs = workingGroupDurationMs(group.children, active, nowMs);
  return {
    status,
    active,
    latestTool: latestTool ?? null,
    toolNames: names.slice(0, MAX_NAMED_TOOLS),
    moreToolNames: Math.max(0, names.length - MAX_NAMED_TOOLS),
    toolCount: tools.length,
    thoughtCount: group.children.length - tools.length,
    failedCount,
    // Work still running in a finished group: Sub-Agents and background
    // commands that outlive the step that started them.
    runningCount: active ? 0 : runningCount,
    durationLabel: durationMs === null ? '' : formatDurationMs(durationMs),
  };
}

// The tooltip behind a Working line: counts the line leaves out.
export function workingGroupDetails(summary) {
  return {
    rows: [
      {
        label: t('chat.working.toolCalls'),
        value: String(summary.toolCount),
      },
      {
        label: t('chat.working.reasoning'),
        value: String(summary.thoughtCount),
      },
      {
        label: t('chat.working.failed'),
        value: String(summary.failedCount),
      },
      { label: t('chat.working.duration'), value: summary.durationLabel },
    ].filter((row) => row.value && row.value !== '0'),
  };
}

// How long the group's Tools ran, from the first start to the last end (or
// now while the group is active), so parallel calls count once. Thinking has
// no reliable start time in Session history, so a Thinking-only group adds up
// its own durations instead. Null when nothing is measurable.
function workingGroupDurationMs(children, active, nowMs) {
  const tools = children.filter((child) => child.type === 'tool_call');
  if (tools.length === 0) {
    const durations = children
      .map((child) => child.durationMs ?? child.durationEstimateMs)
      .filter((duration) => Number.isFinite(duration) && duration >= 0);
    return durations.length > 0
      ? durations.reduce((sum, duration) => sum + duration, 0)
      : null;
  }
  let start = null;
  let end = null;
  for (const tool of tools) {
    const from = timestampToMs(toolStartedTimestamp(tool));
    if (from === null) continue;
    const duration = toolDurationMs(tool);
    start = start === null ? from : Math.min(start, from);
    const to = duration === null ? from : from + duration;
    end = end === null ? to : Math.max(end, to);
  }
  if (start === null) return null;
  if (active) end = Math.max(end, nowMs);
  return end >= start ? end - start : null;
}
