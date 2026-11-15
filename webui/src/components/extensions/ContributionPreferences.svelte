<script>
  import { useContributions } from '$lib/contributions.svelte.js';
  import {
    builtinContributionEntries,
    contributionSurfaceOptions,
  } from '$lib/contributionPresentation.js';
  import { t } from '$lib/i18n.js';
  import SortableList from '../ui/SortableList.svelte';
  import Button from '../ui/Button.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import Dropdown from '../Dropdown.svelte';

  const registry = useContributions();
  let surface = $state('chat.composer.actions');
  const options = $derived(contributionSurfaceOptions());
  const layout = $derived(registry?.layout(surface) ?? {});
  const entries = $derived.by(() => {
    const all = [
      ...builtinContributionEntries(surface),
      ...(registry?.mounted[surface] ?? []),
      ...(registry?.entries ?? []).filter((entry) => entry.surface === surface),
    ];
    const order = layout.order ?? [];
    // Later entries refine the catalog with the mounted title/order and the
    // current Extension declaration without rendering duplicate controls.
    return Object.values(
      Object.fromEntries(all.map((entry) => [entry.id, entry])),
    ).sort((a, b) => {
      const ai = order.indexOf(a.id),
        bi = order.indexOf(b.id);
      if (ai >= 0 || bi >= 0)
        return (ai < 0 ? Infinity : ai) - (bi < 0 ? Infinity : bi);
      return (a.order ?? 100) - (b.order ?? 100) || a.id.localeCompare(b.id);
    });
  });

  function toggle(entry, visible) {
    const hidden = (layout.hidden ?? []).filter((id) => id !== entry.id);
    if (!visible) hidden.push(entry.id);
    registry.save(surface, { hidden });
  }
  function reorder(from, to) {
    const ids = entries.map((entry) => entry.id);
    const [id] = ids.splice(from, 1);
    ids.splice(to, 0, id);
    registry.save(surface, {
      order: [
        ...ids,
        ...(layout.order ?? []).filter((old) => !ids.includes(old)),
      ],
    });
  }
  function replacementOptions(entry) {
    return [
      { value: '', label: t('contributions.standard') },
      ...entries
        .filter((item) => item.replaces === entry.id)
        .map((item) => ({ value: item.id, label: item.title })),
    ];
  }
</script>

{#if registry}
  <section class="contribution-preferences">
    <h3>{t('contributions.title')}</h3>
    <p class="s-desc">{t('contributions.help')}</p>
    <div class="contribution-preferences__toolbar">
      <Dropdown
        value={surface}
        {options}
        ariaLabel={t('contributions.surface')}
        onValueChange={(value) => (surface = value)}
      />
      <Button variant="tertiary" onClick={() => registry.reset(surface)}
        >{t('contributions.reset')}</Button
      >
    </div>
    {#if registry.storageError}<Banner variant="error"
        >{t('contributions.storageError')}</Banner
      >{/if}
    {#if entries.length}
      <SortableList
        items={entries}
        getLabel={(entry) => entry.title}
        onReorder={reorder}
        class="contribution-preferences__list"
        itemClass="contribution-preferences__item"
        itemFocusable
      >
        {#snippet item(entry)}
          <div class="contribution-preferences__row">
            <span>{entry.title}</span>
            <Toggle
              size="sm"
              checked={!(layout.hidden ?? []).includes(entry.id)}
              ariaLabel={t('contributions.visible', { name: entry.title })}
              onChange={(visible) => toggle(entry, visible)}
            />
          </div>
          {#if !entry.replaces && entries.some((item) => item.replaces === entry.id)}
            <Dropdown
              value={layout.replacements?.[entry.id] ?? ''}
              options={replacementOptions(entry)}
              ariaLabel={t('contributions.replacement', { name: entry.title })}
              onValueChange={(value) =>
                registry.save(surface, {
                  replacements: { ...layout.replacements, [entry.id]: value },
                })}
            />
          {/if}
        {/snippet}
      </SortableList>
    {:else}<p class="s-desc">{t('contributions.empty')}</p>{/if}
  </section>
{/if}

<style>
  .contribution-preferences {
    margin-top: 20px;
  }
  .contribution-preferences__toolbar,
  .contribution-preferences__row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .contribution-preferences :global(.contribution-preferences__list) {
    list-style: none;
    padding: 0;
  }
  .contribution-preferences :global(.contribution-preferences__item) {
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
</style>
