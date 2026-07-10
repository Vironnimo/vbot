export const DEFAULT_COMPACTION_POLICY = Object.freeze({
  enabled: true,
  trigger: Object.freeze({ type: 'context_ratio', threshold: 0.8 }),
  strategy: Object.freeze({
    type: 'summary_tail',
    tail_tokens: 15000,
    summary_model: null,
  }),
});

export const COMPACTION_TRIGGER_TYPES = Object.freeze([
  'context_ratio',
  'input_tokens',
]);
export const COMPACTION_STRATEGY_TYPES = Object.freeze([
  'summary_tail',
  'continuation',
]);

export function normalizeCompactionPolicy(value) {
  const policy = isObject(value) ? value : DEFAULT_COMPACTION_POLICY;
  const trigger = isObject(policy.trigger) ? policy.trigger : {};
  const strategy = isObject(policy.strategy) ? policy.strategy : {};
  const triggerType = COMPACTION_TRIGGER_TYPES.includes(trigger.type)
    ? trigger.type
    : 'context_ratio';
  const strategyType = COMPACTION_STRATEGY_TYPES.includes(strategy.type)
    ? strategy.type
    : 'summary_tail';
  return {
    enabled: policy.enabled !== false,
    trigger:
      triggerType === 'input_tokens'
        ? { type: triggerType, tokens: positiveInteger(trigger.tokens, 100000) }
        : {
            type: triggerType,
            threshold: ratio(trigger.threshold, 0.8),
          },
    strategy:
      strategyType === 'continuation'
        ? { type: strategyType }
        : {
            type: strategyType,
            tail_tokens: positiveInteger(strategy.tail_tokens, 15000),
            summary_model: textOrNull(strategy.summary_model),
          },
  };
}

export function compactionPoliciesEqual(left, right) {
  return (
    JSON.stringify(normalizeCompactionPolicy(left)) ===
    JSON.stringify(normalizeCompactionPolicy(right))
  );
}

export function buildCompactionPolicyPayload(value) {
  return normalizeCompactionPolicy(value);
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function ratio(value, fallback) {
  const parsed = Number(String(value ?? '').replace(',', '.'));
  return Number.isFinite(parsed) && parsed > 0 && parsed <= 1
    ? parsed
    : fallback;
}

function textOrNull(value) {
  const text =
    value === null || value === undefined ? '' : String(value).trim();
  return text || null;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
