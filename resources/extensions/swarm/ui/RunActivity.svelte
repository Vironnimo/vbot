<script>
  import { sessionReferenceRules } from './referenceLinks.js';
  import { resumableParticipantState } from './pagePresentation.js';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import SessionEmbed from '@vbot/extension-sdk/ui/SessionEmbed.svelte';
  import EmptyState from '@vbot/extension-sdk/ui/EmptyState.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import ParticipantChip from './ParticipantChip.svelte';
  import './runActivity.css';

  let { model } = $props();

  const headingId = $props.id();

  const participants = $derived(model.selectedSwarm.participants ?? []);
  const inspected = $derived(model.selectedParticipant);
  const references = $derived(sessionReferenceRules(model.selectedSwarm));
  const canResumeInspected = $derived(
    model.canResume &&
      inspected &&
      !inspected.run_active &&
      resumableParticipantState(inspected.state),
  );
  const stop = $derived(stopNotice(inspected?.stop_reason));

  let resuming = $state(null);

  async function resumeInspected() {
    const participantId = inspected.id;
    resuming = participantId;
    try {
      await model.lifecycle('resume', participantId);
    } finally {
      resuming = null;
    }
  }

  function stopNotice(reason) {
    switch (reason) {
      case undefined:
      case null:
        return null;
      case 'network':
        return { variant: 'error', text: t('swarm.activity.stop.network') };
      case 'timeout':
        return { variant: 'error', text: t('swarm.activity.stop.timeout') };
      case 'provider':
        return { variant: 'error', text: t('swarm.activity.stop.provider') };
      case 'internal':
        return { variant: 'error', text: t('swarm.activity.stop.internal') };
      case 'shutdown':
        return { variant: 'warn', text: t('swarm.activity.stop.shutdown') };
      case 'process_restart':
        return { variant: 'warn', text: t('swarm.activity.stop.restart') };
      case 'failed':
        return { variant: 'error', text: t('swarm.activity.stop.failed') };
      case 'not_started':
        return { variant: 'error', text: t('swarm.activity.stop.notStarted') };
      default:
        return { variant: 'warn', text: t('swarm.activity.stop.interrupted') };
    }
  }
</script>

<div
  class="panel activity"
  id="swarm-run-panel-participants"
  role="tabpanel"
  aria-labelledby="swarm-run-tab-participants"
>
  <div
    class="participants participant-row"
    role="group"
    aria-label={t('swarm.participants')}
  >
    {#each participants as participant (participant.id)}
      <ParticipantChip
        {participant}
        selected={inspected?.id === participant.id}
        onInspect={model.inspectParticipant}
      />
    {/each}
  </div>
  {#if inspected}
    <article class="activity-view" aria-labelledby={headingId}>
      <header class="activity-head" id={headingId + '-header'}>
        <ParticipantAvatar id={inspected.id} name={inspected.display_name} />
        <h3 class="activity-head__name" id={headingId}>
          {inspected.display_name}
        </h3>
        {#if canResumeInspected}
          <Button
            variant="secondary"
            class="activity-head__resume"
            loading={resuming === inspected.id}
            disabled={Boolean(model.pending)}
            onClick={resumeInspected}>{t('swarm.resumeParticipant')}</Button
          >
        {/if}
        <Button
          variant="tertiary"
          icon
          class="activity-head__close"
          ariaLabel={t('common.close')}
          tooltip={t('common.close')}
          onClick={model.closeParticipant}><SwarmIcon kind="close" /></Button
        >
      </header>
      {#if stop}
        <Banner variant={stop.variant} class="activity-stop">{stop.text}</Banner
        >
      {/if}
      <div class="activity-session">
        <SessionEmbed
          client={model.client}
          groupId={model.selectedSwarm.id}
          participantId={inspected.id}
          presentation="full"
          clipBelow={headingId + '-header'}
          {references}
          onReference={model.board.openReference}
        />
      </div>
    </article>
  {:else if participants.length}
    <EmptyState
      class="activity-empty"
      title={t('swarm.activity.empty')}
      description={t('swarm.activity.emptyHint')}
    />
  {/if}
</div>
