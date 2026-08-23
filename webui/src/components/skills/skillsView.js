// Pure presentation helpers for the Skills management view.
//
// The backend's `skill.inventory` returns one flat list of entries, each
// annotated with its origin tag, owner (for private homes), and share/disable
// state. This module owns the view-only projections: ordering the origin
// groups, deriving a group label from an entry, and mapping a status to a
// StatusChip variant. No Svelte, no transport.

// Group order mirrors the prompt catalog: Bundled / Global / per-Project /
// per-Agent private — Shared is appended as the manager-only extra group.
export const SKILL_GROUP_ORDER = Object.freeze([
  'bundled',
  'global',
  'project',
  'private',
  'shared',
]);

const PROJECT_ORIGIN_PREFIX = 'project:';
const AGENT_ORIGIN = 'agent';

function projectDisplayName(origin) {
  return origin.slice(PROJECT_ORIGIN_PREFIX.length);
}

// Which display group an inventory entry belongs to. Extension Skills carry the
// `global` origin tag, so they naturally appear under Global. A shared entry is
// shown once in the Shared group (with its owner), not hidden inside the
// owner's private list twice.
export function skillGroupKey(entry) {
  if (entry.shared) {
    return 'shared';
  }
  const { origin } = entry;
  if (origin === 'bundled') {
    return 'bundled';
  }
  if (origin === 'global' || origin === null || origin === undefined) {
    return 'global';
  }
  if (typeof origin === 'string' && origin.startsWith(PROJECT_ORIGIN_PREFIX)) {
    return 'project';
  }
  if (origin === AGENT_ORIGIN) {
    return 'private';
  }
  return 'global';
}

export function skillGroupLabel(entry, translate) {
  const key = skillGroupKey(entry);
  if (key === 'project') {
    return translate('skills.group.project', "Project '{name}'", {
      name: projectDisplayName(entry.origin),
    });
  }
  if (key === 'private') {
    return translate('skills.group.private', "Private '{name}'", {
      name: entry.owner_id || '',
    });
  }
  if (key === 'shared') {
    return translate('skills.group.shared', 'Shared');
  }
  if (key === 'bundled') {
    return translate('skills.group.bundled', 'Bundled');
  }
  return translate('skills.group.global', 'Global');
}

// Group the flat inventory into ordered `{ key, label, skills }` groups.
// Within a group, skills keep their server order (sorted by name/origin).
export function groupInventorySkills(entries, translate) {
  const groups = new Map();
  for (const entry of Array.isArray(entries) ? entries : []) {
    const key = skillGroupKey(entry);
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: skillGroupLabel(entry, translate),
        skills: [],
      });
    }
    groups.get(key).skills.push(entry);
  }
  return SKILL_GROUP_ORDER.filter((key) => groups.has(key)).map((key) =>
    groups.get(key),
  );
}

// Status → StatusChip variant. Disabled outranks everything (the server already
// collapses the status to `disabled`); invalid/unavailable are warnings/errors,
// available is success.
export function skillStatusVariant(entry) {
  switch (entry.status) {
    case 'available':
      return 'success';
    case 'unavailable':
      return 'warn';
    case 'disabled':
      return 'neutral';
    default:
      return 'error';
  }
}

export function skillStatusLabel(entry, translate) {
  switch (entry.status) {
    case 'available':
      return translate('skills.status.available', 'Available');
    case 'unavailable':
      return translate('skills.status.unavailable', 'Unavailable');
    case 'disabled':
      return translate('skills.status.disabled', 'Disabled');
    default:
      return translate('skills.status.invalid', 'Invalid');
  }
}

// A row's diagnostics disclosure content: requirement reasons plus validation
// warnings. Empty when there is nothing to explain.
export function skillDiagnosticLines(entry) {
  const missing = Array.isArray(entry.missing) ? entry.missing : [];
  const optional = Array.isArray(entry.optional_missing)
    ? entry.optional_missing
    : [];
  const warnings = Array.isArray(entry.warnings) ? entry.warnings : [];
  return [...missing, ...optional, ...warnings];
}

// Whether this row can be edited/deleted through the existing write scopes:
// only the data-dir global pool and an agent's private home are writable.
// Extension Skills are also tagged `global` and appear under Global; the
// backend editor scope (`skill.read`) only returns real scope directories, so
// an extension row would fail its content load — but keeping the simple origin
// rule here matches how every other surface treats the tag, and a failed edit
// surfaces a meaningful server error rather than a hidden control.
export function skillSupportsEditAndDelete(entry) {
  const key = skillGroupKey(entry);
  return key === 'global' || key === 'private';
}
