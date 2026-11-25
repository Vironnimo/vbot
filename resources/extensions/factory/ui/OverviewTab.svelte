<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import FactoryChat from './FactoryChat.svelte';
  import {
    memberLabel,
    runStateLabel,
    summaryPreview,
    timestamp,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  let replies = $state({});

  const replyDraft = (runId) => replies[runId] ?? '';
  const setReply = (runId, value) => {
    replies = { ...replies, [runId]: value };
  };

  async function sendReply(run) {
    const text = replyDraft(run.id).trim();
    if (!text) return;
    const result = await model.mutate('job_run.reply', {
      run_id: run.id,
      text,
    });
    if (result) {
      setReply(run.id, '');
    }
  }

  async function cancelRun(run) {
    await model.mutate('job_run.cancel', { run_id: run.id });
  }

  async function dismissRun(run) {
    await model.mutate('job_run.dismiss', { run_id: run.id });
  }
</script>

<div class="factory-overview">
  <div class="factory-overview-chat">
    {#key model.factory.project_id}
      <FactoryChat
        {model}
        projectId={model.factory.project_id}
        inactive={model.factory.state !== 'active'}
        onSent={() => model.refresh()}
      />
    {/key}
  </div>

  <div class="factory-dashboard">
    <section
      class="factory-dash-panel"
      aria-label={t('factory.dashboard.needsAttention')}
    >
      <header class="factory-dash-header">
        <h2>{t('factory.dashboard.needsAttention')}</h2>
        <span class="factory-count">{model.needsAttention.length}</span>
      </header>
      {#if !model.needsAttention.length}
        <p class="factory-empty">{t('factory.dashboard.noNeedsAttention')}</p>
      {:else}
        <ul class="factory-dash-list">
          {#each model.needsAttention as run (run.id)}
            <li class="factory-dash-item factory-dash-item--attention">
              <div class="factory-dash-row">
                <strong>{run.job_name || run.job_id}</strong>
                <span class="factory-badge factory-badge--{run.state}"
                  >{runStateLabel(run.state)}</span
                >
                <span class="factory-subtle">{timestamp(run.created_at)}</span>
                <Button
                  variant="tertiary"
                  onClick={() =>
                    onNavigate(`/job-run/${encodeURIComponent(run.id)}`)}
                  >{t('factory.dashboard.inspect')}</Button
                >
                {#if run.state === 'failed'}
                  <Button
                    variant="tertiary"
                    disabled={Boolean(model.pending)}
                    onClick={() => dismissRun(run)}
                    >{t('factory.dashboard.dismiss')}</Button
                  >
                {/if}
              </div>
              {#if run.question || run.reason}
                <p class="factory-dash-detail">{run.question || run.reason}</p>
              {/if}
              <form
                class="factory-inline-reply"
                onsubmit={(event) => {
                  event.preventDefault();
                  void sendReply(run);
                }}
              >
                <TextField
                  id={`reply-${run.id}`}
                  ariaLabel={t('factory.dashboard.replyPlaceholder')}
                  placeholder={t('factory.dashboard.replyPlaceholder')}
                  value={replyDraft(run.id)}
                  onInput={(value) => setReply(run.id, value)}
                />
                <Button
                  variant="primary"
                  disabled={!replyDraft(run.id).trim() ||
                    Boolean(model.pending)}
                  onClick={() => sendReply(run)}
                  >{t('factory.dashboard.reply')}</Button
                >
              </form>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section
      class="factory-dash-panel"
      aria-label={t('factory.dashboard.running')}
    >
      <header class="factory-dash-header">
        <h2>{t('factory.dashboard.running')}</h2>
        <span class="factory-count">{model.running.length}</span>
      </header>
      {#if !model.running.length}
        <p class="factory-empty">{t('factory.dashboard.noRunning')}</p>
      {:else}
        <ul class="factory-dash-list">
          {#each model.running as run (run.id)}
            <li class="factory-dash-item">
              <div class="factory-dash-row">
                <strong>{run.job_name || run.job_id}</strong>
                <span class="factory-badge factory-badge--{run.state}"
                  >{runStateLabel(run.state)}</span
                >
                <span class="factory-subtle"
                  >{memberLabel(model.members, run.member_id)}</span
                >
                <span class="factory-subtle">{timestamp(run.created_at)}</span>
                <div class="factory-dash-actions">
                  <Button
                    variant="tertiary"
                    onClick={() =>
                      onNavigate(`/job-run/${encodeURIComponent(run.id)}`)}
                    >{t('factory.dashboard.inspect')}</Button
                  >
                  <Button
                    variant="danger"
                    disabled={Boolean(model.pending)}
                    onClick={() => cancelRun(run)}
                    >{t('factory.dashboard.cancel')}</Button
                  >
                </div>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section
      class="factory-dash-panel"
      aria-label={t('factory.dashboard.upcoming')}
    >
      <header class="factory-dash-header">
        <h2>{t('factory.dashboard.upcoming')}</h2>
        <span class="factory-count">{model.upcoming.length}</span>
      </header>
      {#if !model.upcoming.length}
        <p class="factory-empty">{t('factory.dashboard.noUpcoming')}</p>
      {:else}
        <ul class="factory-dash-list">
          {#each model.upcoming as item (`${item.job_id}:${item.trigger_id}`)}
            <li class="factory-dash-item">
              <div class="factory-dash-row">
                <strong>{item.job_name}</strong>
                <span class="factory-mono">{item.schedule}</span>
                <span class="factory-subtle">{timestamp(item.next_fire)}</span>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section
      class="factory-dash-panel"
      aria-label={t('factory.dashboard.recent')}
    >
      <header class="factory-dash-header">
        <h2>{t('factory.dashboard.recent')}</h2>
        <span class="factory-count">{model.recentRuns.length}</span>
      </header>
      {#if !model.recentRuns.length}
        <p class="factory-empty">{t('factory.dashboard.noRecent')}</p>
      {:else}
        <ul class="factory-dash-list">
          {#each model.recentRuns as run (run.id)}
            <li class="factory-dash-item">
              <div class="factory-dash-row">
                <Button
                  variant="tertiary"
                  onClick={() =>
                    onNavigate(`/job-run/${encodeURIComponent(run.id)}`)}
                  >{run.job_name || run.job_id}</Button
                >
                <span class="factory-badge factory-badge--{run.state}"
                  >{runStateLabel(run.state)}</span
                >
                <span class="factory-subtle">{timestamp(run.created_at)}</span>
              </div>
              {#if run.summary || run.reason}
                <p class="factory-dash-detail factory-dash-detail--clamped">
                  {summaryPreview(run.summary || run.reason)}
                </p>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  </div>
</div>

<style>
  .factory-overview {
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
    gap: 20px;
    align-items: start;
  }
  @media (max-width: 900px) {
    .factory-overview {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .factory-dashboard {
    display: grid;
    gap: 16px;
    min-width: 0;
  }
  .factory-dash-panel {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    padding: 16px;
    min-width: 0;
  }
  .factory-dash-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .factory-dash-header h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 650;
  }
  .factory-empty {
    margin: 0;
    color: var(--text-med);
    font-size: 13px;
  }
  .factory-dash-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 10px;
  }
  .factory-dash-item {
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-2);
    padding: 10px 12px;
    display: grid;
    gap: 8px;
  }
  .factory-dash-item--attention {
    border-color: var(--accent);
  }
  .factory-dash-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .factory-dash-actions {
    margin-left: auto;
    display: flex;
    gap: 6px;
  }
  .factory-dash-detail {
    margin: 0;
    color: var(--text-med);
    font-size: 13px;
    line-height: 1.45;
  }
  .factory-dash-detail--clamped {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .factory-inline-reply {
    display: flex;
    gap: 8px;
    align-items: center;
  }
</style>
