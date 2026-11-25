<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Checkbox from '@vbot/extension-sdk/ui/Checkbox.svelte';
  import ConfirmDialog from '@vbot/extension-sdk/ui/ConfirmDialog.svelte';
  import CopyButton from '@vbot/extension-sdk/ui/CopyButton.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import { failureText, timestamp } from './presentation.js';

  let {
    trigger = $bindable(),
    jobId = '',
    jobEnabled = true,
    factoryId = '',
    saved = null,
    model,
    onRemove,
  } = $props();

  let secret = $state('');
  let secretError = $state('');
  let secretBusy = $state(false);
  let rotateOpen = $state(false);

  const prefix = $derived(`factory-trigger-${trigger.id}`);
  const webhookUrl = $derived(
    saved?.type === 'webhook' ? saved?.webhook_url || '' : '',
  );
  const typeOptions = [
    { value: 'schedule', label: t('factory.trigger.schedule') },
    { value: 'webhook', label: t('factory.trigger.webhook') },
  ];

  function selectType(value) {
    trigger.type = value;
    if (value === 'schedule' && !trigger.schedule) {
      trigger.schedule = '0 9 * * 1-5';
    }
  }

  async function readSecret(rotate = false) {
    if (!jobId || !factoryId) return;
    secretBusy = true;
    secretError = '';
    try {
      const result = await model.call('job.secret', {
        factory_id: factoryId,
        job_id: jobId,
        trigger_id: trigger.id,
        ...(rotate ? { rotate: true } : {}),
      });
      secret = result.secret ?? '';
      rotateOpen = false;
    } catch (cause) {
      secretError = failureText(cause);
    } finally {
      secretBusy = false;
    }
  }
</script>

<article class="factory-trigger" data-trigger-id={trigger.id}>
  <header class="factory-trigger-header">
    <Checkbox
      id={`${prefix}-enabled`}
      checked={trigger.enabled}
      onChange={(value) => (trigger.enabled = value)}
      >{t('factory.trigger.enabled')}</Checkbox
    >
    <Button
      variant="tertiary"
      ariaLabel={t('factory.trigger.remove')}
      onClick={onRemove}>{t('factory.trigger.remove')}</Button
    >
  </header>
  <div class="factory-fields">
    <div class="factory-field">
      <label id={`${prefix}-type-label`} for={`${prefix}-type`}
        >{t('factory.trigger.type')}</label
      >
      <Dropdown
        id={`${prefix}-type`}
        ariaLabelledby={`${prefix}-type-label`}
        value={trigger.type}
        options={typeOptions}
        onOpenChange={model.pickerOpen}
        onValueChange={selectType}
      />
    </div>
    {#if trigger.type === 'schedule'}
      <div class="factory-field">
        <label for={`${prefix}-schedule`}>{t('factory.trigger.when')}</label>
        <TextField
          id={`${prefix}-schedule`}
          maxlength="200"
          value={trigger.schedule}
          invalid={!trigger.schedule?.trim()}
          aria-describedby={`${prefix}-schedule-help`}
          onInput={(value) => (trigger.schedule = value)}
        />
        <p id={`${prefix}-schedule-help`} class="factory-help">
          {t('factory.trigger.whenHelp')}
        </p>
      </div>
    {/if}
    <div class="factory-field">
      <label for={`${prefix}-title`}>{t('factory.trigger.title')}</label>
      <TextField
        id={`${prefix}-title`}
        maxlength="200"
        value={trigger.title}
        onInput={(value) => (trigger.title = value)}
      />
    </div>
  </div>

  {#if trigger.type === 'webhook'}
    <section class="factory-webhook" aria-label={t('factory.trigger.webhook')}>
      {#if webhookUrl}
        <div class="factory-webhook-row">
          <span class="factory-subtle">{t('factory.trigger.url')}</span>
          <span class="factory-mono" data-webhook-url>{webhookUrl}</span>
          <CopyButton text={webhookUrl} />
        </div>
        <div class="factory-webhook-row">
          <span class="factory-subtle">{t('factory.trigger.secret')}</span>
          {#if secret}
            <span class="factory-mono" data-webhook-secret>{secret}</span>
            <CopyButton text={secret} />
          {:else}
            <Button
              variant="tertiary"
              loading={secretBusy}
              onClick={() => readSecret()}
              >{t('factory.trigger.showSecret')}</Button
            >
          {/if}
          <Button
            variant="tertiary"
            disabled={secretBusy}
            onClick={() => (rotateOpen = true)}
            >{t('factory.trigger.rotate')}</Button
          >
        </div>
        <p class="factory-help">{t('factory.trigger.webhookHelp')}</p>
      {:else}
        <p class="factory-help">{t('factory.trigger.saveForUrl')}</p>
      {/if}
      {#if secretError}
        <Banner variant="error" role="alert">{secretError}</Banner>
      {/if}
    </section>
  {/if}

  {#if saved}
    <p class="factory-help" data-trigger-status>
      {#if saved.next_fire}
        {t('factory.trigger.next', { time: timestamp(saved.next_fire) })}
      {/if}
      {#if saved.last_fire}
        {t('factory.trigger.last', { time: timestamp(saved.last_fire) })}
      {/if}
      {#if !trigger.enabled || !jobEnabled}
        {t('factory.trigger.paused')}
      {/if}
    </p>
    {#if saved.last_error}
      <p class="factory-field-error" role="status">{saved.last_error}</p>
    {/if}
  {/if}
</article>

{#if rotateOpen}
  <ConfirmDialog
    title={t('factory.trigger.rotateTitle')}
    body={t('factory.trigger.rotateHelp')}
    confirmLabel={t('factory.trigger.rotate')}
    busy={secretBusy}
    error={secretError}
    onConfirm={() => readSecret(true)}
    onCancel={() => (rotateOpen = false)}
  />
{/if}

<style>
  .factory-trigger {
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface-2);
    padding: 14px;
    display: grid;
    gap: 12px;
    min-width: 0;
  }
  .factory-trigger + :global(.factory-trigger) {
    margin-top: 10px;
  }
  .factory-trigger-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .factory-webhook {
    display: grid;
    gap: 8px;
  }
  .factory-webhook-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    min-width: 0;
  }
  .factory-webhook-row .factory-subtle {
    min-width: 64px;
  }
  .factory-field-error {
    margin: 0;
    color: var(--red);
    font-size: 12px;
    line-height: 1.5;
  }
</style>
