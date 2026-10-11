<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ContextMenu from '../../../../webui/src/components/ui/ContextMenu.svelte';
  import { contextMenuAnchor } from '../../../../webui/src/components/ui/contextMenu.js';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import { failureText } from './failureText.js';
  import {
    aroundSlot,
    listParts,
    runStateLabel,
    runStateVariant,
    runSummary,
    runTitle,
    SLOT,
  } from './runPresentation.js';
  import SwarmIcon from './SwarmIcon.svelte';
  import RunBoard from './RunBoard.svelte';
  import RunActivity from './RunActivity.svelte';
  import RunUsage from './RunUsage.svelte';
  import WikiPanel from './WikiPanel.svelte';
  import './runView.css';

  // The selected Run: its header (title, summary, state, the one lifecycle
  // action and the Run's menu), the participants that need attention, the
  // tabs and the shown tab's panel. The view is the page's scroller; the
  // tabs stick to its top edge, and `--swarm-run-sticky-top` tells the
  // panels how much of the scroller they cover. The Wiki stays mounted
  // across tab changes.
  let { model } = $props();

  const run = $derived(model.selectedRun);
  const title = $derived(runTitle(run));
  const summary = $derived(runSummary(run, model.now));
  const state = $derived(model.selectedSwarm.state);

  // Participants whose work stopped with an error or was interrupted.
  const failed = $derived(
    (model.selectedSwarm.participants ?? []).filter(
      (participant) =>
        !participant.run_active &&
        ['failed', 'interrupted'].includes(participant.state),
    ),
  );
  // The notice names them in the locale's list words, each name a link to
  // the participant's Activity; Resume's note follows as a second sentence.
  const failedText = $derived(
    aroundSlot(t('swarm.participants.failed', { names: SLOT })),
  );
  const failedNames = $derived(listParts(failed.length));
  const resumeNote = $derived(
    model.canResume ? ` ${t('swarm.participants.failedResume')}` : '',
  );

  // Resume is the obvious next step of a Run that needs the user or was
  // interrupted; otherwise it stays a secondary action.
  const resumeFirst = $derived(
    ['needs_attention', 'interrupted'].includes(state),
  );
  const stopping = $derived(model.pending === 'stop' || state === 'stopping');

  let menu = $state(null);

  let tabsHeight = $state(0);

  let titleElement = $state(null);

  // A Run started from the start form takes focus on its title.
  $effect(() => {
    if (titleElement && model.takeFocus('run', model.selectedSwarm.id))
      titleElement.focus();
  });

  function openMenu(event) {
    menu = { ...contextMenuAnchor(event), ...model.runMenu(run) };
  }

  function inspect(participant) {
    void Promise.resolve(
      model.navigate(() => model.activity.inspectParticipant(participant)),
    ).catch((cause) => {
      if (!model.disposed) model.error = failureText(cause);
    });
  }
</script>

{#snippet resumeButton(variant)}<Button
    {variant}
    loading={model.pending === 'resume'}
    disabled={Boolean(model.pending)}
    onClick={() => model.lifecycle('resume')}
    ><SwarmIcon kind="play" />{model.pending === 'resume'
      ? t('swarm.resuming')
      : t('swarm.resume')}</Button
  >{/snippet}

<div
  class="swarm-run page-scroll"
  style:--swarm-run-sticky-top={`${tabsHeight}px`}
>
  <header class="swarm-head">
    <h2
      class="view-header__title swarm-head__title"
      tabindex="-1"
      bind:this={titleElement}
    >
      {title}
    </h2>
    <p class="view-header__subtitle swarm-head__summary">
      {summary.before}{#if summary.time}<time
          datetime={run.created_at}
          use:tooltip={summary.moment}>{summary.time}</time
        >{/if}{summary.after}
    </p>
    <div class="swarm-head__meta">
      <StatusChip variant={runStateVariant(state)}
        >{runStateLabel(state)}</StatusChip
      >
      {#if model.working}<Button
          variant="secondary"
          loading={stopping}
          disabled={Boolean(model.pending)}
          onClick={() => model.lifecycle('stop')}
          ><SwarmIcon kind="stop" />{stopping
            ? t('swarm.stopping')
            : t('swarm.stop')}</Button
        >{:else if model.canResume}{@render resumeButton(
          resumeFirst ? 'primary' : 'secondary',
        )}{/if}
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('swarm.run.actions', { title })}
        tooltip={t('swarm.run.more')}
        aria-haspopup="menu"
        aria-expanded={menu ? 'true' : 'false'}
        onClick={openMenu}><SwarmIcon kind="more" /></Button
      >
    </div>
  </header>
  {#if failed.length}
    <Banner variant="error" role="status" class="swarm-failed">
      <p>
        {failedText[0]}{#each failedNames as part, index (index)}{#if part.text === undefined}<button
              type="button"
              class="swarm-failed__name"
              onclick={() => inspect(failed[part.item])}
              >{failed[part.item].display_name}</button
            >{:else}{part.text}{/if}{/each}{failedText[1]}{resumeNote}
      </p>
      <!-- While others still work, the header offers Stop; Resume stays here. -->
      {#if model.working && model.canResume}{@render resumeButton(
          'secondary',
        )}{/if}
    </Banner>
  {/if}
  <div class="swarm-tabs" bind:offsetHeight={tabsHeight}>
    <TabList
      items={model.tabs}
      value={model.activeTab}
      idPrefix="swarm-run"
      ariaLabel={t('swarm.run.sections')}
      onChange={(next) => model.navigate(() => model.openTab(next))}
    />
  </div>
  {#key model.selectedSwarm.id}<WikiPanel
      bind:this={model.wikiPanel}
      swarmId={model.selectedSwarm.id}
      client={model.client}
      contentLinks={model.board.contentLinks}
      active={model.activeTab === 'wiki'}
    />{/key}
  {#if model.activeTab === 'board'}<RunBoard
      board={model.board}
      {model}
    />{:else if model.activeTab === 'participants'}<RunActivity
      activity={model.activity}
      {model}
    />{:else if model.activeTab === 'usage'}<RunUsage
      usage={model.usage}
      {model}
    />{/if}
</div>

<ContextMenu {menu} onClose={() => (menu = null)} />
