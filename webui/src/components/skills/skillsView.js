// Presentation only: the server owns availability, identity, and write scopes.
export const SKILL_PAGE_SIZE = 50;

export function agentDisplayName(agentId, agents) {
  return agents.find((agent) => agent.id === agentId)?.name || agentId;
}

export function skillSourceLabel(entry, translate, agents) {
  if (entry.owner_id) return agentDisplayName(entry.owner_id, agents);
  if (entry.origin?.startsWith('project:')) return entry.origin.slice(8);
  if (entry.origin === 'bundled')
    return translate('skills.library.bundled', 'Included with vBot');
  return (
    entry.source_label || translate('skills.library.global', 'Global skills')
  );
}

export function matchesSkillScope(entry, scope) {
  if (scope === 'all') return true;
  if (scope === 'shared') return Boolean(entry.shared);
  if (scope.startsWith('agent:')) return entry.owner_id === scope.slice(6);
  return entry.origin === scope;
}

export function skillCollections(entries, agents, translate) {
  const items = [
    {
      key: 'all',
      label: translate('skills.library.all', 'All skills'),
      section: 'library',
    },
    {
      key: 'shared',
      label: translate('skills.library.shared', 'Shared skills'),
      section: 'library',
    },
    ...agents.map((agent) => ({
      key: `agent:${agent.id}`,
      label: agent.name || agent.id,
      section: 'agents',
    })),
    {
      key: 'global',
      label: translate('skills.library.global', 'Global skills'),
      section: 'sources',
    },
    {
      key: 'bundled',
      label: translate('skills.library.bundled', 'Included with vBot'),
      section: 'sources',
    },
    ...[
      ...new Set(
        entries
          .map((entry) => entry.origin)
          .filter((origin) => origin?.startsWith('project:')),
      ),
    ]
      .sort()
      .map((origin) => ({
        key: origin,
        label: origin.slice(8),
        section: 'projects',
      })),
  ];
  return items.map((item) => ({
    ...item,
    count: entries.filter((entry) => matchesSkillScope(entry, item.key)).length,
  }));
}

export function filterSkills(
  entries,
  query,
  scope = 'all',
  status = 'all',
  agents = [],
) {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return entries
    .filter((entry) => {
      if (!matchesSkillScope(entry, scope)) return false;
      if (status === 'attention') {
        if (
          !skillDiagnosticLines(entry).length &&
          !['unavailable', 'invalid'].includes(entry.status)
        )
          return false;
      } else if (status !== 'all' && entry.status !== status) return false;
      const text = [
        entry.name,
        entry.description,
        entry.origin,
        entry.source_label,
        entry.owner_id,
        agentDisplayName(entry.owner_id, agents),
      ]
        .join(' ')
        .toLocaleLowerCase();
      return words.every((word) => text.includes(word));
    })
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) ||
        (left.id || '').localeCompare(right.id || ''),
    );
}

// Only the presentation omits YAML; the Original text tab and editor retain it.
export function skillInstructionBody(content) {
  return content.replace(
    /^\uFEFF?---[^\S\r\n]*\r?\n[\s\S]*?\r?\n---[^\S\r\n]*(?:\r?\n|$)/,
    '',
  );
}

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

export function createSkillDocument(name, description, instructions) {
  return `---\nname: ${JSON.stringify(name.trim())}\ndescription: ${JSON.stringify(description.trim())}\n---\n\n${instructions}`;
}
