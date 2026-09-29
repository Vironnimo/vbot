<script>
  // Collection navigation of the Skills manager: the library filters, then
  // each Identity Agent and Project with the number of Skills it activates.
  // Wide layouts show a sidebar list; narrow ones one grouped Dropdown.
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';

  const noop = () => {};

  let {
    collections = [],
    scope = '',
    variant = 'sidebar',
    onSelect = noop,
  } = $props();

  let sections = $derived(
    [
      { id: 'library', label: t('skills.library') },
      { id: 'agents', label: t('skills.section.agents') },
      { id: 'projects', label: t('skills.section.projects') },
    ]
      .map((section) => ({
        ...section,
        items: collections.filter((item) => item.section === section.id),
      }))
      .filter((section) => section.items.length > 0),
  );
</script>

{#if variant === 'dropdown'}
  <Dropdown
    value={scope}
    options={sections.flatMap((section) =>
      section.items.map((item) => ({
        value: item.key,
        label: item.label,
        secondaryLabel: String(item.count),
        group: section.label,
      })),
    )}
    placeholder={t('skills.folders.title')}
    ariaLabel={t('skills.collections')}
    onValueChange={onSelect}
  />
{:else}
  <nav class="secondary-list">
    {#each sections as section (section.id)}
      <h3 class="skills-nav-label">{section.label}</h3>
      {#each section.items as item (item.key)}
        <button
          type="button"
          class="secondary-list__item skills-collection"
          class:active={scope === item.key}
          aria-current={scope === item.key ? 'page' : undefined}
          onclick={() => onSelect(item.key)}
        >
          <span
            class="skills-collection-name"
            use:tooltip={{
              text: item.label,
              placement: 'right',
              whenTruncated: true,
            }}>{item.label}</span
          >
          <span class="skills-count">{item.count}</span>
        </button>
      {/each}
    {/each}
  </nav>
{/if}
