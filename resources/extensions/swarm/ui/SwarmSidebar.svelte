<script>
  import {
    participantTotal,
    profileTooltip,
    runTitle,
    runTooltip,
  } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';

  // The page's navigation: "New run", the Swarms (profiles) and the Runs.
  let { model } = $props();
  // The goal form is the page's home: "New run" is its navigation entry.
  const startVisible = $derived(!model.editor && !model.selectedSwarm);
</script>

<aside class="secondary-pane">
  <div class="secondary-pane__header">
    <span class="secondary-pane__title">{t('swarm.profiles')}</span>
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('swarm.newProfile')}
      tooltip={t('swarm.newProfile')}
      loading={model.pending === 'profile'}
      onClick={() => model.navigate(() => model.openProfile())}
      ><svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
        <path d="M7 1.5v11M1.5 7h11" />
      </svg></Button
    >
  </div>
  <div class="secondary-pane__pinned">
    <button
      type="button"
      class="secondary-list__item secondary-pane__pinned-item swarm-new-run"
      class:active={startVisible}
      aria-current={startVisible ? 'page' : undefined}
      onclick={() => model.newSwarm()}>{t('swarm.newRun')}</button
    >
  </div>
  <div class="secondary-pane__scroll swarm-pane-scroll">
    <nav class="secondary-list" aria-label={t('swarm.profiles')}>
      {#each model.profiles as profile (profile.id)}
        <button
          class="secondary-list__item"
          class:active={model.editor?.id === profile.id}
          aria-current={model.editor?.id === profile.id ? 'page' : undefined}
          use:tooltip={profileTooltip(profile)}
          onclick={() => model.navigate(() => model.openProfile(profile))}
        >
          <span class="sidebar-title"
            >{profile.name} ({participantTotal(profile)})</span
          >
        </button>
      {:else}<EmptyState
          density="compact"
          title={t('swarm.noProfiles')}
          description={t('swarm.noProfilesHelp')}
        />{/each}
      {#if model.profilesCursor}<Button
          variant="tertiary"
          onClick={model.loadMoreProfiles}>{t('swarm.profiles.more')}</Button
        >{/if}
    </nav>
    {#each model.runGroups as group (group.id)}
      <section class="run-group" aria-labelledby={`swarm-runs-${group.id}`}>
        <h3 class="secondary-list__group" id={`swarm-runs-${group.id}`}>
          {group.label}
        </h3>
        <nav class="secondary-list" aria-label={group.label}>
          {#each group.entries as swarm (swarm.id)}
            <button
              class="secondary-list__item"
              class:active={!model.editor &&
                model.selectedSwarm?.id === swarm.id}
              use:tooltip={runTooltip(swarm)}
              onclick={() => model.navigate(() => model.openSwarm(swarm.id))}
            >
              <span class="sidebar-title">{runTitle(swarm)}</span>
            </button>
          {:else}
            <p class="run-group__empty">{group.empty}</p>
          {/each}
        </nav>
      </section>
    {/each}
    {#if model.swarmsCursor}<Button
        variant="tertiary"
        onClick={model.loadMoreSwarms}>{t('swarm.swarms.more')}</Button
      >{/if}
  </div>
</aside>
