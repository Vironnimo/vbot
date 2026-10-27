<script>
  import AutocompletePopup from './composer/AutocompletePopup.svelte';
  import { t } from '$lib/i18n.js';
  import { skillAutocompleteRows } from './composer/autocomplete.js';

  const noop = () => {};

  let {
    skills = [],
    query = '',
    marker = '/',
    activeIndex = 0,
    onSelect = noop,
    onHover = noop,
  } = $props();

  let matchingSkills = $derived(skillAutocompleteRows(skills, query));
  let popup = $state(null);

  export function hasMatches() {
    return matchingSkills.length > 0;
  }

  export function selectActive() {
    return popup?.selectActive() ?? false;
  }

  function eyebrowText() {
    if (marker === '$') {
      return t('skillAutocomplete.eyebrow.skills');
    }

    return t('skillAutocomplete.eyebrow.commandsAndSkills');
  }
</script>

<AutocompletePopup
  bind:this={popup}
  kind="skill"
  items={matchingSkills}
  keyOf={(skill) => `${skill.type}:${skill.name}`}
  {activeIndex}
  ariaLabel={t('skillAutocomplete.label')}
  eyebrow={eyebrowText()}
  {onSelect}
  {onHover}
>
  {#snippet row(skill)}
    <span class="skill-autocomplete__name">{skill.name}</span>
    {#if skill.description}
      <span class="skill-autocomplete__description">
        {skill.description}
      </span>
    {:else}
      <span class="skill-autocomplete__description muted">
        {t('skillAutocomplete.noDescription')}
      </span>
    {/if}
  {/snippet}
</AutocompletePopup>

<style>
  .skill-autocomplete__name {
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skill-autocomplete__description {
    overflow: hidden;
    color: inherit;
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .skill-autocomplete__description.muted {
    color: var(--text-lo);
    font-style: italic;
  }
</style>
