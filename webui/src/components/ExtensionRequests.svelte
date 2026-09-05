<script>
  // Requests can arrive during Chat, so the Settings panel cannot own this surface.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensionRequests } from '$lib/api.js';
  import {
    inputFields,
    inputResponse,
    inputUrl,
  } from '$lib/extensionInputs.js';
  import { t } from '$lib/i18n.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import TextField from './ui/TextField.svelte';
  import Dropdown from './Dropdown.svelte';

  const POLL_INTERVAL_MS = 2000;
  let requests = $state([]);
  let selected = $state(null);
  let drafts = $state({});
  let error = $state('');
  let busy = $state(false);
  let stopped = false;

  onMount(() => {
    let timer;
    async function poll() {
      try {
        const result = await listExtensionRequests();
        if (stopped) return;
        requests = result.requests;
        if (selected && !requests.some((item) => item.id === selected.id))
          selected = null;
      } catch (failure) {
        if (!stopped && selected) error = failure.message;
      } finally {
        if (!stopped) timer = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  });

  function review(request) {
    selected = request;
    drafts = {};
    error = '';
  }

  async function respond(action) {
    const request = selected;
    if (!request) return;
    busy = true;
    error = '';
    try {
      const response = inputResponse(request, drafts, action);
      await extensionOperation(request.extension, request.response_operation, {
        request_id: request.id,
        response,
      });
      if (stopped) return;
      requests = requests.filter((item) => item.id !== request.id);
      if (selected?.id === request.id) {
        selected = null;
        drafts = {};
      }
    } catch (failure) {
      if (!stopped) error = failure.message;
    } finally {
      busy = false;
    }
  }
</script>

{#if requests.length}
  <Banner variant="warn" role="status">
    <span
      >{t(
        'extensions.inputWaiting',
        '{count} Extension requests need your response.',
        { count: requests.length },
      )}</span
    >
    <Button variant="primary" onClick={() => review(requests[0])}
      >{t('extensions.reviewInput', 'Review request')}</Button
    >
  </Banner>
{/if}

{#if selected}
  <Modal
    title={t('extensions.inputTitle', 'Request from {name}', {
      name: selected.connection ?? selected.extension,
    })}
    closeDisabled={busy}
    onClose={() => (selected = null)}
  >
    {#snippet body()}
      <div class="modal-body extension-input">
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        <p>
          {selected.payload?.message ??
            t(
              'extensions.signInHelp',
              'Open the sign-in page. After signing in, paste the complete redirected address below.',
            )}
        </p>
        {#if inputUrl(selected)}
          <a href={inputUrl(selected)} target="_blank" rel="noopener noreferrer"
            >{t('extensions.openRequest', 'Open requested page')}</a
          >
        {/if}
        {#if selected.kind === 'oauth'}
          <FormField
            label={t('extensions.redirectUrl', 'Redirected address')}
            full
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                type="password"
                autocomplete="off"
                value={drafts.redirect_url ?? ''}
                onInput={(value) =>
                  (drafts = { ...drafts, redirect_url: value })}
                disabled={busy}
              />
            {/snippet}
          </FormField>
        {:else}
          {#each inputFields(selected) as input (input.key)}
            <FormField
              label={input.title ?? input.key}
              help={[input.description, input.enum?.join(', ')]
                .filter(Boolean)
                .join(' — ')}
              full
            >
              {#snippet children(field)}
                {#if input.enum || input.oneOf || input.type === 'boolean'}
                  <Dropdown
                    id={field.controlId}
                    value={drafts[input.key] ?? ''}
                    options={input.type === 'boolean'
                      ? [
                          { value: 'true', label: t('common.yes', 'Yes') },
                          { value: 'false', label: t('common.no', 'No') },
                        ]
                      : (input.oneOf?.map((choice) => ({
                          value: String(choice.const),
                          label: choice.title ?? String(choice.const),
                        })) ??
                        input.enum.map((value, index) => ({
                          value: String(value),
                          label: input.enumNames?.[index] ?? String(value),
                        })))}
                    onValueChange={(value) =>
                      (drafts = { ...drafts, [input.key]: value })}
                    disabled={busy}
                  />
                {:else}
                  <TextField
                    id={field.controlId}
                    type={['number', 'integer'].includes(input.type)
                      ? 'number'
                      : 'text'}
                    value={drafts[input.key] ?? ''}
                    onInput={(value) =>
                      (drafts = { ...drafts, [input.key]: value })}
                    disabled={busy}
                  />
                {/if}
              {/snippet}
            </FormField>
          {/each}
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="primary"
        onClick={() => respond('accept')}
        disabled={busy}>{t('extensions.sendResponse', 'Send response')}</Button
      >
      <Button
        variant="secondary"
        onClick={() => respond('decline')}
        disabled={busy}>{t('extensions.declineInput', 'Decline')}</Button
      >
      <Button
        variant="secondary"
        onClick={() => respond('cancel')}
        disabled={busy}>{t('common.cancel', 'Cancel')}</Button
      >
    {/snippet}
  </Modal>
{/if}

<style>
  .extension-input {
    display: grid;
    gap: 14px;
    max-height: 65vh;
    overflow-y: auto;
  }
  .extension-input p {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .extension-input a {
    color: var(--accent);
  }
</style>
