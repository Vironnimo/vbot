import { t } from '$lib/i18n.js';
import { parseModelSelectionValue } from '$lib/modelSelection.js';

/**
 * Quick-tooltip content for an Agent's activity marker (header picker,
 * activity chips, Project Team tabs): the activity label as its title and
 * the effective Model as canonical `provider/model`.
 */
export function agentActivityTooltip(activityLabel, modelValue) {
  const { model } = parseModelSelectionValue(
    typeof modelValue === 'string' ? modelValue.trim() : '',
  );
  if (!model) {
    return activityLabel;
  }
  return {
    title: activityLabel,
    rows: [
      {
        label: t('chat.agentActivity.model', 'Model'),
        value: model,
        mono: true,
      },
    ],
  };
}
