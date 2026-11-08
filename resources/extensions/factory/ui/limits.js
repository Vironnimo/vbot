import { t } from '../../../../webui/src/lib/i18n.js';

export const limitFields = [
  { key: 'tool_calls', label: () => t('factory.limit.tools') },
  { key: 'tokens', label: () => t('factory.limit.tokens') },
  { key: 'cost_usd', label: () => t('factory.limit.cost') },
  { key: 'runs', label: () => t('factory.limit.runs') },
];

const number = (value) => Number(String(value ?? '').replace(',', '.'));
export const normalizeLimits = (value = {}) =>
  Object.fromEntries(
    limitFields
      .map(({ key }) => [key, number(value[key])])
      .filter(([, amount]) => amount !== 0),
  );
export const limitsProblem = (value = {}) =>
  limitFields.some(({ key }) => {
    const amount = number(value[key]);
    return (
      !Number.isFinite(amount) ||
      amount < 0 ||
      (key !== 'cost_usd' && !Number.isSafeInteger(amount))
    );
  })
    ? t('factory.invalid.limits')
    : '';
