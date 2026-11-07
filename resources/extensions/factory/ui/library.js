import { t } from '../../../../webui/src/lib/i18n.js';

export function templateKind(kind) {
  if (kind === 'step') return t('factory.library.kindStep');
  if (kind === 'sequence') return t('factory.library.kindSequence');
  return t('factory.library.kindWorkflow');
}

export const templateKinds = () => [
  { value: 'step', label: t('factory.library.kindStep') },
  { value: 'sequence', label: t('factory.library.kindSequence') },
  { value: 'workflow', label: t('factory.library.kindWorkflow') },
];

export function changeField(field) {
  const labels = {
    name: () => t('factory.stepName'),
    description: () => t('factory.workflowDescription'),
    task: () => t('factory.stepTask'),
    agent: () => t('factory.agent'),
    source: () => t('factory.library.agentSource'),
    model: () => t('factory.model'),
    effort: () => t('factory.effort'),
    after: () => t('factory.stepStarts'),
    approval: () => t('factory.stepApproval'),
    returns_to: () => t('factory.stepReturnsTo'),
    rounds: () => t('factory.stepRounds'),
  };
  return labels[field]?.() ?? t('factory.library.structure');
}

export function changeValue(value, workflow) {
  if (value === null || value === undefined) return t('factory.library.absent');
  if (typeof value === 'boolean')
    return value ? t('factory.library.yes') : t('factory.library.no');
  if (Array.isArray(value))
    return (
      value.map((item) => changeValue(item, workflow)).join(', ') ||
      t('factory.library.empty')
    );
  if (typeof value === 'object')
    return (
      Object.entries(value)
        .filter(([field]) => field !== 'id')
        .map(
          ([field, content]) =>
            `${changeField(field)}: ${changeValue(content, workflow)}`,
        )
        .join('\n') || t('factory.library.empty')
    );
  if (typeof value === 'string')
    return (
      (workflow?.steps?.find((step) => step.id === value)?.name ?? value) ||
      t('factory.library.empty')
    );
  return String(value);
}
