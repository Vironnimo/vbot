import { t } from '$lib/i18n.js';

const FORMAT_LABELS = Object.freeze({
  opencode: () => t('projects.format.opencode', 'OpenCode'),
  claude: () => t('projects.format.claude', 'Claude Code'),
});

export function formatLabel(formatKey) {
  return FORMAT_LABELS[formatKey] ? FORMAT_LABELS[formatKey]() : formatKey;
}
