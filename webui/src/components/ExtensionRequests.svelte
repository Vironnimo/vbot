<script>
  // Requests can arrive during Chat, so the Settings panel cannot own this surface.
  import { onMount } from 'svelte';
  import { extensionOperation, listExtensionRequests } from '$lib/api.js';
  import { dateTimePrefs } from '$lib/dateTimePrefs.svelte.js';
  import {
    PENDING_INPUTS_RESOURCE,
    initialInputDrafts,
    inputFields,
    inputResponse,
    validateInput,
  } from '$lib/extensionInputs.js';
  import { t } from '$lib/i18n.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Checkbox from './ui/Checkbox.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import TextField from './ui/TextField.svelte';
  import Dropdown from './Dropdown.svelte';
  import RequestedUrl from './RequestedUrl.svelte';

  const componentId = $props.id();
  let { subscribeInvalidations = null } = $props();
  let requests = $state([]);
  let selected = $state(null);
  let drafts = $state({});
  let errors = $state({});
  let error = $state('');
  let busy = $state(false);
  let stopped = false;
  // One read runs at a time; a request arriving meanwhile runs once more
  // afterwards, so the last read always follows the newest change.
  let loading = false;
  let loadQueued = false;

  let fields = $derived(selected ? inputFields(selected) : []);
  // A server asking the user to open a page (MCP URL-mode elicitation): the
  // consent is opening it, so the request has no form and no send action.
  let opensPage = $derived(
    selected?.kind === 'elicitation' && selected.payload?.mode === 'url',
  );
  let requester = $derived(selected?.connection ?? selected?.extension ?? '');

  // An Extension publishes a pending-inputs change whenever its list gains or
  // loses an entry, so the list is read only then. An invalidation without an
  // owner (reconnect, Extension reload, enable or disable) reads it too;
  // other Extension changes cannot alter it.
  function onInvalidation({ owner, change }) {
    if (owner == null || change?.resource === PENDING_INPUTS_RESOURCE)
      void refresh();
  }

  async function refresh() {
    loadQueued = true;
    if (loading) return;
    loading = true;
    try {
      while (loadQueued && !stopped) {
        loadQueued = false;
        try {
          const result = await listExtensionRequests();
          if (stopped) return;
          requests = result.requests;
          if (selected && !requests.some((item) => item.id === selected.id))
            selected = null;
        } catch (failure) {
          if (!stopped && selected) error = failure.message;
        }
      }
    } finally {
      loading = false;
    }
  }

  $effect(() => subscribeInvalidations?.(onInvalidation));

  onMount(() => {
    void refresh();
    return () => {
      stopped = true;
    };
  });

  function review(request) {
    selected = request;
    drafts = initialInputDrafts(request, { timeZone: dateTimePrefs.timeZone });
    errors = {};
    error = '';
  }

  function setDraft(key, value) {
    drafts = { ...drafts, [key]: value };
    if (errors[key]) errors = { ...errors, [key]: '' };
  }

  function toggleChoice(field, value, checked) {
    const current = drafts[field.key] ?? [];
    setDraft(
      field.key,
      checked ? [...current, value] : current.filter((item) => item !== value),
    );
  }

  function fieldHelp(field) {
    return [
      field.description,
      field.format === 'date-time'
        ? t('extensions.inputTimeZone', { zone: dateTimePrefs.timeZone })
        : '',
    ]
      .filter(Boolean)
      .join(' ');
  }

  async function respond(action) {
    const request = selected;
    if (!request) return;
    if (action === 'accept' && request.kind === 'elicitation' && !opensPage) {
      errors = validateInput(request, drafts);
      if (Object.values(errors).some(Boolean)) return;
    }
    busy = true;
    error = '';
    try {
      const response = inputResponse(request, drafts, action, {
        timeZone: dateTimePrefs.timeZone,
      });
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
    <span>{t('extensions.inputWaiting', { count: requests.length })}</span>
    <Button variant="primary" onClick={() => review(requests[0])}
      >{t('extensions.reviewInput')}</Button
    >
  </Banner>
{/if}

{#if selected}
  <Modal
    title={t('extensions.inputTitle', { name: requester })}
    closeDisabled={busy}
    onClose={() => (selected = null)}
  >
    {#snippet body()}
      <form
        id={`${componentId}-form`}
        class="modal-body extension-input"
        novalidate
        onsubmit={(event) => {
          event.preventDefault();
          if (!opensPage) void respond('accept');
        }}
      >
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        {#if opensPage}
          <p>{t('extensions.urlRequest', { name: requester })}</p>
          {#if selected.payload?.message}
            <p class="extension-input__message">{selected.payload.message}</p>
          {/if}
          <RequestedUrl
            url={selected.payload?.url}
            openLabel={t('extensions.openPage')}
            disabled={busy}
            onOpen={() => respond('accept')}
            onOpenFailed={() => (error = t('extensions.openFailed'))}
          />
          <p class="extension-input__hint">{t('extensions.urlConsent')}</p>
        {:else}
          <p class="extension-input__message">
            {selected.payload?.message ?? t('extensions.signInHelp')}
          </p>
        {/if}
        {#if selected.kind === 'oauth'}
          {#if selected.payload?.url}
            <RequestedUrl
              url={selected.payload.url}
              openLabel={t('extensions.openSignIn')}
              disabled={busy}
              onOpenFailed={() => (error = t('extensions.openFailed'))}
            />
          {/if}
          <FormField label={t('extensions.redirectUrl')} full>
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                type="password"
                autocomplete="off"
                value={drafts.redirect_url ?? ''}
                onInput={(value) => setDraft('redirect_url', value)}
                disabled={busy}
              />
            {/snippet}
          </FormField>
        {:else if !opensPage}
          {#each fields as input (input.key)}
            {#if input.kind === 'boolean'}
              <div class="extension-input__choice">
                <Checkbox
                  checked={drafts[input.key] === true}
                  disabled={busy}
                  onChange={(checked) => setDraft(input.key, checked)}
                  >{input.label}</Checkbox
                >
                {#if input.description}<p class="extension-input__hint">
                    {input.description}
                  </p>{/if}
              </div>
            {:else if input.kind === 'multiselect'}
              <fieldset
                class="extension-input__choices"
                aria-describedby={errors[input.key]
                  ? `${componentId}-${input.key}-error`
                  : undefined}
              >
                <legend
                  >{input.label}{#if input.required}<span
                      class="extension-input__required"
                      aria-hidden="true">*</span
                    >{/if}</legend
                >
                {#if input.description}<p class="extension-input__hint">
                    {input.description}
                  </p>{/if}
                {#each input.options as option (option.value)}
                  <Checkbox
                    checked={(drafts[input.key] ?? []).includes(option.value)}
                    disabled={busy}
                    onChange={(checked) =>
                      toggleChoice(input, option.value, checked)}
                    >{option.label}</Checkbox
                  >
                {/each}
                {#if errors[input.key]}<p
                    id={`${componentId}-${input.key}-error`}
                    class="extension-input__error"
                  >
                    {errors[input.key]}
                  </p>{/if}
              </fieldset>
            {:else}
              <FormField
                controlId={`${componentId}-${input.key}`}
                label={input.label}
                help={fieldHelp(input)}
                error={errors[input.key] ?? ''}
                required={input.required}
                full
              >
                {#snippet children(field)}
                  {#if input.kind === 'select'}
                    <Dropdown
                      id={field.controlId}
                      ariaDescribedby={field.describedBy}
                      value={drafts[input.key] ?? ''}
                      options={input.required
                        ? input.options
                        : [
                            { value: '', label: t('extensions.noChoice') },
                            ...input.options,
                          ]}
                      onValueChange={(value) => setDraft(input.key, value)}
                      disabled={busy}
                    />
                  {:else if input.kind === 'number'}
                    <TextField
                      id={field.controlId}
                      aria-describedby={field.describedBy}
                      type="number"
                      inputmode={input.integer ? 'numeric' : 'decimal'}
                      step={input.integer ? 1 : 'any'}
                      min={input.minimum}
                      max={input.maximum}
                      invalid={field.invalid}
                      value={drafts[input.key] ?? ''}
                      onInput={(value) => setDraft(input.key, value)}
                      disabled={busy}
                    />
                  {:else}
                    <TextField
                      id={field.controlId}
                      aria-describedby={field.describedBy}
                      type={input.kind === 'text' ? input.type : 'text'}
                      minlength={input.minLength}
                      maxlength={input.maxLength}
                      invalid={field.invalid}
                      value={drafts[input.key] ?? ''}
                      onInput={(value) => setDraft(input.key, value)}
                      disabled={busy}
                    />
                  {/if}
                {/snippet}
              </FormField>
            {/if}
          {/each}
        {/if}
      </form>
    {/snippet}
    {#snippet footer()}
      {#if !opensPage}
        <Button
          variant="primary"
          type="submit"
          form={`${componentId}-form`}
          disabled={busy}>{t('extensions.sendResponse')}</Button
        >
      {/if}
      <Button
        variant="secondary"
        tooltip={t('extensions.declineHelp')}
        onClick={() => respond('decline')}
        disabled={busy}>{t('extensions.declineInput')}</Button
      >
      <Button
        variant="secondary"
        tooltip={t('extensions.cancelHelp')}
        onClick={() => respond('cancel')}
        disabled={busy}>{t('extensions.cancelInput')}</Button
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
  }
  .extension-input__message {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .extension-input__hint {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .extension-input__choice {
    display: grid;
    gap: 4px;
  }
  .extension-input__choices {
    display: grid;
    gap: 6px;
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  .extension-input__choices legend {
    padding: 0;
    margin-bottom: 4px;
  }
  .extension-input__required {
    color: var(--text-lo);
  }
  .extension-input__error {
    color: var(--red);
    font-size: var(--fs-body-sm);
  }
</style>
