import { isPlainObject } from './values.js';

// Permission Rules editing (`.vorch/domain-maps/permissions.md`): an Agent's
// own rule set, the shared default, or a Project Team override. A rule set is
// null (no own rules: the level inherits, full access when nothing is
// inherited) or an object with the optional parts below. The server owns the
// rule language, the presets and validation (`permissions.validate`); this
// module only edits the stored form.

export const PERMISSION_BASES = Object.freeze({
  read: Object.freeze(['anywhere', 'work_area']),
  write: Object.freeze(['anywhere', 'work_area', 'scratch']),
  commands: Object.freeze(['any', 'listed']),
  web: Object.freeze(['any', 'listed']),
});

export const PERMISSION_BASE_PARTS = Object.freeze(
  Object.keys(PERMISSION_BASES),
);

// The value of a base no level sets.
export const PERMISSION_BASE_DEFAULTS = Object.freeze({
  read: 'anywhere',
  write: 'anywhere',
  commands: 'any',
  web: 'any',
});

export const PERMISSION_RULE_EFFECTS = Object.freeze(['allow', 'deny']);
export const PERMISSION_RULE_KINDS = Object.freeze([
  'Read',
  'Edit',
  'Shell',
  'Web',
]);

export const PERMISSION_PRESET_FULL_ACCESS = 'full_access';
export const PERMISSION_PRESET_CUSTOM = 'custom';
// The preset choice for no own rules: the level uses what it inherits.
export const PERMISSION_PRESET_INHERIT = 'inherit';

// The levels an editor edits: an Agent's own rules, a Team member's Project
// override (both inherit), and the shared default (inherits nothing, so no
// rules and Full access are the same choice there).
export const PERMISSION_LEVEL_AGENT = 'agent';
export const PERMISSION_LEVEL_OVERRIDE = 'override';
export const PERMISSION_LEVEL_DEFAULT = 'default';

const RULE_PATTERN = /^\s*([A-Za-z]+)\s*\(([\s\S]*)\)\s*$/;

// A rule set as the editors keep it: null, or a copy with only the known
// parts. Lists are copied so edits never touch the caller's value.
export function normalizePermissionRules(value) {
  if (!isPlainObject(value)) return null;
  const rules = {};
  for (const part of PERMISSION_BASE_PARTS) {
    if (typeof value[part] === 'string') rules[part] = value[part];
  }
  for (const effect of PERMISSION_RULE_EFFECTS) {
    if (Array.isArray(value[effect])) {
      rules[effect] = value[effect].filter((rule) => typeof rule === 'string');
    }
  }
  return rules;
}

// `Read(docs/**)` -> { kind: 'Read', pattern: 'docs/**' }; a text without the
// `Kind(pattern)` form keeps its whole text as the pattern of no kind.
export function splitPermissionRule(text) {
  const match = RULE_PATTERN.exec(String(text ?? ''));
  if (!match) return { kind: '', pattern: String(text ?? '').trim() };
  const kind =
    PERMISSION_RULE_KINDS.find(
      (candidate) => candidate.toLowerCase() === match[1].toLowerCase(),
    ) ?? match[1];
  return { kind, pattern: match[2] };
}

export function joinPermissionRule(kind, pattern) {
  return `${kind}(${pattern})`;
}

// An Agent can change only files it can read (the server's rule): `write`
// `anywhere` under `read` `work_area` narrows to `work_area`. The server
// refuses one rule set that holds both and narrows them across levels.
export function writeWithinRead(read, write) {
  return read === 'work_area' && write === 'anywhere' ? 'work_area' : write;
}

// `rules` with one base set; `null` removes the part so it is inherited. A
// rule set without own rules starts empty: setting a part makes it own rules.
// An own `write` that the new `read` no longer permits narrows with it.
export function withPermissionBase(rules, part, value) {
  const next = { ...(normalizePermissionRules(rules) ?? {}) };
  if (value === null || value === undefined) delete next[part];
  else next[part] = value;
  if (typeof next.write === 'string') {
    next.write = writeWithinRead(next.read, next.write);
  }
  return next;
}

// `rules` with its own allow or deny list replaced; `null` removes the list
// so the inherited one applies.
export function withPermissionRuleList(rules, effect, list) {
  const next = { ...(normalizePermissionRules(rules) ?? {}) };
  if (list === null || list === undefined) delete next[effect];
  else next[effect] = [...list];
  return next;
}

// `rules` with a preset applied (`definition` from the server's `presets`):
// `commands` and `web` are unset, then every part the definition sets is
// set. Full access defines every part, empty lists included, so it replaces
// the own rules and overrides everything inherited; the other presets set
// their bases and keep the own allow and deny lists.
export function applyPermissionPreset(rules, definition) {
  if (!isPlainObject(definition)) return null;
  const own = normalizePermissionRules(rules) ?? {};
  const next = {};
  for (const effect of PERMISSION_RULE_EFFECTS) {
    if (own[effect]) next[effect] = [...own[effect]];
  }
  return { ...next, ...normalizePermissionRules(definition) };
}

// Whether two rule sets hold the same parts and rules.
export function samePermissionRules(left, right) {
  const a = normalizePermissionRules(left);
  const b = normalizePermissionRules(right);
  if (a === null || b === null) return a === b;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].every(
    (key) => JSON.stringify(a[key]) === JSON.stringify(b[key]),
  );
}

// The preset choice an editor shows for the own rules `rules` at `level`.
// `preset` is the server's `preset` of the resulting rules, `presets` its
// definitions. Where a level inherits, no own rules is "use the inherited
// rules" and Full access only the stored Full access set, which overrides
// them; rules that merely restrict nothing yet show as Custom. The shared
// default inherits nothing, so no rules shows as Full access there.
export function permissionPresetChoice(rules, { level, preset, presets }) {
  const own = normalizePermissionRules(rules);
  const definitions = isPlainObject(presets) ? presets : {};
  if (level === PERMISSION_LEVEL_DEFAULT) {
    if (own === null || preset === PERMISSION_PRESET_FULL_ACCESS) {
      return PERMISSION_PRESET_FULL_ACCESS;
    }
  } else {
    if (own === null) return PERMISSION_PRESET_INHERIT;
    if (
      Object.hasOwn(definitions, PERMISSION_PRESET_FULL_ACCESS) &&
      samePermissionRules(own, definitions[PERMISSION_PRESET_FULL_ACCESS])
    ) {
      return PERMISSION_PRESET_FULL_ACCESS;
    }
  }
  if (
    typeof preset === 'string' &&
    preset !== PERMISSION_PRESET_FULL_ACCESS &&
    Object.hasOwn(definitions, preset)
  ) {
    return preset;
  }
  return PERMISSION_PRESET_CUSTOM;
}

// The base `part` that applies: the own value, else the inherited one, else
// its unrestricted default. `source` says which level supplied it. `write`
// stays within the `read` that applies.
export function effectivePermissionBase(rules, inherited, part) {
  if (part === 'write') {
    const write = levelPermissionBase(rules, inherited, part);
    const read = levelPermissionBase(rules, inherited, 'read').value;
    return { ...write, value: writeWithinRead(read, write.value) };
  }
  return levelPermissionBase(rules, inherited, part);
}

function levelPermissionBase(rules, inherited, part) {
  const own = normalizePermissionRules(rules);
  if (typeof own?.[part] === 'string') {
    return { value: own[part], source: 'own' };
  }
  const parent = normalizePermissionRules(inherited);
  if (typeof parent?.[part] === 'string') {
    return { value: parent[part], source: 'inherited' };
  }
  return { value: PERMISSION_BASE_DEFAULTS[part], source: 'default' };
}

// Rules pasted into an editor: one rule per line, or a JSON object in the
// form of Claude Code settings (`{"permissions": {"allow": [...], "deny":
// [...]}}`) or of a rule set (`{"allow": [...], "deny": [...]}`). Lines go to
// `effect`; JSON lists keep their own effect. Blank lines and list markers
// are dropped.
export function parsePastedPermissionRules(text, effect = 'allow') {
  const source = String(text ?? '').trim();
  const pasted = { allow: [], deny: [] };
  if (!source) return pasted;
  if (source.startsWith('{')) {
    try {
      const parsed = JSON.parse(source);
      const lists = isPlainObject(parsed?.permissions)
        ? parsed.permissions
        : parsed;
      if (isPlainObject(lists)) {
        for (const listEffect of PERMISSION_RULE_EFFECTS) {
          if (Array.isArray(lists[listEffect])) {
            pasted[listEffect] = lists[listEffect]
              .filter((rule) => typeof rule === 'string' && rule.trim())
              .map((rule) => rule.trim());
          }
        }
        return pasted;
      }
    } catch {
      // Not JSON: read it as lines.
    }
  }
  pasted[effect] = source
    .split(/\r?\n/)
    .map((line) =>
      line
        .trim()
        .replace(/^[-*]\s+/, '')
        .replace(/^["']|["'],?$/g, '')
        .trim(),
    )
    .filter(Boolean);
  return pasted;
}

// `rules` with the pasted rules appended to its own lists; an inherited list
// is copied first, so pasting adds to what applied before.
export function withPastedPermissionRules(rules, inherited, pasted) {
  let next = normalizePermissionRules(rules) ?? {};
  const parent = normalizePermissionRules(inherited) ?? {};
  for (const effect of PERMISSION_RULE_EFFECTS) {
    const added = pasted?.[effect] ?? [];
    if (added.length === 0) continue;
    const current = next[effect] ?? parent[effect] ?? [];
    next = withPermissionRuleList(next, effect, [
      ...current,
      ...added.filter((rule) => !current.includes(rule)),
    ]);
  }
  return next;
}

// Whether every own rule has a pattern; a rule being typed has none yet.
// Saving waits until it does (the server would refuse the rule set).
export function permissionRulesComplete(rules) {
  const own = normalizePermissionRules(rules);
  if (!own) return true;
  return PERMISSION_RULE_EFFECTS.every((effect) =>
    (own[effect] ?? []).every(
      (rule) => splitPermissionRule(rule).pattern.trim() !== '',
    ),
  );
}

// The problems `permissions.validate` reported for one rule, or for a part
// (`index` null).
export function permissionProblems(errors, part, index = null) {
  if (!Array.isArray(errors)) return [];
  return errors
    .filter(
      (error) =>
        error?.part === part && (error?.index ?? null) === (index ?? null),
    )
    .map((error) => String(error.message ?? ''));
}

// Whether a Tool result is a refusal by the Agent's Permission Rules.
export function isPermissionDeniedResult(result) {
  let value = result;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return false;
    }
  }
  return value?.error?.code === 'permission_denied';
}
