import { optionDecorations } from '$lib/dropdownPanel.js';

// Rendering and keyboard navigation consume the same normalized match sets.
export function skillAutocompleteRows(items, query) {
  return matchSkills(normalizeSkills(items), query);
}

export function modelAutocompleteRows(items, query) {
  return matchOptions(normalizeOptions(items), query);
}

function normalizeSkills(items) {
  return items
    .filter((skill) => typeof skill?.name === 'string' && skill.name.length > 0)
    .map((skill) => ({
      name: skill.name,
      description: skill.description ?? '',
      // Carry trigger metadata through so the composer can tell a no-argument
      // command (run immediately) from a skill or argument-bearing command
      // (insert token and wait) when one is selected.
      type: skill.type,
      argument: skill.argument,
      searchName: skill.name.toLowerCase(),
      searchText: `${skill.name} ${skill.description ?? ''}`.toLowerCase(),
    }));
}

function matchRank(skill, normalizedQuery) {
  if (skill.searchName.startsWith(normalizedQuery)) {
    return 0;
  }
  if (skill.searchName.includes(normalizedQuery)) {
    return 1;
  }
  if (skill.searchText.includes(normalizedQuery)) {
    return 2;
  }
  return -1;
}

function matchSkills(items, value) {
  const normalizedQuery = value.trim().toLowerCase();
  if (!normalizedQuery) {
    return items;
  }

  // Keep any match (name or description), but rank name hits — and prefix
  // hits ahead of those — above description-only hits. Original order breaks
  // ties since sort() is stable.
  return items
    .map((skill, index) => ({
      skill,
      index,
      rank: matchRank(skill, normalizedQuery),
    }))
    .filter((entry) => entry.rank !== -1)
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map((entry) => entry.skill);
}

function normalizeOptions(items) {
  return items
    .filter(
      (option) => typeof option?.value === 'string' && option.value.length > 0,
    )
    .map((option) => ({
      value: option.value,
      label: option.label ?? option.value,
      // The Model id's provider path, shown muted.
      labelLead:
        typeof option.labelLead === 'string' &&
        (option.label ?? option.value).startsWith(option.labelLead)
          ? option.labelLead
          : '',
      secondaryLabel: option.secondaryLabel ?? '',
      marker: optionDecorations(option).marker,
      searchText:
        `${option.label ?? option.value} ${option.secondaryLabel ?? ''}`
          .trim()
          .toLowerCase(),
    }));
}

function matchOptions(items, value) {
  const normalizedQuery = value.trim().toLowerCase();

  if (!normalizedQuery) {
    return items;
  }

  return items.filter((option) => option.searchText.includes(normalizedQuery));
}
