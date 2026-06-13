<script>
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    skills = [],
    query = '',
    marker = '/',
    activeIndex = 0,
    onSelect = noop,
    onHover = noop,
  } = $props();

  let normalizedSkills = $derived(normalizeSkills(skills));
  let matchingSkills = $derived(matchSkills(normalizedSkills, query));

  export function hasMatches() {
    return matchingSkills.length > 0;
  }

  export function selectActive() {
    const skill = matchingSkills[activeIndex] ?? matchingSkills[0];

    if (skill) {
      onSelect(skill);
      return true;
    }

    return false;
  }

  function normalizeSkills(items) {
    return items
      .filter(
        (skill) => typeof skill?.name === 'string' && skill.name.length > 0,
      )
      .map((skill) => ({
        name: skill.name,
        description: skill.description ?? '',
        searchText: `${skill.name} ${skill.description ?? ''}`.toLowerCase(),
      }));
  }

  function matchSkills(items, value) {
    const normalizedQuery = value.trim().toLowerCase();
    const filteredSkills = normalizedQuery
      ? items.filter((skill) => skill.searchText.includes(normalizedQuery))
      : items;

    return filteredSkills;
  }

  function eyebrowText() {
    if (marker === '$') {
      return t('skillAutocomplete.eyebrow.skills', 'skills');
    }

    return t(
      'skillAutocomplete.eyebrow.commandsAndSkills',
      'commands & skills',
    );
  }
</script>

{#if matchingSkills.length > 0}
  <div
    class="skill-autocomplete"
    role="listbox"
    aria-label={t('skillAutocomplete.label', 'Skill suggestions')}
  >
    <div class="skill-autocomplete__eyebrow">
      {eyebrowText()}
    </div>
    {#each matchingSkills as skill, index (skill.name)}
      <button
        type="button"
        class="skill-autocomplete__option"
        class:active={index === activeIndex}
        role="option"
        aria-selected={index === activeIndex}
        onmouseenter={() => onHover(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(skill)}
      >
        <span class="skill-autocomplete__name">{skill.name}</span>
        {#if skill.description}
          <span class="skill-autocomplete__description">
            {skill.description}
          </span>
        {:else}
          <span class="skill-autocomplete__description muted">
            {t('skillAutocomplete.noDescription', 'No description available')}
          </span>
        {/if}
      </button>
    {/each}
  </div>
{/if}

<style>
  .skill-autocomplete {
    position: absolute;
    right: 20px;
    bottom: calc(100% - 8px);
    left: 20px;
    z-index: 20;
    display: flex;
    flex-direction: column;
    max-height: min(320px, 45vh);
    overflow-y: auto;
    border: 1px solid rgba(232, 135, 10, 0.3);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }

  .skill-autocomplete__eyebrow {
    padding: 8px 10px 6px;
    border-bottom: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    line-height: 1;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .skill-autocomplete__option {
    display: grid;
    grid-template-columns: minmax(96px, 0.42fr) minmax(0, 1fr);
    gap: 12px;
    width: 100%;
    padding: 9px 10px;
    border: 0;
    border-bottom: 1px solid var(--border);
    color: var(--text-med);
    background: transparent;
    text-align: left;
    transition:
      background-color 120ms ease,
      color 120ms ease;
  }

  .skill-autocomplete__option:last-child {
    border-bottom: 0;
  }

  .skill-autocomplete__option:hover,
  .skill-autocomplete__option.active {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .skill-autocomplete__option.active .skill-autocomplete__name {
    color: var(--accent);
  }

  .skill-autocomplete__name {
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skill-autocomplete__description {
    overflow: hidden;
    color: inherit;
    font-family: var(--font-ui);
    font-size: 12.5px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skill-autocomplete__description.muted {
    color: var(--text-lo);
    font-style: italic;
  }

  @media (max-width: 640px) {
    .skill-autocomplete {
      right: 14px;
      left: 14px;
    }

    .skill-autocomplete__option {
      grid-template-columns: 1fr;
      gap: 3px;
    }
  }
</style>
