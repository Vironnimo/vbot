export const TOOL_ACCESS_MODE_ALL = 'all';
export const TOOL_ACCESS_MODE_SELECTED = 'selected';
export const TOOL_ACCESS_MODE_NONE = 'none';

export const TOOL_ACCESS_MODES = Object.freeze([
  TOOL_ACCESS_MODE_ALL,
  TOOL_ACCESS_MODE_SELECTED,
  TOOL_ACCESS_MODE_NONE,
]);

export const TOOL_ACTIVATION_CONFIGURABLE = 'configurable';
export const TOOL_ACTIVATION_FOLLOWS = 'follows';

const EMPTY_POLICY = Object.freeze({ mode: TOOL_ACCESS_MODE_ALL });

export function normalizeToolAccess(value) {
  if (!isPlainObject(value) || !TOOL_ACCESS_MODES.includes(value.mode)) {
    return { ...EMPTY_POLICY };
  }

  const denied = normalizeNames(value.denied);
  if (value.mode === TOOL_ACCESS_MODE_SELECTED) {
    const deniedSet = new Set(denied);
    const allowed = normalizeNames(value.allowed).filter(
      (name) => !deniedSet.has(name),
    );
    return compactPolicy({ mode: value.mode, allowed, denied });
  }
  return compactPolicy({ mode: value.mode, denied });
}

export function changeToolAccessMode(
  value,
  mode,
  catalog = [],
  ceiling = null,
) {
  const current = normalizeToolAccess(value);
  if (!TOOL_ACCESS_MODES.includes(mode) || mode === current.mode) {
    return current;
  }
  if (mode === TOOL_ACCESS_MODE_SELECTED) {
    const denied = [...(current.denied ?? [])];
    const deniedSet = new Set(denied);
    const allowed =
      current.mode === TOOL_ACCESS_MODE_ALL
        ? configurableNames(catalog, ceiling).filter(
            (name) => !deniedSet.has(name),
          )
        : [];
    return compactPolicy({ mode, allowed, denied });
  }
  return compactPolicy({ mode, denied: [...(current.denied ?? [])] });
}

export function setToolAccessState(
  value,
  name,
  state,
  catalog = [],
  ceiling = null,
) {
  const current = normalizeToolAccess(value);
  const tool = toolByName(catalog, name);
  const names = ceilingNames(ceiling);
  if (!name || (names && !names.has(name))) {
    return current;
  }

  const denied = new Set(current.denied ?? []);
  const allowed = new Set(current.allowed ?? []);
  if (state === 'denied') {
    denied.add(name);
    allowed.delete(name);
    return compactPolicy({
      ...current,
      allowed: [...allowed],
      denied: [...denied],
    });
  }

  denied.delete(name);
  if (state === 'default') {
    allowed.delete(name);
    return compactPolicy({
      ...current,
      allowed: [...allowed],
      denied: [...denied],
    });
  }

  if (state !== 'enabled' || !toolIsConfigurable(tool)) {
    return compactPolicy({
      ...current,
      allowed: [...allowed],
      denied: [...denied],
    });
  }
  if (current.mode === TOOL_ACCESS_MODE_ALL) {
    return compactPolicy({ ...current, denied: [...denied] });
  }
  allowed.add(name);
  return compactPolicy({
    mode: TOOL_ACCESS_MODE_SELECTED,
    allowed: [...allowed],
    denied: [...denied],
  });
}

export function setToolFamilyState(
  value,
  members,
  state,
  catalog = [],
  ceiling = null,
) {
  let next = normalizeToolAccess(value);
  for (const member of members) {
    next = setToolAccessState(next, member.name, state, catalog, ceiling);
  }
  return next;
}

// The compact editor exposes one permission switch per Tool. The persisted
// representation still differs by mode: an off configurable Tool is an
// exclusion in `all`, but simply absent from `selected`; automatic Tools use an
// absolute denial because they have no direct selection entry of their own.
export function toolAccessPreferenceEnabled(value, tool) {
  const policy = normalizeToolAccess(value);
  if (
    policy.mode === TOOL_ACCESS_MODE_NONE ||
    (policy.denied ?? []).includes(tool?.name)
  ) {
    return false;
  }
  if (!toolIsConfigurable(tool)) {
    return true;
  }
  return (
    policy.mode === TOOL_ACCESS_MODE_ALL ||
    (policy.mode === TOOL_ACCESS_MODE_SELECTED &&
      (policy.allowed ?? []).includes(tool?.name))
  );
}

export function setToolAccessPreference(
  value,
  tool,
  enabled,
  catalog = [],
  ceiling = null,
) {
  const policy = normalizeToolAccess(value);
  if (enabled) {
    return setToolAccessState(
      policy,
      tool?.name,
      toolIsConfigurable(tool) ? 'enabled' : 'default',
      catalog,
      ceiling,
    );
  }
  return setToolAccessState(
    policy,
    tool?.name,
    toolIsConfigurable(tool) && policy.mode === TOOL_ACCESS_MODE_SELECTED
      ? 'default'
      : 'denied',
    catalog,
    ceiling,
  );
}

export function setToolFamilyPreference(
  value,
  members,
  enabled,
  catalog = [],
  ceiling = null,
) {
  let next = normalizeToolAccess(value);
  for (const member of members) {
    next = setToolAccessPreference(next, member, enabled, catalog, ceiling);
  }
  return next;
}

export function toolAccessState(value, tool, catalog = [], context = {}) {
  const policy = normalizeToolAccess(value);
  if ((policy.denied ?? []).includes(tool.name)) {
    return 'denied';
  }
  if (toolIsConfigurable(tool)) {
    if (policy.mode === TOOL_ACCESS_MODE_ALL) {
      return 'included';
    }
    if (
      policy.mode === TOOL_ACCESS_MODE_SELECTED &&
      (policy.allowed ?? []).includes(tool.name)
    ) {
      return 'enabled';
    }
    return 'off';
  }
  return automaticToolIsActive(policy, tool, catalog, context)
    ? 'automatic'
    : 'inactive';
}

export function toolAccessIncludes(value, name) {
  const policy = normalizeToolAccess(value);
  if (
    (policy.denied ?? []).includes(name) ||
    policy.mode === TOOL_ACCESS_MODE_NONE
  ) {
    return false;
  }
  return (
    policy.mode === TOOL_ACCESS_MODE_ALL ||
    (policy.mode === TOOL_ACCESS_MODE_SELECTED &&
      (policy.allowed ?? []).includes(name))
  );
}

export function groupToolCatalog(catalog = [], ceiling = null) {
  const permitted = ceilingNames(ceiling);
  const visible = [];
  const known = new Set();
  for (const candidate of Array.isArray(catalog) ? catalog : []) {
    if (
      !isPlainObject(candidate) ||
      !candidate.name ||
      known.has(candidate.name)
    ) {
      continue;
    }
    if (permitted && !permitted.has(candidate.name)) {
      continue;
    }
    known.add(candidate.name);
    visible.push(candidate);
  }

  if (permitted) {
    for (const name of permitted) {
      if (!known.has(name)) {
        visible.push({
          name,
          description: '',
          family: null,
          activation: TOOL_ACTIVATION_CONFIGURABLE,
          ready: false,
          registered: false,
        });
      }
    }
  }

  const groups = new Map();
  const singles = [];
  for (const tool of visible) {
    if (!tool.family) {
      singles.push(tool);
      continue;
    }
    const members = groups.get(tool.family) ?? [];
    members.push(tool);
    groups.set(tool.family, members);
  }
  const result = [...groups.entries()].map(([id, members]) => ({
    id,
    family: true,
    members: sortTools(members),
  }));
  if (singles.length > 0) {
    result.push({ id: null, family: false, members: sortTools(singles) });
  }
  return result;
}

export function policyNamesNotInCatalog(value, catalog = []) {
  const policy = normalizeToolAccess(value);
  const known = new Set(
    (Array.isArray(catalog) ? catalog : []).map((tool) => tool?.name),
  );
  return [...(policy.allowed ?? []), ...(policy.denied ?? [])].filter(
    (name, index, all) => !known.has(name) && all.indexOf(name) === index,
  );
}

export function toolIsConfigurable(tool) {
  return (
    (tool?.activation ?? TOOL_ACTIVATION_CONFIGURABLE) ===
    TOOL_ACTIVATION_CONFIGURABLE
  );
}

function configurableNames(catalog, ceiling) {
  return groupToolCatalog(catalog, ceiling)
    .flatMap((group) => group.members)
    .filter(toolIsConfigurable)
    .map((tool) => tool.name);
}

function toolByName(catalog, name) {
  return (
    (Array.isArray(catalog) ? catalog : []).find(
      (tool) => tool?.name === name,
    ) ?? { name, activation: TOOL_ACTIVATION_CONFIGURABLE }
  );
}

function automaticToolIsActive(
  policy,
  tool,
  catalog,
  context,
  seen = new Set(),
) {
  if (policy.mode === TOOL_ACCESS_MODE_NONE || seen.has(tool?.name)) {
    return false;
  }
  if ((policy.denied ?? []).includes(tool?.name)) {
    return false;
  }
  if (tool?.activation === 'memory_mode') {
    return context?.memoryPromptMode !== 'off';
  }
  if (tool?.activation === 'session_grant') {
    return true;
  }
  if (tool?.activation !== TOOL_ACTIVATION_FOLLOWS) {
    return true;
  }

  const sourceName = tool.activation_source;
  if (!sourceName) {
    return false;
  }
  const source = toolByName(catalog, sourceName);
  const nextSeen = new Set(seen);
  nextSeen.add(tool.name);
  if (toolIsConfigurable(source)) {
    return (
      !(policy.denied ?? []).includes(sourceName) &&
      (policy.mode === TOOL_ACCESS_MODE_ALL ||
        (policy.mode === TOOL_ACCESS_MODE_SELECTED &&
          (policy.allowed ?? []).includes(sourceName)))
    );
  }
  return automaticToolIsActive(policy, source, catalog, context, nextSeen);
}

function ceilingNames(ceiling) {
  return Array.isArray(ceiling) ? new Set(normalizeNames(ceiling)) : null;
}

function compactPolicy(value) {
  const policy = { mode: value.mode };
  const denied = normalizeNames(value.denied);
  if (value.mode === TOOL_ACCESS_MODE_SELECTED) {
    const deniedSet = new Set(denied);
    policy.allowed = normalizeNames(value.allowed).filter(
      (name) => !deniedSet.has(name),
    );
  }
  if (denied.length > 0) {
    policy.denied = denied;
  }
  return policy;
}

function normalizeNames(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((name) => (typeof name === 'string' ? name.trim() : ''))
    .filter(
      (name, index, all) => name && name !== '*' && all.indexOf(name) === index,
    );
}

function sortTools(tools) {
  return [...tools].sort((left, right) => left.name.localeCompare(right.name));
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
