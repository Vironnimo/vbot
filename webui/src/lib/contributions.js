// Presentation composition is independent of transport and Svelte. Both core
// snippets and Extension descriptors use these identifiers and ordering rules.
export const CONTRIBUTION_SURFACES = Object.freeze([
  'shell.status',
  'shell.sidebar',
  'chat.header',
  'chat.composer.actions',
  'chat.composer.context',
  'chat.composer.footer',
  'chat.composer.footer.start',
  'chat.composer.footer.end',
  'chat.timeline',
  'chat.tool-result',
  'chat.message',
  'chat.message.actions',
  'chat.sessions',
  'chat.session.item',
  'agent.editor.sections',
  'project.editor.sections',
  'settings.sections',
]);

export const UI_CONTEXT_FIELDS = Object.freeze([
  'agent_id',
  'session_id',
  'project_id',
  'run_id',
  'run_active',
  'has_draft',
  'has_session',
  'settings_section',
  'tool_name',
  'tool_status',
  'connected',
  'view',
  'computer_id',
  'tool_call_id',
  'context_tokens',
  'context_window',
  'session_cost',
  'output_speed',
  'message_id',
  'message_role',
  'session_selected',
  'editor_mode',
]);

export function contributionContext(value = {}) {
  return Object.fromEntries(
    UI_CONTEXT_FIELDS.filter((key) => {
      const item = value[key];
      return (
        item === null ||
        ['string', 'boolean'].includes(typeof item) ||
        (typeof item === 'number' && Number.isFinite(item))
      );
    }).map((key) => [key, value[key]]),
  );
}

export function contributionMatches(item, context = {}) {
  let nodes = 0;
  const scalar = (value) =>
    value === null ||
    ['string', 'boolean'].includes(typeof value) ||
    (typeof value === 'number' && Number.isFinite(value));
  function matches(condition, depth = 0) {
    if (
      ++nodes > 64 ||
      depth > 8 ||
      !condition ||
      typeof condition !== 'object' ||
      Array.isArray(condition)
    )
      return undefined;
    const keys = Object.keys(condition);
    for (const operator of ['all', 'any']) {
      if (!Object.hasOwn(condition, operator)) continue;
      if (
        keys.length !== 1 ||
        !Array.isArray(condition[operator]) ||
        !condition[operator].length
      )
        return undefined;
      // Evaluate every child so the structural limit is independent of context.
      const results = condition[operator].map((child) =>
        matches(child, depth + 1),
      );
      if (results.includes(undefined)) return undefined;
      return operator === 'all'
        ? results.every(Boolean)
        : results.some(Boolean);
    }
    if (Object.hasOwn(condition, 'not')) {
      if (keys.length !== 1) return undefined;
      const result = matches(condition.not, depth + 1);
      return result === undefined ? undefined : !result;
    }
    if (Object.hasOwn(condition, 'field')) {
      const { field, op, value } = condition;
      if (
        !UI_CONTEXT_FIELDS.includes(field) ||
        keys.length !== 3 ||
        !keys.includes('op') ||
        !keys.includes('value')
      )
        return undefined;
      const present = Object.hasOwn(context, field);
      if (op === 'exists')
        return typeof value === 'boolean' ? present === value : undefined;
      if (op === 'in')
        return Array.isArray(value) && value.every(scalar)
          ? present && value.includes(context[field])
          : undefined;
      if (
        !['eq', 'ne', 'gt', 'gte', 'lt', 'lte'].includes(op) ||
        !scalar(value)
      )
        return undefined;
      if (!['eq', 'ne'].includes(op) && typeof value !== 'number')
        return undefined;
      if (!present) return false;
      const actual = context[field];
      if (op === 'eq') return actual === value;
      if (op === 'ne') return actual !== value;
      if (
        typeof actual !== 'number' ||
        typeof value !== 'number' ||
        !Number.isFinite(actual)
      )
        return false;
      if (op === 'gt') return actual > value;
      if (op === 'gte') return actual >= value;
      if (op === 'lt') return actual < value;
      if (op === 'lte') return actual <= value;
      return false;
    }
    if (
      Object.entries(condition).some(
        ([key, value]) => !UI_CONTEXT_FIELDS.includes(key) || !scalar(value),
      )
    )
      return undefined;
    return Object.entries(condition).every(
      ([key, value]) => Object.hasOwn(context, key) && context[key] === value,
    );
  }
  return item.when == null || matches(item.when) === true;
}

export function composeContributions(
  builtins,
  extensions,
  surface,
  context = {},
  layout = {},
) {
  const all = [
    ...builtins,
    ...extensions.filter((item) => item.surface === surface),
  ];
  const seen = new Set();
  const candidates = all.filter((item) => {
    if (!item || typeof item.id !== 'string' || seen.has(item.id)) return false;
    seen.add(item.id);
    return contributionMatches(item, context);
  });
  const hidden = new Set(Array.isArray(layout.hidden) ? layout.hidden : []);
  const selected = new Set();
  const replaced = new Set();
  for (const [target, replacement] of Object.entries(
    layout.replacements ?? {},
  )) {
    const candidate = candidates.find(
      (item) => item.id === replacement && item.replaces === target,
    );
    if (candidate && !hidden.has(replacement)) {
      selected.add(replacement);
      replaced.add(target);
    }
  }
  const order = Array.isArray(layout.order) ? layout.order : [];
  return candidates
    .filter(
      (item) =>
        !hidden.has(item.id) &&
        !replaced.has(item.id) &&
        (!item.replaces || selected.has(item.id)),
    )
    .sort((a, b) => {
      const ai = order.indexOf(a.id),
        bi = order.indexOf(b.id);
      if (ai >= 0 || bi >= 0)
        return (ai < 0 ? Infinity : ai) - (bi < 0 ? Infinity : bi);
      return (
        (Number.isFinite(a.order) ? a.order : 100) -
          (Number.isFinite(b.order) ? b.order : 100) || a.id.localeCompare(b.id)
      );
    });
}

export function normalizeContributionItems(descriptor, response, context = {}) {
  const items = descriptor.source ? response?.items : [descriptor];
  if (!Array.isArray(items)) return [];
  const seen = new Set();
  return items
    .slice(0, 100)
    .filter((item) => {
      if (!item || typeof item !== 'object') return false;
      const id = typeof item.id === 'string' ? item.id : descriptor.id;
      if (seen.has(id)) return false;
      seen.add(id);
      return typeof (item.label ?? descriptor.title) === 'string';
    })
    .map((item) => ({
      ...item,
      id: typeof item.id === 'string' ? item.id : descriptor.id,
      label: String(
        item.label ??
          (typeof item.text === 'string' ? item.text : descriptor.title) ??
          '',
      ).slice(0, 2000),
      text: contributionText(
        item.text ?? descriptor.text ?? item.label ?? descriptor.title,
        context,
      ),
      disabled: item.disabled === true,
      action: item.action ?? descriptor.action,
    }));
}

function contributionText(value, context) {
  if (
    value &&
    typeof value === 'object' &&
    UI_CONTEXT_FIELDS.includes(value.context)
  )
    return Object.hasOwn(context, value.context)
      ? String(context[value.context] ?? '')
      : '';
  return typeof value === 'string' ? value.slice(0, 2000) : '';
}

export function bindContributionParams(params = {}, context = {}) {
  return Object.fromEntries(
    Object.entries(params).map(([key, value]) => {
      if (
        value &&
        typeof value === 'object' &&
        !Array.isArray(value) &&
        Object.keys(value).length === 1 &&
        typeof value.context === 'string'
      ) {
        if (
          !UI_CONTEXT_FIELDS.includes(value.context) ||
          !Object.hasOwn(context, value.context)
        )
          throw new Error(`UI context unavailable: ${value.context}`);
        return [key, context[value.context]];
      }
      return [key, value];
    }),
  );
}

export function contributionData(data) {
  try {
    const encoded = JSON.stringify(data ?? {});
    return encoded.length <= 256 * 1024
      ? JSON.parse(encoded)
      : { truncated: true };
  } catch {
    return { unavailable: true };
  }
}
