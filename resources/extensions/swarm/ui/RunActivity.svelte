<script>
  import { resumableParticipantState } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import ParticipantChip from './ParticipantChip.svelte';

  // The Activity tab: the Run's participants and the inspected participant's
  // activity. The page model supplies the Run, the participant's resume and
  // the Board's content links.
  let { activity, model } = $props();
</script>

<section class="panel" role="tabpanel">
  <h3>{t('swarm.participants')}</h3>
  <div class="participants participant-row">
    {#each model.selectedSwarm.participants ?? [] as participant (participant.id)}
      <ParticipantChip
        {participant}
        selected={activity.history?.participant.id === participant.id}
        onInspect={activity.inspectParticipant}
      />
    {/each}
  </div>
  {#if activity.history}<article class="history" use:model.board.contentLinks>
      <div class="section-head">
        <h3>
          {activity.history.participant.display_name}
          {t('swarm.activity')}
        </h3>
        <div class="actions">
          <button
            class="context-usage"
            aria-label={t('chat.contextRingLabel')}
            use:tooltip={activity.activityContextTooltip}
          >
            <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"
              ><circle
                cx="9"
                cy="9"
                r="6"
                fill="none"
                stroke="var(--border-2)"
                stroke-width="2"
              /><circle
                cx="9"
                cy="9"
                r="6"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                pathLength="1"
                stroke-dasharray={`${activity.contextRatio} 1`}
                transform="rotate(-90 9 9)"
              /></svg
            >
            {t('swarm.context')}: {activity.contextTokens}
          </button>
          {#if model.canResume && activity.selectedParticipant && !activity.selectedParticipant.run_active && resumableParticipantState(activity.selectedParticipant.state)}
            <Button
              variant="primary"
              disabled={Boolean(model.pending)}
              onClick={() =>
                model.lifecycle('resume', activity.selectedParticipant.id)}
              >{t('swarm.resumeParticipant')}</Button
            >
          {/if}
          <Button
            variant="tertiary"
            icon
            ariaLabel={t('common.close')}
            tooltip={t('common.close')}
            onClick={activity.leaveActivity}><SwarmIcon kind="close" /></Button
          >
        </div>
      </div>
      {#if activity.history.data.has_more && activity.history.data.next_before}
        <Button
          variant="secondary"
          disabled={activity.historyLoading}
          onClick={activity.loadEarlierActivity}
        >
          {t('swarm.loadOlderMessages')}
        </Button>
      {/if}
      {#each activity.activityTimeline as item (item.id)}
        {#if item.type === 'assistant_run'}<ChatAssistantRun
            {item}
            isReasoningOpen={activity.isReasoningOpen}
            onReasoningOpenChange={activity.setReasoningOpen}
            onCancelToolCall={activity.cancelToolCall}
            agentName={activity.history.participant.display_name}
          />{:else}<ChatTimelineEntry
            {item}
            agentName={activity.history.participant.display_name}
            messageEditingDisabled
          />{/if}
      {:else}<EmptyState
          density="compact"
          title={t('swarm.noActivity')}
        />{/each}
    </article>{/if}
</section>
