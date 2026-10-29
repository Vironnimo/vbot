<script>
  // Retained notifications are independent of the job's future delivery settings.
  // This explicit operation supplies a new chat only for platform-blocked messages.
  import { onMount } from 'svelte';
  import { listChannels } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { channelPlatformLabel } from '../settings/channels/controller.svelte.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';

  let { job, onRetry, onClose } = $props();
  let channels = $state([]);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let channelId = $state('');
  let chatId = $state('');
  let threadId = $state('');
  let destroyed = false;
  const selectedChannel = $derived(
    channels.find((channel) => channel.id === channelId),
  );
  const options = $derived(
    channels.map((channel) => ({
      value: channel.id,
      label: `${channel.id} · ${channelPlatformLabel(channel.platform)}`,
    })),
  );

  onMount(() => {
    void loadChannels();
    return () => {
      destroyed = true;
    };
  });

  async function loadChannels() {
    loading = true;
    error = '';
    try {
      const result = await listChannels();
      if (destroyed) return;
      channels = (result?.channels ?? []).filter(
        (channel) => channel.agent_id === job.agent_id,
      );
    } catch (failure) {
      if (!destroyed) error = failure.message || t('cron.delivery.retryError');
    } finally {
      if (!destroyed) loading = false;
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (busy || loading || !selectedChannel || !chatId.trim()) return;
    busy = true;
    error = '';
    try {
      const accepted = await onRetry({
        kind: 'chat',
        channel_id: channelId,
        platform: selectedChannel.platform,
        platform_target: chatId.trim(),
        ...(threadId.trim() ? { thread_id: threadId.trim() } : {}),
      });
      if (destroyed) return;
      if (accepted) onClose();
      else error = t('cron.delivery.retryError');
    } finally {
      if (!destroyed) busy = false;
    }
  }
</script>

<Modal title={t('cron.delivery.chooseTarget')} closeDisabled={busy} {onClose}>
  {#snippet body()}
    <form id="cron-delivery-retry-form" onsubmit={submit}>
      <div class="modal-body">
        <p>{t('cron.delivery.rebindExplanation')}</p>
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        {#if loading}
          <p role="status">{t('common.loading')}</p>
        {:else}
          <FormField
            controlId="cron-delivery-retry-channel"
            label={t('cron.form.deliveryChannel')}
          >
            {#snippet children(field)}
              <Dropdown
                id={field.controlId}
                ariaLabelledby={field.labelId}
                value={channelId}
                {options}
                disabled={busy || options.length === 0}
                placeholder={t('cron.form.deliveryChannelPlaceholder')}
                onValueChange={(value) => {
                  channelId = value;
                  chatId = '';
                  threadId = '';
                }}
              />
            {/snippet}
          </FormField>
          {#if options.length === 0}<p>
              {t('cron.form.deliveryChannelNone')}
            </p>{/if}
          <FormField
            controlId="cron-delivery-retry-chat"
            label={t('cron.form.deliveryChat')}
            help={t('cron.form.deliveryChatHelp')}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                aria-describedby={field.describedBy}
                code
                value={chatId}
                list="cron-delivery-retry-chats"
                disabled={busy || !selectedChannel}
                onInput={(value) => (chatId = value)}
              />
              <datalist id="cron-delivery-retry-chats">
                {#each selectedChannel?.allowed_chat_ids ?? [] as id (id)}<option
                    value={id}
                  ></option>{/each}
              </datalist>
            {/snippet}
          </FormField>
          <FormField
            controlId="cron-delivery-retry-thread"
            label={t('cron.form.deliveryThread')}
            help={t('cron.form.deliveryThreadHelp')}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                aria-describedby={field.describedBy}
                code
                value={threadId}
                disabled={busy || !selectedChannel}
                onInput={(value) => (threadId = value)}
              />
            {/snippet}
          </FormField>
        {/if}
      </div>
    </form>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" disabled={busy} onClick={onClose}
      >{t('common.cancel')}</Button
    >
    {#if error && channels.length === 0}
      <Button variant="secondary" disabled={loading} onClick={loadChannels}
        >{t('common.retry')}</Button
      >
    {/if}
    <Button
      type="submit"
      form="cron-delivery-retry-form"
      variant="primary"
      loading={busy}
      disabled={loading || !selectedChannel || !chatId.trim()}
      >{t('cron.delivery.rebind')}</Button
    >
  {/snippet}
</Modal>
