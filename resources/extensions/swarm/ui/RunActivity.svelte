<script>
  import { resumableParticipantState } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import ContextUsageRing from '../../../../webui/src/components/chat/ContextUsageRing.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import ParticipantChip from './ParticipantChip.svelte';
  import './runActivity.css';

  // The Activity tab: the Run's participants, then the inspected
  // participant's activity under a header that stays in view while it
  // scrolls: name, context use, Resume participant and close. Below the
  // timeline, why the participant stopped when the timeline does not say.
  // The page model supplies the Run, the participant's resume and the
  // Board's content links.
  let { activity, model } = $props();

  const headingId = $props.id();

  const participants = $derived(model.selectedSwarm.participants ?? []);
  const inspected = $derived(activity.selectedParticipant);
  const canResumeInspected = $derived(
    model.canResume &&
      inspected &&
      !inspected.run_active &&
      resumableParticipantState(inspected.state),
  );
  const stop = $derived(stopNotice(activity.stopReason));

  let resuming = $state(false);
  let view = $state(null);
  // The participant whose activity was last brought into view at its end.
  let revealed = null;

  async function resumeInspected() {
    resuming = true;
    try {
      await model.lifecycle('resume', inspected.id);
    } finally {
      resuming = false;
    }
  }

  // The footer text for a stop reason of the Activity model.
  function stopNotice(reason) {
    switch (reason) {
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

  // Scrolls the nearest scrolling ancestor of `element` to its end.
  function scrollToEnd(element) {
    for (let node = element.parentElement; node; node = node.parentElement) {
      if (
        node.scrollHeight > node.clientHeight &&
        /auto|scroll/.test(getComputedStyle(node).overflowY)
      ) {
        node.scrollTop = node.scrollHeight;
        return;
      }
    }
  }

  // A participant's activity opens at its newest entries.
  $effect(() => {
    const id = activity.history?.participant.id;
    if (!inspected) revealed = null;
    if (!id || !view || revealed === id) return;
    revealed = id;
    scrollToEnd(view);
  });
</script>

<div
  class="panel activity"
  id="swarm-run-panel-participants"
  role="tabpanel"
  aria-labelledby="swarm-run-tab-participants"
>
  <div
    class="participant-row activity-participants"
    role="group"
    aria-label={t('swarm.participants')}
  >
    {#each participants as participant (participant.id)}
      <ParticipantChip
        {participant}
        selected={inspected?.id === participant.id}
        onInspect={activity.inspectParticipant}
      />
    {/each}
  </div>
  {#if inspected}
    <article class="activity-view" aria-labelledby={headingId} bind:this={view}>
      <header class="activity-head">
        <ParticipantAvatar id={inspected.id} name={inspected.display_name} />
        <h3 class="activity-head__name" id={headingId}>
          {inspected.display_name}
        </h3>
        <div class="activity-head__context">
          <ContextUsageRing
            contextUsage={activity.history?.data.context_usage}
            contextWindow={activity.contextWindow}
            usage={activity.history?.data.usage}
            sessionUsage={activity.history?.data.session_usage}
            showFigures
          />
        </div>
        {#if canResumeInspected}
          <Button
            variant="secondary"
            class="activity-head__resume"
            loading={resuming}
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
          onClick={activity.leaveActivity}><SwarmIcon kind="close" /></Button
        >
      </header>
      {#if activity.history}
        {#if activity.history.data.has_more && activity.history.data.next_before}
          <Button
            variant="secondary"
            class="activity-older"
            disabled={activity.historyLoading}
            onClick={activity.loadEarlierActivity}
          >
            {t('swarm.loadOlderMessages')}
          </Button>
        {/if}
        <div class="history" use:model.board.contentLinks>
          {#each activity.activityTimeline as item (item.id)}
            {#if item.type === 'assistant_run'}<ChatAssistantRun
                {item}
                isReasoningOpen={activity.isReasoningOpen}
                onReasoningOpenChange={activity.setReasoningOpen}
                onCancelToolCall={activity.cancelToolCall}
                agentName={inspected.display_name}
              />{:else}<ChatTimelineEntry
                {item}
                agentName={inspected.display_name}
                messageEditingDisabled
              />{/if}
          {:else}<EmptyState
              density="compact"
              title={t('swarm.noActivity')}
            />{/each}
        </div>
        {#if stop}
          <Banner variant={stop.variant} class="activity-stop"
            >{stop.text}</Banner
          >
        {/if}
      {:else if activity.historyLoading}
        <Banner variant="neutral" role="status">{t('common.loading')}</Banner>
      {/if}
    </article>
  {:else if participants.length}
    <EmptyState
      class="activity-empty"
      title={t('swarm.activity.empty')}
      description={t('swarm.activity.emptyHint')}
    />
  {/if}
</div>
