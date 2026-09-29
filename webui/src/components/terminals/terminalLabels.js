import { t } from '$lib/i18n.js';
import { formatTerminalCommandLine } from '$lib/terminalsView.js';
import { formatMoment } from '$lib/timeText.js';

export function launchHistoryLabel(entry) {
  return (
    formatTerminalCommandLine(entry?.command, entry?.args) ||
    t('terminals.commandPlaceholder')
  );
}

export function launchHistoryWorkdir(entry) {
  return (
    String(entry?.workdir || '').trim() || t('terminals.workdirPlaceholder')
  );
}

export function groupKindLabel(kind) {
  if (kind === 'user') {
    return t('terminals.kind.user');
  }
  if (kind === 'agent') {
    return t('terminals.kind.agent');
  }
  if (kind === 'finished') {
    return t('terminals.kind.finished');
  }
  return t('terminals.kind.manual');
}

export function groupCanEdit(group) {
  return group?.kind === 'user' || group?.kind === 'agent';
}

export function terminalTarget(item) {
  if (!item?.owner) {
    return t('terminals.manualOwner');
  }
  const agentId = item?.owner?.agent_id || '—';
  const projectId = item?.owner?.project_id;
  return projectId ? `${agentId}@${projectId}` : agentId;
}

export function terminalTitle(item) {
  const customName = typeof item?.name === 'string' ? item.name.trim() : '';
  if (customName) {
    return customName;
  }
  const announcedTitle =
    typeof item?.title === 'string' ? item.title.trim() : '';
  if (announcedTitle) {
    return announcedTitle;
  }
  const launched = launchedCommand(item);
  if (launched) {
    return launched;
  }
  const command = String(item?.command || '').trim();
  const executable = command.split(/[\\/]/).pop()?.toLowerCase() || '';
  const labels = {
    'pwsh.exe': 'PowerShell',
    pwsh: 'PowerShell',
    'powershell.exe': 'Windows PowerShell',
    powershell: 'Windows PowerShell',
    'cmd.exe': 'Command Prompt',
    cmd: 'Command Prompt',
    'bash.exe': 'Bash',
    bash: 'Bash',
    zsh: 'Zsh',
    fish: 'Fish',
  };
  return labels[executable] || command || 'Terminal';
}

export function launchedCommand(item) {
  const launchCommand = String(item?.launch_command || '').trim();
  if (!launchCommand) {
    return '';
  }
  const args = Array.isArray(item?.launch_args) ? item.launch_args : [];
  return [launchCommand, ...args].join(' ');
}

export function terminalError(message) {
  return message || t('terminals.unknownError');
}

const FINISHED_STATES = new Set(['exited', 'error']);

function stateText(item) {
  switch (item?.state) {
    case 'starting':
      return t('terminals.state.starting');
    case 'ready':
      return t('terminals.state.ready');
    case 'working':
      return t('terminals.state.working');
    case 'exited':
      return Number.isInteger(item.exit_code)
        ? t('terminals.state.exitedWithCode', { code: item.exit_code })
        : t('terminals.state.exited');
    case 'error':
      return t('terminals.state.error');
    default:
      return '';
  }
}

function stateTone(item) {
  if (item?.state === 'error') return 'danger';
  if (item?.state === 'exited') {
    return item.exit_code === 0 || item.exit_code == null ? 'muted' : 'danger';
  }
  if (item?.state === 'starting') return 'muted';
  return 'success';
}

/**
 * Details card of a Terminal's title: what the tile bar omits. The rows
 * give the full command line, working directory, state (with the exit code
 * once finished), start and finish moments, process id while running, and
 * the grid size. Selectable, so the command and directory can be copied.
 */
export function terminalDetails(item, { nowMs = Date.now() } = {}) {
  const finished = FINISHED_STATES.has(item?.state);
  const command =
    launchedCommand(item) ||
    formatTerminalCommandLine(
      item?.command,
      Array.isArray(item?.arguments) ? item.arguments : [],
    );
  const size =
    Number.isInteger(item?.columns) && Number.isInteger(item?.rows)
      ? t('terminals.details.sizeValue', {
          columns: item.columns,
          rows: item.rows,
        })
      : '';
  return {
    title: terminalTitle(item),
    rows: [
      {
        label: t('terminals.details.command'),
        value: command || t('terminals.commandPlaceholder'),
        mono: Boolean(command),
      },
      {
        label: t('terminals.details.directory'),
        value: String(item?.workdir || '').trim(),
        mono: true,
      },
      {
        label: t('terminals.details.state'),
        value: stateText(item),
        tone: stateTone(item),
      },
      {
        label: t('terminals.details.started'),
        value: formatMoment(item?.started_at, { nowMs }),
      },
      {
        label: t('terminals.details.finished'),
        value: finished ? formatMoment(item?.finished_at, { nowMs }) : '',
      },
      {
        label: t('terminals.details.pid'),
        value: !finished && item?.pid != null ? String(item.pid) : '',
        mono: true,
      },
      { label: t('terminals.details.size'), value: size },
    ],
    placement: 'bottom',
    selectable: true,
  };
}

function groupHint(kind) {
  switch (kind) {
    case 'user':
      return t('terminals.groupHint.user');
    case 'agent':
      return t('terminals.groupHint.agent');
    case 'finished':
      return t('terminals.groupHint.finished');
    default:
      return t('terminals.groupHint.automatic');
  }
}

/**
 * Details card of a group tab: what the group is and how its terminals
 * split into running and finished ones. `terminals` is the loaded list.
 */
export function terminalGroupDetails(group, terminals = []) {
  const members = terminals.filter(
    (terminal) => terminal?.group_id === group?.group_id,
  );
  const finished = members.filter((terminal) =>
    FINISHED_STATES.has(terminal?.state),
  ).length;
  const count = Math.max(Number(group?.terminal_count) || 0, members.length);
  return {
    title: group?.name ?? '',
    text: groupHint(group?.kind),
    rows: [
      {
        label: t('terminals.details.terminals'),
        value: t('terminals.details.terminalCount', {
          count,
          running: count - finished,
          finished,
        }),
      },
    ],
    placement: 'bottom',
  };
}

/** Beside a recent setup: its full command line, directory and last use. */
export function launchHistoryDetails(entry, { nowMs = Date.now() } = {}) {
  const command = formatTerminalCommandLine(entry?.command, entry?.args);
  return {
    rows: [
      {
        label: t('terminals.details.command'),
        value: command || t('terminals.commandPlaceholder'),
        mono: Boolean(command),
      },
      {
        label: t('terminals.details.directory'),
        value: launchHistoryWorkdir(entry),
        mono: Boolean(String(entry?.workdir || '').trim()),
      },
      {
        label: t('terminals.details.lastUsed'),
        value: formatMoment(entry?.used_at, { nowMs }),
      },
    ],
  };
}
