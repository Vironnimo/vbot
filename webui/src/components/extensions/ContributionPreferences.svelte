<script>
  import { useContributions } from '$lib/contributions.svelte.js';
  import {
    builtinContributionEntries,
    contributionSurfaceOptions,
  } from '$lib/contributionPresentation.js';
  import { t } from '$lib/i18n.js';
  import SortableList from '../ui/SortableList.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import Dropdown from '../Dropdown.svelte';

  const registry = useContributions();
  const areaId = $props.id();
  let surface = $state('chat.composer.actions');
  const options = $derived(contributionSurfaceOptions());
  const layout = $derived(registry?.layout(surface) ?? {});
  const disabled = $derived(!registry?.ready || registry.migrationPending);
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
  function selectReplacement(target, replacement) {
    const replacements = { ...layout.replacements };
    if (replacement) replacements[target] = replacement;
    else delete replacements[target];
    registry.save(surface, { replacements });
  }
</script>

{#if registry}
  <div class="contribution-preferences">
    {#if registry.error || !registry.ready || registry.migrationPending}
      <div class="contribution-preferences__notice">
        {#if registry.error}<Banner variant="error">{registry.error}</Banner
          >{/if}
        {#if !registry.ready}
          <p class="s-row-desc" role="status">{t('contributions.loading')}</p>
          {#if registry.error}<Button
              variant="tertiary"
              onClick={() => registry.refresh()}
              >{t('contributions.loadRetry')}</Button
            >{/if}
        {/if}
        {#if registry.migrationPending}
          <Banner>{t('contributions.migration')}</Banner>
          <div class="s-group-toolbar__actions">
            <Button onClick={() => registry.migrate(true)}
              >{t('contributions.importLayout')}</Button
            >
            <Button variant="tertiary" onClick={() => registry.migrate(false)}
              >{t('contributions.useDefaults')}</Button
            >
          </div>
        {/if}
      </div>
    {/if}
    <fieldset class="s-group__rows" {disabled}>
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for={areaId}
            >{t('contributions.surface')}</label
          >
          <p class="s-row-desc">{t('contributions.help')}</p>
        </div>
        <div class="s-row-control">
          <Dropdown
            id={areaId}
            value={surface}
            {options}
            ariaLabel={t('contributions.surface')}
            triggerClass="settings-view__dropdown"
            listClass="settings-view__thinking-list"
            onValueChange={(value) => (surface = value)}
          />
        </div>
      </div>
      {#if entries.length}
        <SortableList
          items={entries}
          getLabel={(entry) => entry.title}
          onReorder={reorder}
          class="s-group__rows contribution-preferences__list"
          itemClass="s-row s-row--compact"
          {disabled}
          itemFocusable
        >
          {#snippet item(entry)}
            <div class="s-row-info">
              <span class="contribution-preferences__name">{entry.title}</span>
              {#if !entry.replaces && entries.some((item) => item.replaces === entry.id)}
                <InfoHint text={t('contributions.replacementHelp')} />
                <Dropdown
                  value={layout.replacements?.[entry.id] ?? ''}
                  options={replacementOptions(entry)}
                  ariaLabel={t('contributions.replacement', {
                    name: entry.title,
                  })}
                  onValueChange={(value) => selectReplacement(entry.id, value)}
                />
              {/if}
            </div>
            <div class="s-row-control">
              <Toggle
                size="sm"
                checked={!(layout.hidden ?? []).includes(entry.id)}
                ariaLabel={t('contributions.visible', { name: entry.title })}
                onChange={(visible) => toggle(entry, visible)}
              />
            </div>
          {/snippet}
        </SortableList>
      {:else}
        <p class="contribution-preferences__notice s-row-desc">
          {t('contributions.empty')}
        </p>
      {/if}
    </fieldset>
    <div class="contribution-preferences__footer">
      <SaveStatus
        saving={registry.saving}
        pending={registry.pending}
        onClick={() => registry.retry()}
      />
      <Button
        variant="tertiary"
        {disabled}
        onClick={() => registry.reset(surface)}
      >
        {t('contributions.reset')}
      </Button>
    </div>
  </div>
{/if}

<style>
  fieldset {
    border: 0;
    padding: 0;
    margin: 0;
    min-width: 0;
  }
  .contribution-preferences__notice {
    display: grid;
    justify-items: start;
    gap: 8px;
    padding: 12px 18px;
  }
  .contribution-preferences :global(.contribution-preferences__list) {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: 1px solid var(--border);
  }
  .contribution-preferences__name {
    font-size: var(--fs-body-md);
    overflow-wrap: anywhere;
  }
  .contribution-preferences__footer {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 8px;
    padding: 6px 12px;
    border-top: 1px solid var(--border);
  }
</style>
