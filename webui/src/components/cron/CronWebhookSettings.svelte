<script>
  // The Webhook section of a webhook job's form: the URL its callers send
  // requests to, the secret those requests prove, and the latest request.
  // The URL and the secret exist once the job is saved as a webhook job.
  import { t } from '$lib/i18n.js';
  import { webhookReachText, webhookRequestsText } from './presentation.js';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  let { editor, systemTimezone } = $props();

  let webhook = $derived(
    editor.isCreating ? null : (editor.selectedJob?.webhook ?? null),
  );
  let secret = $derived(editor.webhookSecret);
</script>

<section class="s-section" aria-labelledby="cron-section-webhook">
  <header class="s-section__head">
    <h3 class="s-section__title" id="cron-section-webhook">
      {t('cron.sections.webhook')}
    </h3>
  </header>
  <p class="s-section__desc">
    {t('cron.sections.webhookSubtitle')}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      {#if !webhook}
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-desc" data-testid="cron-webhook-unsaved">
              {t('cron.webhook.unsaved')}
            </div>
          </div>
        </div>
      {:else}
        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label">{t('cron.webhook.url')}</span>
            {#if webhookReachText(webhook.reach)}
              <div class="s-row-desc" data-testid="cron-webhook-reach">
                {webhookReachText(webhook.reach)}
              </div>
            {/if}
          </div>
          <div class="s-row-control cron-webhook-control">
            <code class="cron-webhook-value" data-testid="cron-webhook-url"
              >{webhook.url ??
                t('cron.webhook.urlPath', { path: webhook.path })}</code
            >
            {#if webhook.url}
              <CopyButton
                text={webhook.url}
                label={t('cron.webhook.copyUrl')}
              />
            {/if}
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label cron-label-with-hint">
              {t('cron.webhook.secret')}
              <InfoHint text={t('cron.webhook.secretHelp')} />
            </span>
          </div>
          <div class="s-row-control cron-webhook-control">
            <code class="cron-webhook-value" data-testid="cron-webhook-secret"
              >{secret || '••••••••••••••••'}</code
            >
            {#if secret}
              <CopyButton text={secret} label={t('cron.webhook.copySecret')} />
            {/if}
            <Button
              variant="secondary"
              disabled={editor.webhookSecretBusy}
              onClick={editor.toggleWebhookSecret}
            >
              {secret
                ? t('cron.webhook.hideSecret')
                : t('cron.webhook.showSecret')}
            </Button>
            <Button
              variant="secondary"
              disabled={editor.webhookSecretBusy}
              onClick={editor.requestRotateSecret}
            >
              {t('cron.webhook.rotate')}
            </Button>
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label">{t('cron.webhook.lastRequest')}</span>
          </div>
          <div class="s-row-control">
            <span
              class="cron-webhook-requests"
              data-testid="cron-webhook-requests"
              >{webhookRequestsText(editor.selectedJob, systemTimezone)}</span
            >
          </div>
        </div>
      {/if}
    </div>
  </div>
</section>
