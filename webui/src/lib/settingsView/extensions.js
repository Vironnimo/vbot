import { textOrEmpty, textOrFallback } from './values.js';

// --- Extensions ---------------------------------------------------------------

const EXTENSION_HOOK_EVENT_ORDER = [
  'run_start',
  'context',
  'tool_call',
  'tool_result',
  'run_end',
];

export function applyExtensionsPanelList(result) {
  const extensions = Array.isArray(result?.extensions) ? result.extensions : [];

  return extensions
    .filter(
      (extension) =>
        extension &&
        typeof extension === 'object' &&
        typeof extension.name === 'string' &&
        extension.name.length > 0,
    )
    .map((extension) => ({
      name: extension.name,
      status: textOrFallback(extension.status, 'loaded'),
      disabled: extension.disabled === true,
      version: textOrEmpty(extension.version),
      description: textOrEmpty(extension.description),
      error: textOrEmpty(extension.error),
      overriddenBy: textOrEmpty(extension.overridden_by),
      capabilityErrors: Array.isArray(extension.capability_errors)
        ? extension.capability_errors.filter(
            (entry) => typeof entry === 'string' && entry.length > 0,
          )
        : [],
      config:
        extension.config && typeof extension.config === 'object'
          ? extension.config
          : {},
      settingsSchema: normalizeSchemaFields(extension.settings_schema),
      readyState: extension.ready_state === 'waiting' ? 'waiting' : 'ready',
      capabilities: normalizeExtensionCapabilities(extension.capabilities),
    }));
}

// Each capability tool is a ``{ name, ready }`` object; ``ready`` defaults to
// true when absent so an older-shaped payload never reads as "waiting".
function normalizeCapabilityTools(tools) {
  if (!Array.isArray(tools)) {
    return [];
  }
  return tools
    .filter(
      (tool) =>
        tool &&
        typeof tool === 'object' &&
        typeof tool.name === 'string' &&
        tool.name.length > 0,
    )
    .map((tool) => ({ name: tool.name, ready: tool.ready !== false }));
}

function normalizeCapabilityCommands(commands) {
  if (!Array.isArray(commands)) {
    return [];
  }
  return commands
    .filter(
      (command) =>
        command &&
        typeof command === 'object' &&
        typeof command.name === 'string' &&
        command.name.length > 0,
    )
    .map((command) => ({
      name: command.name,
      registered: command.registered === true,
    }));
}

function normalizeExtensionCapabilities(capabilities) {
  const source =
    capabilities && typeof capabilities === 'object' ? capabilities : {};
  const hooks =
    source.hooks && typeof source.hooks === 'object' ? source.hooks : {};

  return {
    hooks: EXTENSION_HOOK_EVENT_ORDER.filter(
      (event) => Number(hooks[event]) > 0,
    ).map((event) => ({ event, count: Number(hooks[event]) })),
    tools: normalizeCapabilityTools(source.tools),
    commands: normalizeCapabilityCommands(source.commands),
    recallBackends: Array.isArray(source.recall_backends)
      ? source.recall_backends.filter(
          (backend) => typeof backend === 'string' && backend.length > 0,
        )
      : [],
    startup: source.startup === true,
    shutdown: source.shutdown === true,
  };
}

export function extensionStatusChipVariant(status) {
  if (status === 'loaded') {
    return 'success';
  }
  if (status === 'failed') {
    return 'error';
  }
  // ``disabled`` and ``overridden`` share the same muted/neutral variant: both
  // are inert records the user cannot act on directly.
  return 'warn';
}

export function summarizeExtensionCapabilities(capabilities, translate) {
  const normalized =
    capabilities && Array.isArray(capabilities.hooks)
      ? capabilities
      : normalizeExtensionCapabilities(capabilities);
  const parts = [];

  if (normalized.hooks.length > 0) {
    const hookSummary = normalized.hooks
      .map((hook) => `${hook.event}(${hook.count})`)
      .join(', ');
    parts.push(
      `${translate('settings.extensions.hooks', 'Hooks')}: ${hookSummary}`,
    );
  }
  if (normalized.tools.length > 0) {
    const toolNames = normalized.tools
      .map((tool) => (typeof tool === 'string' ? tool : tool.name))
      .join(', ');
    parts.push(
      `${translate('settings.extensions.tools', 'Tools')}: ${toolNames}`,
    );
  }
  if (normalized.commands.length > 0) {
    parts.push(
      `${translate('settings.extensions.commands', 'Commands')}: ${normalized.commands.map((command) => `/${command.name}`).join(', ')}`,
    );
  }
  if (normalized.recallBackends.length > 0) {
    parts.push(
      `${translate('settings.extensions.recallBackends', 'Recall backends')}: ${normalized.recallBackends.join(', ')}`,
    );
  }
  if (normalized.startup) {
    parts.push(translate('settings.extensions.startup', 'startup'));
  }
  if (normalized.shutdown) {
    parts.push(translate('settings.extensions.shutdown', 'shutdown'));
  }

  return parts.join(' · ');
}

/**
 * Describe an extension's derived waiting state for the Extensions panel (the
 * one place the waiting state is shown). Returns ``null`` when the extension is
 * ready. When waiting, returns the status hint and, if the schema (Phase 2)
 * declares unset secret fields, a line naming them by label:
 *   { hint, waitingFor }  // waitingFor is null when no unset secret is known
 */
export function describeExtensionWaiting(extension, translate) {
  if (!extension || extension.readyState !== 'waiting') {
    return null;
  }
  const hint = translate(
    'settings.extensions.waiting',
    'On, waiting for configuration',
  );
  const unsetSecretLabels = Array.isArray(extension.settingsSchema)
    ? extension.settingsSchema
        .filter((field) => field.type === 'secret' && field.set === false)
        .map((field) => field.label)
    : [];
  if (unsetSecretLabels.length === 0) {
    return { hint, waitingFor: null };
  }
  return {
    hint,
    waitingFor: translate(
      'settings.extensions.waitingFor',
      'Waiting for: {fields}',
      { fields: unsetSecretLabels.join(', ') },
    ),
  };
}

export function buildExtensionsUpdatePayload(extensions, override = {}) {
  const items = Array.isArray(extensions) ? extensions : [];
  const disabled = [];
  const config = {};

  for (const extension of items) {
    const name = textOrEmpty(extension?.name);
    if (!name) {
      continue;
    }

    const isOverride = name === override.name;
    const extensionDisabled =
      isOverride && typeof override.disabled === 'boolean'
        ? override.disabled
        : extension.disabled === true;
    if (extensionDisabled) {
      disabled.push(name);
    }

    const extensionConfig =
      isOverride && override.config && typeof override.config === 'object'
        ? override.config
        : extension.config && typeof extension.config === 'object'
          ? extension.config
          : {};
    if (Object.keys(extensionConfig).length > 0) {
      config[name] = extensionConfig;
    }
  }

  return { extensions: { disabled, config } };
}

// --- Extension settings schema (form helpers) ---------------------------------

const SCHEMA_FIELD_TYPES = ['text', 'number', 'toggle', 'secret'];

/**
 * Normalize a raw ``settings_schema`` list into display-ready field descriptors,
 * dropping malformed entries. Secret fields keep ``envKey``/``set``; others keep
 * ``default``.
 */
export function normalizeSchemaFields(schema) {
  if (!Array.isArray(schema)) {
    return [];
  }
  return schema
    .filter(
      (field) =>
        field &&
        typeof field === 'object' &&
        typeof field.key === 'string' &&
        field.key.length > 0 &&
        SCHEMA_FIELD_TYPES.includes(field.type),
    )
    .map((field) => ({
      key: field.key,
      type: field.type,
      label: textOrFallback(field.label, field.key),
      description: textOrEmpty(field.description),
      required: field.required === true,
      default: field.default === undefined ? null : field.default,
      envKey: field.type === 'secret' ? textOrEmpty(field.env_key) : '',
      set: field.type === 'secret' ? field.set === true : false,
    }));
}

/**
 * Build the editable form state (per non-secret field) from a schema and the
 * persisted config. Text/number inputs are strings; toggles are booleans.
 * Secrets are write-only and never seeded here.
 */
export function buildSchemaFormState(schema, config) {
  const fields = normalizeSchemaFields(schema);
  const source = config && typeof config === 'object' ? config : {};
  const state = {};
  for (const field of fields) {
    if (field.type === 'secret') {
      continue;
    }
    if (field.type === 'toggle') {
      const value = source[field.key];
      state[field.key] =
        typeof value === 'boolean' ? value : field.default === true;
      continue;
    }
    // text / number: keep as a string input value; empty when absent.
    const value = source[field.key];
    state[field.key] =
      value === undefined || value === null ? '' : String(value);
  }
  return state;
}

/**
 * Build the config object for the ``settings.update`` payload from the form
 * state. Toggles are always explicit; text/number fields are omitted when the
 * input is empty (so the default applies at read time). Numbers are parsed:
 * integer text yields an int, otherwise a float; unparseable input produces a
 * per-field error and ``ok: false``.
 *
 * @returns {{ ok: boolean, config: object, errors: Record<string, string> }}
 */
export function buildSchemaConfigFromForm(schema, formState) {
  const fields = normalizeSchemaFields(schema);
  const source = formState && typeof formState === 'object' ? formState : {};
  const config = {};
  const errors = {};

  for (const field of fields) {
    if (field.type === 'secret') {
      continue;
    }
    if (field.type === 'toggle') {
      config[field.key] = source[field.key] === true;
      continue;
    }
    const raw = source[field.key];
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (text.length === 0) {
      continue;
    }
    if (field.type === 'number') {
      const parsed = parseSchemaNumber(text);
      if (parsed === null) {
        errors[field.key] = 'invalid-number';
        continue;
      }
      config[field.key] = parsed;
      continue;
    }
    config[field.key] = text;
  }

  return { ok: Object.keys(errors).length === 0, config, errors };
}

function parseSchemaNumber(text) {
  // Integer text (no dot) parses to an int; anything else to a float. In JS
  // both are ``number``; JSON then serializes ``8123`` vs ``80.5`` faithfully.
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(text)) {
    return null;
  }
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** Whether an extension list entry declares an editable settings surface. */
export function hasSettingsSchema(extension) {
  return (
    extension &&
    Array.isArray(extension.settingsSchema) &&
    extension.settingsSchema.length > 0
  );
}
