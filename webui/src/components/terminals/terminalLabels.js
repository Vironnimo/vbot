import { t } from '$lib/i18n.js';
import { formatTerminalCommandLine } from '$lib/terminalsView.js';

export function launchHistoryLabel(entry) {
  return (
    formatTerminalCommandLine(entry?.command, entry?.args) ||
    t('terminals.commandPlaceholder', 'Default shell')
  );
}

export function launchHistoryWorkdir(entry) {
  return (
    String(entry?.workdir || '').trim() ||
    t('terminals.workdirPlaceholder', 'User home directory')
  );
}

export function groupKindLabel(kind) {
  if (kind === 'user') {
    return t('terminals.kind.user', 'My group');
  }
  if (kind === 'agent') {
    return t('terminals.kind.agent', 'Agent');
  }
  if (kind === 'finished') {
    return t('terminals.kind.finished', 'Finished');
  }
  return t('terminals.kind.manual', 'Manual');
}

export function groupCanEdit(group) {
  return group?.kind === 'user' || group?.kind === 'agent';
}

export function terminalTarget(item) {
  if (!item?.owner) {
    return t('terminals.manualOwner', 'Manual');
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
  return message || t('terminals.unknownError', 'Unknown terminal error');
}
