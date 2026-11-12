// A choice that falls back to a default while it has no own value lists that
// default as the option it resolves to, marked "(default)", instead of a
// separate "Default …" entry that says nothing about what applies. Choosing
// the marked option clears the own value again, so the choice keeps following
// the default (user requirement 2026-11-12, `webui/shared-controls.md` ->
// Default choices).
import { t } from './i18n.js';

/** `label` marked as the default: "High (default)". */
export function defaultOptionLabel(label) {
  return t('common.defaultOption', { label });
}

/**
 * Marks the option whose value is `defaultValue`: it takes `unsetValue` and
 * the "(default)" label. When no enabled option has that value (the default
 * is unknown, null or not offered), a separate unset option labelled
 * `fallbackLabel` leads the list instead; without a `fallbackLabel` the list
 * gets no unset option. `mark` turns an option label into the marked one.
 */
export function markDefaultOption(
  options,
  {
    defaultValue = null,
    unsetValue = '',
    fallbackLabel = '',
    mark = defaultOptionLabel,
  } = {},
) {
  const index = isSet(defaultValue)
    ? options.findIndex(
        (option) => option.value === defaultValue && !option.disabled,
      )
    : -1;
  if (index === -1) {
    return fallbackLabel
      ? [{ value: unsetValue, label: fallbackLabel }, ...options]
      : options;
  }
  return options.map((option, optionIndex) =>
    optionIndex === index
      ? { ...option, value: unsetValue, label: mark(option.label) }
      : option,
  );
}

/**
 * The value a control built by `markDefaultOption` shows: no own value and an
 * own value equal to the default both select the marked option.
 */
export function defaultChoiceValue(
  value,
  { defaultValue = null, unsetValue = '' } = {},
) {
  return !isSet(value) ||
    value === unsetValue ||
    (isSet(defaultValue) && value === defaultValue)
    ? unsetValue
    : value;
}

function isSet(value) {
  return value !== null && value !== undefined;
}
