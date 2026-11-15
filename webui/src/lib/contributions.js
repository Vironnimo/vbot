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
]);

export function contributionContext(value = {}) {
  return Object.fromEntries(
    UI_CONTEXT_FIELDS.filter((key) => {
      const item = value[key];
      return (
        item === null || ['string', 'number', 'boolean'].includes(typeof item)
      );
    }).map((key) => [key, value[key]]),
  );
}

export function contributionMatches(item, context = {}) {
  return (
    !item.when ||
    Object.entries(item.when).every(
      ([key, value]) =>
        UI_CONTEXT_FIELDS.includes(key) && context[key] === value,
    )
  );
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
