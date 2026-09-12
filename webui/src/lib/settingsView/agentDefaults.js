import { textOrEmpty, positiveIntegerOrDefault } from './values.js';
import { normalizeCompactionPolicy } from '../compactionPolicy.js';

const SUBAGENT_SETTINGS_DEFAULTS = Object.freeze({
  max_subagent_depth: 4,
  max_subagents_per_turn: 8,
  subagent_timeout_minutes: 60,
});

export const AGENT_DEFAULTS_FIELDS = Object.freeze([
  'model',
  'fallback_models',
  'temperature',
  'thinking_effort',
]);

export const AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT =
  '__thinking_effort_no_default__';

const AGENT_DEFAULT_THINKING_EFFORT_OPTIONS = Object.freeze([
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

export function normalizeAgentDefaultsSettings(rawSettings) {
  const agentDefaults = resolveAgentDefaultsSource(rawSettings);

  return {
    model: textOrEmpty(agentDefaults.model),
    fallback_models: normalizeAgentDefaultsStringList(
      agentDefaults.fallback_models,
    ),
    temperature: normalizeAgentDefaultsTemperature(agentDefaults.temperature),
    thinking_effort: normalizeAgentDefaultsThinkingEffort(
      agentDefaults.thinking_effort,
    ),
  };
}

export function buildAgentDefaultsPayload(formValues) {
  const values = formValues && typeof formValues === 'object' ? formValues : {};

  return {
    defaults: {
      agent: {
        model: normalizeAgentDefaultsTextForPayload(values.model),
        fallback_models: normalizeAgentDefaultsStringListForPayload(
          values.fallback_models,
        ),
        temperature: normalizeAgentDefaultsTemperature(values.temperature),
        thinking_effort: normalizeAgentDefaultsThinkingEffortForPayload(
          values.thinking_effort,
        ),
      },
    },
  };
}

export function normalizeSessionTitleSettings(rawSettings) {
  const source =
    rawSettings?.session_titles &&
    typeof rawSettings.session_titles === 'object'
      ? rawSettings.session_titles
      : rawSettings && typeof rawSettings === 'object'
        ? rawSettings
        : {};

  return {
    enabled: source.enabled === true,
    model: textOrEmpty(source.model),
  };
}

export function buildSessionTitleSettingsPayload(formValues) {
  const normalized = normalizeSessionTitleSettings(formValues);
  return {
    session_titles: {
      enabled: normalized.enabled,
      model: normalized.model,
    },
  };
}

export function normalizeSubAgentSettings(rawSettings) {
  const subagents = rawSettings?.subagents ?? {};

  return {
    max_subagent_depth: positiveIntegerOrDefault(
      subagents.max_subagent_depth,
      SUBAGENT_SETTINGS_DEFAULTS.max_subagent_depth,
    ),
    max_subagents_per_turn: positiveIntegerOrDefault(
      subagents.max_subagents_per_turn,
      SUBAGENT_SETTINGS_DEFAULTS.max_subagents_per_turn,
    ),
    subagent_timeout_minutes: positiveIntegerOrDefault(
      subagents.subagent_timeout_minutes,
      SUBAGENT_SETTINGS_DEFAULTS.subagent_timeout_minutes,
    ),
  };
}

export function normalizeCompactionSettings(rawSettings) {
  return normalizeCompactionPolicy(rawSettings?.compaction);
}

export function buildCompactionSettingsPayload(formValues) {
  return {
    compaction: normalizeCompactionSettings({
      compaction: formValues,
    }),
  };
}

export function getCompactionSettings(settings) {
  return normalizeCompactionSettings(settings);
}

export function buildSubAgentSettingsPayload(formValues) {
  return {
    subagents: normalizeSubAgentSettings({
      subagents: formValues,
    }),
  };
}

function resolveAgentDefaultsSource(rawSettings) {
  const defaults = rawSettings?.defaults;

  if (defaults && typeof defaults === 'object' && !Array.isArray(defaults)) {
    const agentDefaults = defaults.agent;

    if (
      agentDefaults &&
      typeof agentDefaults === 'object' &&
      !Array.isArray(agentDefaults)
    ) {
      return agentDefaults;
    }

    return {};
  }

  if (
    rawSettings &&
    typeof rawSettings === 'object' &&
    !Array.isArray(rawSettings) &&
    AGENT_DEFAULTS_FIELDS.some((field) =>
      Object.prototype.hasOwnProperty.call(rawSettings, field),
    )
  ) {
    return rawSettings;
  }

  return {};
}

function normalizeAgentDefaultsTemperature(value) {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return null;
  }

  // Tolerate a comma decimal separator typed in comma-decimal locales.
  const numberValue = Number(normalized.replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

function normalizeAgentDefaultsTextForPayload(value) {
  const normalized = textOrEmpty(value);
  return normalized.length > 0 ? normalized : null;
}

// The fallback chain is an ordered string list: trim entries, drop empties.
function normalizeAgentDefaultsStringList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) =>
      item === null || item === undefined ? '' : String(item).trim(),
    )
    .filter((item) => item.length > 0);
}

function normalizeAgentDefaultsStringListForPayload(value) {
  const normalized = normalizeAgentDefaultsStringList(value);
  return normalized.length > 0 ? normalized : null;
}

function normalizeAgentDefaultsThinkingEffortForPayload(value) {
  if (value === AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }

  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }

  return AGENT_DEFAULT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}

function normalizeAgentDefaultsThinkingEffort(value) {
  if (value === AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }

  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }

  return AGENT_DEFAULT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}
