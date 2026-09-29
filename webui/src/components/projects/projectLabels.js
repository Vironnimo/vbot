import { t } from '$lib/i18n.js';
import { needsRePoint } from '$lib/projectsView.js';
import { formatMoment } from '$lib/timeText.js';

const FORMAT_LABELS = Object.freeze({
  opencode: () => t('projects.format.opencode'),
  claude: () => t('projects.format.claude'),
});

export function formatLabel(formatKey) {
  return FORMAT_LABELS[formatKey] ? FORMAT_LABELS[formatKey]() : formatKey;
}

/**
 * Details card of a Project list row: why it needs re-pointing (lead), the
 * complete repository path, source format, id (when a name leads) and when
 * it was added. Selectable, so the path can be copied.
 */
export function projectRowDetails(project, { nowMs = Date.now() } = {}) {
  const name = project?.display_name || '';
  return {
    title: name || project?.project_id || '',
    text: needsRePoint(project) ? t('projects.rePoint.description') : '',
    rows: [
      {
        label: t('projects.details.repository'),
        value: project?.cwd || '',
        mono: true,
      },
      {
        label: t('projects.manage.sourceFormat'),
        value: project?.source_format ? formatLabel(project.source_format) : '',
      },
      {
        label: t('projects.details.id'),
        value: name ? project?.project_id || '' : '',
        mono: true,
      },
      {
        label: t('projects.details.added'),
        value: formatMoment(project?.created_at, { nowMs }),
      },
    ],
    placement: 'right',
    selectable: true,
  };
}
