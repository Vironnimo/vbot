<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ContextMenu from '../../../../webui/src/components/ui/ContextMenu.svelte';
  import {
    contextMenuAnchor,
    isContextMenuKey,
  } from '../../../../webui/src/components/ui/contextMenu.js';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import {
    profileSummary,
    profileTooltip,
    runRowDetail,
    runStateVariant,
    runTitle,
    runTooltip,
  } from './runPresentation.js';
  import './swarmSidebar.css';

  // The page's navigation: New Run (the start page), the Swarms (profiles)
  // and the Runs, open ones first. A row opens its Swarm or Run; its context
  // menu holds the same actions as the Run header's menu, the profile editor
  // and the start form, so nothing is reachable by right click alone.
  // On a narrow page the bar after the pane replaces it: one picker with the
  // same entries, whose value is what the work area shows, and New Swarm.
  let { model } = $props();

  const startVisible = $derived(!model.editor && !model.selectedSwarm);

  // The picker is the narrow page's navigation, so its panel may take most of
  // the height below it rather than a form picker's few rows.
  const PICKER_MAX_HEIGHT = 560;

  // The picker's entries; choosing one does what clicking its row does.
  const entries = $derived([
    { value: 'start', label: t('swarm.newRun'), open: model.openStart },
    // A new Swarm's editor has no row; while it shows, it has an entry.
    ...(model.editor === 'new'
      ? [
          {
            value: 'profile:new',
            label: t('swarm.newProfile'),
            group: t('swarm.profiles'),
            open: () => model.navigate(() => model.openProfile()),
          },
        ]
      : []),
    ...model.profiles.map((profile) => ({
      value: `profile:${profile.id}`,
      label: profile.name,
      secondaryLabel: profileSummary(profile),
      group: t('swarm.profiles'),
      open: () => model.navigate(() => model.openProfile(profile)),
    })),
    ...model.runGroups.flatMap((group) =>
      group.entries.map((swarm) => ({
        value: `run:${swarm.id}`,
        label: runTitle(swarm),
        secondaryLabel: runRowDetail(swarm, model.now),
        group: group.label,
        open: () => model.navigate(() => model.openSwarm(swarm.id)),
      })),
    ),
  ]);

  // What the work area shows, as the picker's value and its name; a Run
  // beyond the loaded pages has no entry and names itself as placeholder.
  const shown = $derived.by(() => {
    if (model.editor === 'new')
      return { value: 'profile:new', label: t('swarm.newProfile') };
    if (model.editor) {
      const value = `profile:${model.editor.id}`;
      return { value, label: entryLabel(value) ?? model.editor.name };
    }
    if (model.selectedRun) {
      const value = `run:${model.selectedRun.id}`;
      return { value, label: entryLabel(value) ?? runTitle(model.selectedRun) };
    }
    return { value: 'start', label: t('swarm.newRun') };
  });

  function entryLabel(value) {
    return entries.find((entry) => entry.value === value)?.label;
  }

  // The panel's footer loads the next page of each list that has one.
  const moreLabel = $derived(
    model.profilesCursor && model.swarmsCursor
      ? t('swarm.sidebar.more')
      : model.profilesCursor
        ? t('swarm.profiles.more')
        : model.swarmsCursor
          ? t('swarm.swarms.more')
          : '',
  );

  function loadMore() {
    if (model.profilesCursor) void model.loadMoreProfiles();
    if (model.swarmsCursor) void model.loadMoreSwarms();
  }

  function choose(value) {
    void entries.find((entry) => entry.value === value)?.open();
  }

  // The open row context menu, or null.
  let menu = $state(null);

  // `actions` builds the menu's label and items when it opens.
  function openMenu(event, actions) {
    if (event.defaultPrevented) return;
    event.preventDefault();
    menu = { ...contextMenuAnchor(event), ...actions() };
  }

  function menuKey(event, actions) {
    if (isContextMenuKey(event)) openMenu(event, actions);
  }
</script>

{#snippet newSwarmButton()}
  <Button
    variant="tertiary"
    icon
    ariaLabel={t('swarm.newProfile')}
    tooltip={t('swarm.newProfile')}
    loading={model.pending === 'new-profile'}
    onClick={() => model.navigate(() => model.openProfile())}
    ><svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
      <path d="M7 1.5v11M1.5 7h11" />
    </svg></Button
  >
{/snippet}

<aside class="secondary-pane swarm-pane" aria-labelledby="swarm-pane-title">
  <div class="secondary-pane__header swarm-pane__header">
    <span id="swarm-pane-title" class="secondary-pane__title"
      >{t('swarm.profiles')}</span
    >
    {@render newSwarmButton()}
  </div>
  <div class="secondary-pane__pinned swarm-pane__pinned">
    <Button
      variant="tertiary"
      class={`secondary-list__item secondary-pane__pinned-item swarm-new-run ${startVisible ? 'active' : ''}`}
      aria-current={startVisible ? 'page' : undefined}
      onClick={model.openStart}>{t('swarm.newRun')}</Button
    >
  </div>
  <div class="secondary-pane__scroll secondary-list swarm-pane__scroll">
    <div class="swarm-pane__group">
      {#if model.profiles.length}
        <nav class="swarm-pane__list" aria-label={t('swarm.profiles')}>
          {#each model.profiles as profile (profile.id)}
            {@const current = model.editor?.id === profile.id}
            <button
              type="button"
              class="secondary-list__item swarm-row"
              class:active={current}
              aria-current={current ? 'page' : undefined}
              data-profile-id={profile.id}
              use:tooltip={() => profileTooltip(profile)}
              onclick={() => model.navigate(() => model.openProfile(profile))}
              oncontextmenu={(event) =>
                openMenu(event, () => model.runActions.profileMenu(profile))}
              onkeydown={(event) =>
                menuKey(event, () => model.runActions.profileMenu(profile))}
            >
              <span class="swarm-row__inner">
                <span class="swarm-row__name">{profile.name}</span>
                <span class="swarm-row__detail">{profileSummary(profile)}</span>
              </span>
            </button>
          {/each}
        </nav>
      {:else if !model.loading}
        <EmptyState density="compact" description={t('swarm.noProfiles')} />
      {/if}
      {#if model.profilesCursor}<Button
          variant="tertiary"
          class="swarm-pane__more"
          loading={model.moreProfilesLoading}
          onClick={model.loadMoreProfiles}>{t('swarm.profiles.more')}</Button
        >{/if}
    </div>
    {#each model.runGroups as group (group.id)}
      <section
        class="swarm-pane__group"
        aria-labelledby={`swarm-runs-${group.id}`}
      >
        <h3 class="secondary-list__group" id={`swarm-runs-${group.id}`}>
          {group.label}
        </h3>
        {#if group.entries.length}
          <nav class="swarm-pane__list" aria-label={group.label}>
            {#each group.entries as swarm (swarm.id)}
              {@const current =
                !model.editor && model.selectedSwarm?.id === swarm.id}
              <button
                type="button"
                class="secondary-list__item swarm-row"
                class:active={current}
                aria-current={current ? 'page' : undefined}
                data-run-id={swarm.id}
                use:tooltip={() => runTooltip(swarm, model.now)}
                onclick={() => model.navigate(() => model.openSwarm(swarm.id))}
                oncontextmenu={(event) =>
                  openMenu(event, () => model.runActions.runMenu(swarm))}
                onkeydown={(event) =>
                  menuKey(event, () => model.runActions.runMenu(swarm))}
              >
                <span class="swarm-row__inner">
                  <span class="swarm-row__head">
                    <span
                      class={`swarm-row__dot swarm-row__dot--${runStateVariant(swarm.state)}`}
                      aria-hidden="true"
                    ></span>
                    <span class="swarm-row__name">{runTitle(swarm)}</span>
                  </span>
                  <span class="swarm-row__detail"
                    >{runRowDetail(swarm, model.now)}</span
                  >
                </span>
              </button>
            {/each}
          </nav>
        {:else if !model.loading}
          <EmptyState density="compact" description={group.empty} />
        {/if}
      </section>
    {/each}
    {#if model.swarmsCursor}<Button
        variant="tertiary"
        class="swarm-pane__more"
        loading={model.moreSwarmsLoading}
        onClick={model.loadMoreSwarms}>{t('swarm.swarms.more')}</Button
      >{/if}
  </div>
</aside>

<nav class="swarm-bar" aria-label={t('swarm.profiles')}>
  <SearchableDropdown
    id="swarm-picker"
    value={shown.value}
    options={entries}
    placeholder={shown.label}
    ariaLabel={t('swarm.sidebar.picker', { current: shown.label })}
    triggerClass="swarm-bar__picker"
    panelClass="swarm-bar__panel"
    panelMaxHeight={PICKER_MAX_HEIGHT}
    footerActionLabel={moreLabel}
    onFooterAction={loadMore}
    onValueChange={choose}
  />
  {@render newSwarmButton()}
</nav>

<ContextMenu {menu} onClose={() => (menu = null)} />
