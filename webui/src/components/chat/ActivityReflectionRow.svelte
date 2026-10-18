<script>
  import { tick } from 'svelte';
  import {
    reflectionChangeItems,
    reflectionElapsedLabel,
    reflectionHasChanges,
    reflectionOutcomeLabel,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ActivityStatus from './ActivityStatus.svelte';
  import {
    reflectionDotStatus,
    reflectionScopeLabel,
    reflectionStatusDetails,
    reflectionRowLabel,
  } from './activityPresentation.js';

  let {
    row,
    panelId,
    state,
    nowMs,
    onOpenReflection,
    onLoadReflectionChanges,
    onUndoReflection,
  } = $props();

  const outcomeToggleId = (row) => `${panelId}-${row.runId}-outcome`;
  const changesId = (row) => `${panelId}-${row.runId}-changes`;

  const toggleReviewChanges = (row) => {
    if (state.expandedReviews.has(row.runId)) {
      state.expandedReviews.delete(row.runId);
      state.confirmingUndo.delete(row.runId);
      return;
    }
    state.expandedReviews.add(row.runId);
    onLoadReflectionChanges(row);
  };

  // Leaving the confirmation removes its buttons; focus returns to the
  // review's summary so keyboard users keep their place.
  const leaveUndoConfirmation = async (row) => {
    state.confirmingUndo.delete(row.runId);
    await tick();
    document.getElementById(outcomeToggleId(row))?.focus();
  };

  const confirmUndo = async (row) => {
    await onUndoReflection(row);
    await leaveUndoConfirmation(row);
  };
</script>

{#snippet reflectionRow(row)}
  {@const statusTask = {
    kind: 'reflection',
    row,
    dotStatus: reflectionDotStatus(row.status),
  }}
  <div class="chat-activity__task-row chat-activity__task-row--reflection">
    <span class="chat-activity__task-copy">
      <Button
        variant="tertiary"
        class="chat-activity__task-link"
        ariaLabel={reflectionRowLabel(row)}
        disabled={!row.sessionId}
        onClick={() => row.sessionId && onOpenReflection(row)}
      >
        <span class="chat-activity__task-title">
          {reflectionScopeLabel(row)}
        </span>
      </Button>
      <span class="chat-activity__task-meta">
        <span
          class="chat-activity__task-state"
          use:tooltip={() => ({
            ...reflectionStatusDetails(row),
            placement: 'left',
          })}
        >
          <ActivityStatus dotStatus={statusTask.dotStatus} />
          {#if row.status === 'running' && reflectionElapsedLabel(row.startedAt, nowMs)}
            <span class="chat-activity__task-time">
              {reflectionElapsedLabel(row.startedAt, nowMs)}
            </span>
          {:else if row.status !== 'running' && row.outcome && !reflectionHasChanges(row.outcome)}
            <span class="chat-activity__outcome"
              >{reflectionOutcomeLabel(row.outcome)}</span
            >
          {/if}
        </span>
        {#if row.status !== 'running' && reflectionHasChanges(row.outcome)}
          {@render outcomeToggle(row)}
        {/if}
      </span>
    </span>
  </div>
  {#if row.status !== 'running' && reflectionHasChanges(row.outcome) && state.expandedReviews.has(row.runId)}
    {@render reviewChanges(row)}
  {/if}
{/snippet}

{#snippet outcomeToggle(row)}
  {@const expanded = state.expandedReviews.has(row.runId)}
  <Button
    id={outcomeToggleId(row)}
    variant="tertiary"
    class="chat-activity__outcome chat-activity__outcome-toggle"
    aria-expanded={expanded}
    aria-controls={expanded ? changesId(row) : undefined}
    onClick={() => toggleReviewChanges(row)}
  >
    {reflectionOutcomeLabel(row.outcome)}
    <svg
      class="chat-activity__disclosure"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
    >
  </Button>
{/snippet}

{#snippet reviewChanges(row)}
  <div id={changesId(row)} class="chat-activity__changes">
    {#if row.details?.changes}
      <ul class="chat-activity__change-list">
        {#each reflectionChangeItems(row.details?.changes) as item (item.key)}
          <li
            class="chat-activity__change"
            class:chat-activity__change--undone={item.undone}
          >
            <span class="chat-activity__change-head">
              <span class="chat-activity__change-kind">{item.kind}</span>
              <span
                class="chat-activity__change-place"
                class:chat-activity__change-place--mono={item.mono}
                >{item.place}</span
              >
              {#if item.undone}
                <span class="chat-activity__change-undone"
                  >{t('chat.activity.change.undone')}</span
                >
              {/if}
            </span>
            {#if item.detail}
              <span
                class="chat-activity__change-detail"
                class:chat-activity__change-detail--mono={item.mono}
                >{item.detail}</span
              >
            {/if}
          </li>
        {/each}
      </ul>
    {:else if row.details?.loading}
      <p class="chat-activity__changes-note">
        {t('chat.activity.changes.loading')}
      </p>
    {/if}
    {#if row.details?.loadError}
      <p class="chat-activity__changes-error" role="alert">
        {t('chat.activity.changes.loadError', {
          message: row.details?.loadError,
        })}
      </p>
    {/if}
    {#if !row.outcome.undone}
      {#if state.confirmingUndo.has(row.runId)}
        <div
          class="chat-activity__undo-confirm"
          role="group"
          aria-label={t('chat.activity.undoConfirm')}
        >
          <span class="chat-activity__undo-question"
            >{t('chat.activity.undoConfirm')}</span
          >
          <Button
            variant="danger"
            class="chat-activity__undo-action"
            loading={row.details?.undoing}
            onClick={() => confirmUndo(row)}
          >
            {t('chat.activity.undoConfirmAction')}
          </Button>
          <Button
            variant="tertiary"
            class="chat-activity__undo-action"
            disabled={row.details?.undoing}
            onClick={() => leaveUndoConfirmation(row)}
          >
            {t('chat.activity.undoKeep')}
          </Button>
        </div>
      {:else}
        <Button
          variant="secondary"
          class="chat-activity__undo-action chat-activity__undo"
          loading={row.details?.undoing}
          onClick={() => state.confirmingUndo.add(row.runId)}
        >
          {t('chat.activity.undo')}
        </Button>
      {/if}
    {/if}
    {#if row.details?.undoError}
      <p class="chat-activity__changes-error" role="alert">
        {row.details?.undoError}
      </p>
    {/if}
  </div>
{/snippet}

{@render reflectionRow(row)}
