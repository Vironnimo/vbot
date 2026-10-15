<script>
  // The displayed Session's goal above the composer: the objective, its
  // status, the automatic turns used, and buttons that send `/goal pause`,
  // `/goal resume` or `/goal clear` like typed commands. The goal comes from
  // the Session's `session.get` row (`view/sessionSettings.svelte.js`).
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';

  const noop = () => {};

  let { goal, onCommand = noop } = $props();

  const STATUSES = new Set(['active', 'blocked', 'paused', 'complete']);
  const PAUSE_REASONS = new Set([
    'limit',
    'run_failed',
    'stop',
    'restart',
    'takeover',
    'agent',
  ]);

  let status = $derived(STATUSES.has(goal?.status) ? goal.status : 'active');
  let reason = $derived(
    status === 'paused' && PAUSE_REASONS.has(goal?.pause_reason)
      ? t(`chat.goal.pauseReason.${goal.pause_reason}`)
      : '',
  );
  let turns = $derived(
    Number.isFinite(goal?.turns) && Number.isFinite(goal?.max_turns)
      ? t('chat.goal.turns', { turns: goal.turns, max: goal.max_turns })
      : '',
  );
  let running = $derived(status === 'active' || status === 'blocked');
</script>

{#if goal?.objective}
  <div class="composer-goal" role="status" aria-label={t('chat.goal.label')}>
    <span class="composer-goal__status composer-goal__status--{status}">
      {t(`chat.goal.status.${status}`)}
    </span>
    <span class="composer-goal__objective" use:tooltip={goal.objective}>
      {goal.objective}
    </span>
    {#if reason}
      <span class="composer-goal__meta">{reason}</span>
    {/if}
    {#if turns}
      <span class="composer-goal__meta">{turns}</span>
    {/if}
    <span class="composer-goal__actions">
      <Button
        variant="tertiary"
        class="composer-goal__action"
        onClick={() => onCommand(running ? 'pause' : 'resume')}
      >
        {running ? t('chat.goal.pause') : t('chat.goal.resume')}
      </Button>
      <Button
        variant="tertiary"
        class="composer-goal__action"
        onClick={() => onCommand('clear')}
      >
        {t('chat.goal.clear')}
      </Button>
    </span>
  </div>
{/if}

<style>
  .composer-goal {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    margin-bottom: 6px;
    padding: 5px 10px;
    border: 1px solid var(--border-2);
    border-radius: 8px;
    font-size: var(--fs-label-sm);
    line-height: 1.4;
  }

  .composer-goal__status {
    flex-shrink: 0;
    font-weight: 600;
    color: var(--text-med);
  }

  .composer-goal__status--active {
    color: var(--accent);
  }

  .composer-goal__status--blocked {
    color: var(--amber);
  }

  .composer-goal__objective {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .composer-goal__meta {
    flex-shrink: 0;
    color: var(--text-lo);
  }

  .composer-goal__actions {
    display: flex;
    flex-shrink: 0;
    gap: 2px;
  }

  :global(.composer-goal__action) {
    padding: 2px 6px;
    font-size: var(--fs-label-sm);
  }

  @media (max-width: 640px) {
    .composer-goal {
      flex-wrap: wrap;
    }

    .composer-goal__objective {
      flex-basis: 100%;
      order: -1;
      white-space: normal;
    }
  }
</style>
