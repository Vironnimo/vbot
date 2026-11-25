<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import SessionEmbed from '@vbot/extension-sdk/ui/SessionEmbed.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import {
    memberLabel,
    runOriginLabel,
    runStateLabel,
    timestamp,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  let replyText = $state('');

  const run = $derived(model.selectedRun);
  const canReply = $derived(
    Boolean(run && ['needs_attention', 'failed'].includes(run.state)),
  );
  const canCancel = $derived(
    Boolean(
      run && ['queued', 'running', 'needs_attention'].includes(run.state),
    ),
  );

  async function sendReply() {
    const text = replyText.trim();
    if (!run || !text) return;
    const result = await model.mutate('job_run.reply', {
      run_id: run.id,
      text,
    });
    if (result) replyText = '';
  }

  async function cancelRun() {
    if (!run) return;
    await model.mutate('job_run.cancel', { run_id: run.id });
  }
</script>

{#if !run}
  <p class="factory-empty-state">{t('factory.run.missing')}</p>
{:else}
  <section class="factory-run-detail" aria-label={run.job_name || run.id}>
    <header class="factory-run-header">
      <Button
        variant="tertiary"
        onClick={() =>
          onNavigate(`/factory/${encodeURIComponent(run.factory_id)}`)}
        >{t('factory.run.back')}</Button
      >
      <div class="factory-run-title">
        <h2>{run.job_name || run.job_id || run.id}</h2>
        <span class="factory-badge factory-badge--{run.state}"
          >{runStateLabel(run.state)}</span
        >
        <span class="factory-subtle">{runOriginLabel(run.origin)}</span>
        <span class="factory-subtle"
          >{memberLabel(model.members, run.member_id)}</span
        >
        <span class="factory-subtle"
          >{t('factory.run.created', { time: timestamp(run.created_at) })}</span
        >
        {#if run.finished_at}
          <span class="factory-subtle"
            >{t('factory.run.finished', {
              time: timestamp(run.finished_at),
            })}</span
          >
        {/if}
      </div>
      <div class="factory-run-actions">
        {#if run.session_address}
          <Button
            variant="secondary"
            onClick={() => model.client.openSession(run.session_address)}
            >{t('factory.run.openInChat')}</Button
          >
        {/if}
        {#if canCancel}
          <Button
            variant="danger"
            disabled={Boolean(model.pending)}
            onClick={cancelRun}>{t('factory.run.cancel')}</Button
          >
        {/if}
      </div>
    </header>

    <div class="factory-run-body">
      {#if run.question}
        <section class="factory-panel factory-panel--attention">
          <h3>{t('factory.run.question')}</h3>
          <MarkdownContent source={run.question} />
        </section>
      {/if}

      {#if run.reason}
        <section class="factory-panel">
          <h3>{t('factory.run.reason')}</h3>
          <p>{run.reason}</p>
        </section>
      {/if}

      {#if run.summary}
        <section class="factory-panel">
          <h3>{t('factory.run.summary')}</h3>
          <MarkdownContent source={run.summary} />
        </section>
      {/if}

      {#if canReply}
        <form
          class="factory-panel factory-form"
          onsubmit={(event) => {
            event.preventDefault();
            void sendReply();
          }}
        >
          <label for="job-run-reply">{t('factory.run.reply')}</label>
          <TextArea
            id="job-run-reply"
            rows={3}
            autoGrow
            value={replyText}
            onInput={(value) => (replyText = value)}
          />
          <div>
            <Button
              variant="primary"
              disabled={!replyText.trim() || Boolean(model.pending)}
              onClick={sendReply}>{t('factory.run.sendReply')}</Button
            >
          </div>
        </form>
      {/if}

      {#if run.prompt}
        <section class="factory-panel">
          <h3>{t('factory.run.prompt')}</h3>
          <MarkdownContent source={run.prompt} />
        </section>
      {/if}

      {#if run.input_note}
        <section class="factory-panel">
          <h3>{t('factory.run.inputNote')}</h3>
          <MarkdownContent source={run.input_note} />
        </section>
      {/if}

      {#if run.session_address && run.group_id && run.participant_id}
        <section class="factory-panel">
          <h3>{t('factory.run.session')}</h3>
          <div class="factory-session-slot">
            <SessionEmbed
              client={model.client}
              groupId={run.group_id}
              participantId={run.participant_id}
              presentation="conversation"
            />
          </div>
        </section>
      {:else if run.state === 'queued'}
        <section class="factory-panel">
          <h3>{t('factory.run.session')}</h3>
          <p class="factory-subtle">{t('factory.run.sessionPending')}</p>
        </section>
      {/if}
    </div>
  </section>
{/if}

<style>
  .factory-run-detail {
    display: grid;
    gap: 16px;
  }
  .factory-run-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }
  .factory-run-title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    flex: 1 1 320px;
  }
  .factory-run-title h2 {
    margin: 0;
    font-size: 18px;
  }
  .factory-run-actions {
    display: flex;
    gap: 8px;
  }
  .factory-run-body {
    display: grid;
    gap: 14px;
  }
  .factory-panel {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    padding: 16px;
    display: grid;
    gap: 8px;
  }
  .factory-panel--attention {
    border-color: var(--accent);
  }
  .factory-panel h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 650;
  }
  .factory-session-slot {
    height: clamp(320px, 60dvh, 840px);
  }
</style>
