import { t } from '$lib/i18n.js';
import { parseModelSelectionValue } from '$lib/modelSelection.js';

/**
 * Quick-tooltip content for an Agent's activity marker (header picker and
 * its options, activity chips, Project Team tabs): the activity label as its
 * title, the effective Model as canonical `provider/model` and the effective
 * thinking effort, where no effort leaves the Provider default in place.
 */
export function agentActivityTooltip(
  activityLabel,
  { model: modelValue, thinkingEffort } = {},
) {
  const { model } = parseModelSelectionValue(
    typeof modelValue === 'string' ? modelValue.trim() : '',
  );
  if (!model) {
    return activityLabel;
  }
  const effort =
    typeof thinkingEffort === 'string' ? thinkingEffort.trim() : '';
  return {
    title: activityLabel,
    rows: [
      {
        label: t('chat.agentActivity.model', 'Model'),
        value: model,
        mono: true,
      },
      {
        label: t('chat.agentActivity.thinkingEffort', 'Thinking effort'),
        value:
          effort ||
          t('chat.agentActivity.thinkingEffortDefault', 'Provider default'),
        mono: Boolean(effort),
      },
    ],
  };
}
