import { formatDurationMs, timestampToMs } from './time.js';
import { t } from '$lib/i18n.js';
import {
  toolDurationMs,
  toolNameForRunTool,
  toolStartedTimestamp,
} from './toolFacts.js';
import { reasoningDurationMs, visibleRunChildren } from './activity.js';
import { isShowWidgetTool } from './widgets.js';
import { isPortalTool } from './portals.js';

// Speech, widgets and Portal cards are output for the user, like an Agent
// message. Keep their rows outside Working from the start of the call so
// arriving media never moves or remounts a player inside a folded group.
const isWorkingChild = (child) =>
  child.type === 'reasoning' ||
  (child.type === 'tool_call' &&
    toolNameForRunTool(child) !== 'generate_speech' &&
    !isShowWidgetTool(child) &&
    !isPortalTool(child));

// Compact Work details fold each contiguous span of at least two Thinking or
// Tool rows into one Working line; Assistant output, speech, widgets, Portal cards,
// user messages and every other row stay in place, and a single Thinking or
// Tool row stays inline.
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
    if (isWorkingChild(child)) {
      span.push(child);
      continue;
    }
    flush();
    groups.push({ id: `child:${child.id}`, type: 'child', child });
  }
  flush();
  return groups;
}

// The first row of the Working span each later child of a Run continues, by
// child id; hidden children inside a span continue it too. The viewport keeps
// a span with its first row, so a long Run is never cut inside a Working group.
export function workingSpanStarts(run) {
  const visible = new Set(visibleRunChildren(run));
  const starts = new Map();
  let start = null;
  for (const child of run.items ?? []) {
    if (visible.has(child) && !isWorkingChild(child)) {
      start = null;
    } else if (start) {
      starts.set(child.id, start);
    } else if (visible.has(child)) {
      start = child;
    }
  }
  return starts;
}

const MAX_NAMED_TOOLS = 3;

// What one Working line says about its rows. `rowStatus(child)` is the state
// the Tool row itself shows and `toolName(child)` its displayed name; `active`
// marks the group holding a running Run's latest activity, whose duration
// keeps ticking with `nowMs`, and `thinking` that this activity is Thinking.
// The line names the Tools used, most used first.
export function workingGroupSummary(
  group,
  { active = false, nowMs = Date.now(), rowStatus, toolName },
) {
  const tools = group.children.filter((child) => child.type === 'tool_call');
  const statuses = tools.map((tool) => rowStatus(tool));
  const callsByName = new Map();
  for (const tool of tools) {
    const name = toolName(tool);
    callsByName.set(name, (callsByName.get(name) ?? 0) + 1);
  }
  // A stable sort keeps equally used Tools in the order they were first used.
  const kinds = [...callsByName]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count);
  const durationMs = workingGroupDurationMs(group.children, active, nowMs);
  return {
    active,
    thinking: active && group.children.at(-1)?.type === 'reasoning',
    toolKinds: kinds.slice(0, MAX_NAMED_TOOLS),
    otherToolKinds: kinds.slice(MAX_NAMED_TOOLS),
    toolCount: tools.length,
    thoughtCount: group.children.length - tools.length,
    failedCount: statuses.filter((status) => status === 'failed').length,
    // Work still running in a finished group: Sub-Agents and background
    // commands that outlive the step that started them.
    runningCount: active
      ? 0
      : statuses.filter((status) => status === 'running').length,
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
        label: t('chat.working.otherTools'),
        value: summary.otherToolKinds
          .map(({ name, count }) => (count > 1 ? `${name} ${count}` : name))
          .join(', '),
      },
      {
        label: t('chat.working.failed'),
        value: String(summary.failedCount),
      },
      {
        label: t('chat.working.stillRunning'),
        value: String(summary.runningCount),
      },
      { label: t('chat.working.duration'), value: summary.durationLabel },
    ].filter((row) => row.value && row.value !== '0'),
  };
}

// Add Thinking and the elapsed span of each consecutive batch of Tools.
// Parallel calls count once; Thinking between batches is not counted again
// as a gap between Tools. History does not retain a Thinking start time,
// so use the same measured/frozen/live duration as each Thinking row.
function workingGroupDurationMs(children, active, nowMs) {
  let total = null;
  let tools = [];
  const add = (duration) => {
    if (duration !== null) total = (total ?? 0) + duration;
  };
  for (const child of children) {
    if (child.type === 'tool_call') {
      tools.push(child);
    } else {
      add(toolBatchDurationMs(tools, false, nowMs));
      tools = [];
      add(reasoningDurationMs(child, nowMs));
    }
  }
  add(toolBatchDurationMs(tools, active, nowMs));
  return total;
}

function toolBatchDurationMs(tools, active, nowMs) {
  let start = null;
  let end = null;
  let longestDuration = null;
  for (const tool of tools) {
    const duration = toolDurationMs(tool);
    if (duration !== null) {
      longestDuration = Math.max(longestDuration ?? 0, duration);
    }
    const from = timestampToMs(toolStartedTimestamp(tool));
    if (from === null) continue;
    start = start === null ? from : Math.min(start, from);
    if (duration !== null) {
      const to = from + duration;
      end = end === null ? to : Math.max(end, to);
    }
  }
  if (start === null) return longestDuration;
  if (active) end = Math.max(end ?? start, nowMs);
  if (end === null) return longestDuration;
  return Math.max(longestDuration ?? 0, end - start);
}
