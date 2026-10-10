<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import RunBoard from './RunBoard.svelte';
  import RunActivity from './RunActivity.svelte';
  import RunUsage from './RunUsage.svelte';
  import WikiPanel from './WikiPanel.svelte';
  import './runView.css';

  // The selected Run: its header with the lifecycle actions, the tabs and
  // the shown tab's panel. The Wiki stays mounted across tab changes.
  let { model } = $props();
</script>

<header class="swarm-head view-header">
  <div class="swarm-heading view-header__intro">
    <div class="swarm-heading-title">
      <h2 class="view-header__title">
        {model.selectedSwarm.profile_snapshot?.name || t('swarm.profile')}
      </h2>
      <StatusChip variant={model.working ? 'warn' : 'neutral'}>
        {model.selectedSwarm.state}
      </StatusChip>
    </div>
  </div>
  <div class="view-header__actions actions">
    {#if model.working}<Button
        variant="danger"
        loading={model.pending === 'stop'}
        onClick={() => model.lifecycle('stop')}
        ><SwarmIcon kind="stop" />{model.pending === 'stop'
          ? t('swarm.stopping')
          : t('swarm.stop')}</Button
      >{:else if model.canResume}<Button
        variant="primary"
        loading={model.pending === 'resume'}
        disabled={!!model.pending}
        onClick={() => model.lifecycle('resume')}
        ><SwarmIcon kind="play" />{model.pending === 'resume'
          ? t('swarm.resuming')
          : t('swarm.resume')}</Button
      >{/if}<Button
      variant="danger"
      disabled={!!model.pending || model.working}
      tooltip={model.working
        ? t('swarm.deleteRun.stopFirst')
        : t('swarm.deleteRun.title')}
      onClick={() => {
        model.deleteError = '';
        model.swarmDeleteCandidate = model.selectedSwarm;
      }}
      ariaLabel={t('swarm.deleteRun.title')}
      icon><SwarmIcon kind="trash" /></Button
    ><Button
      variant="secondary"
      onClick={() => (model.profileSnapshotOpen = true)}
      icon
      ariaLabel={t('swarm.inspectProfileSnapshot')}
      tooltip={t('swarm.inspectProfileSnapshot')}
      ><SwarmIcon kind="document" /></Button
    ><Button
      variant="tertiary"
      icon
      onClick={model.openDelivery}
      ariaLabel={t('swarm.changeCommunication')}
      tooltip={t('swarm.changeCommunication')}
      ><SwarmIcon kind="settings" /></Button
    >
  </div>
</header>
<div class="swarm-tabs">
  <TabList
    items={model.tabs}
    value={model.activeTab}
    ariaLabel={t('swarm.details')}
    onChange={(next) => model.navigate(() => model.openTab(next))}
  />
</div>
{#if model.selectedSwarm.participants?.some( (participant) => ['failed', 'interrupted'].includes(participant.state) )}
  <Banner variant="error" role="alert">
    {t('swarm.participants.failed', {
      names: model.selectedSwarm.participants
        .filter((participant) =>
          ['failed', 'interrupted'].includes(participant.state),
        )
        .map((participant) => participant.display_name)
        .join(', '),
    })}
  </Banner>
{/if}
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
