<script>
  // The Agent's pending Secret requests in the displayed Session (the
  // `secrets` Tool's `request` action): a notice per request above the
  // composer, and a dialog that takes the value with masked fields and the
  // lifetime, preset to the requested one. Store answers the request
  // (`agent_secrets.answer`), Decline turns it down; either way the Agent's
  // Tool call returns. The typed values live only while the dialog is open.
  // The list reloads on mount, when the Session changes and whenever
  // `refreshToken` bumps (`resource_changed(kind:"agent_secrets")`), so an
  // answered, declined or timed-out request goes away.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SecretFieldInput from '../secrets/SecretFieldInput.svelte';
  import {
    answerSecretRequest,
    declineSecretRequest,
    listAgentSecrets,
  } from '$lib/api.js';
  import {
    fieldLabel,
    kindLabel,
    lifetimeChoiceParams,
    lifetimeChoices,
    normalizeSecretOverview,
    secretReference,
  } from '$lib/agentSecrets.js';
  import { t } from '$lib/i18n.js';
  import { formatAbsoluteTime } from '$lib/timeText.js';

  const componentId = $props.id();
  const FORM_ID = `${componentId}-form`;

  let { agentId = '', sessionId = '', refreshToken = 0 } = $props();

  let requests = $state([]);
  let loadGeneration = 0;
  let selected = $state(null);
  let values = $state({});
  let lifetime = $state('session');
  let submitted = $state(false);
  let busy = $state(false);
  let error = $state('');

  const shown = $derived(
    sessionId
      ? requests.filter(
          (request) =>
            request.session_id === sessionId &&
            (!agentId || request.agent_id === agentId),
        )
      : [],
  );
  const missing = $derived(
    selected && submitted
      ? selected.fields
          .filter((field) => !field.optional && !values[field.name])
          .map((field) => field.name)
      : [],
  );

  $effect(() => {
    void refreshToken;
    const session = sessionId;
    if (!session) {
      requests = [];
      return;
    }
    void load();
  });

  // A request answered or gone elsewhere closes its dialog.
  $effect(() => {
    if (selected && !shown.some((request) => request.id === selected.id)) {
      close();
    }
  });

  async function load() {
    const generation = ++loadGeneration;
    try {
      const result = await listAgentSecrets();
      if (generation === loadGeneration) {
        requests = normalizeSecretOverview(result).requests;
      }
    } catch {
      // The notice shows again with the next successful read.
    }
  }

  function open(request) {
    selected = request;
    values = {};
    lifetime = lifetimeChoices().some(
      (choice) => choice.value === request.lifetime,
    )
      ? request.lifetime
      : 'session';
    submitted = false;
    busy = false;
    error = '';
  }

  function close() {
    selected = null;
    values = {};
    submitted = false;
    busy = false;
    error = '';
  }

  function forget(requestId) {
    requests = requests.filter((request) => request.id !== requestId);
  }

  async function store(event) {
    event.preventDefault();
    const request = selected;
    if (!request || busy) return;
    submitted = true;
    if (missing.length > 0) return;
    const chosen = lifetimeChoiceParams(lifetime);
    const given = Object.fromEntries(
      Object.entries(values).filter(([, value]) => value !== ''),
    );
    busy = true;
    error = '';
    try {
      await answerSecretRequest({ id: request.id, values: given, ...chosen });
      forget(request.id);
      close();
    } catch (failure) {
      busy = false;
      error = `${t('chat.secretRequest.storeError')} ${failure?.message ?? ''}`;
    }
  }

  async function decline(request) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await declineSecretRequest(request.id);
      forget(request.id);
      close();
    } catch (failure) {
      busy = false;
      error = `${t('chat.secretRequest.declineError')} ${failure?.message ?? ''}`;
      if (!selected) selected = request;
    }
  }
</script>

{#each shown as request (request.id)}
  <Banner
    appearance="compact"
    class="chat-view__footer-banner secret-request"
    role="status"
    data-secret-request={request.name}
  >
    <span class="secret-request__text">
      {request.replaces
        ? t('chat.secretRequest.replaceNotice', { name: request.name })
        : t('chat.secretRequest.notice', { name: request.name })}
    </span>
    <span class="secret-request__actions">
      <Button
        variant="tertiary"
        disabled={busy}
        onClick={() => decline(request)}
      >
        {t('chat.secretRequest.decline')}
      </Button>
      <Button variant="primary" disabled={busy} onClick={() => open(request)}>
        {t('chat.secretRequest.enter')}
      </Button>
    </span>
  </Banner>
{/each}

{#if selected}
  <Modal
    title={t('chat.secretRequest.title', { name: selected.name })}
    closeDisabled={busy}
    onClose={close}
  >
    {#snippet body()}
      <form id={FORM_ID} novalidate onsubmit={store}>
        <div class="modal-body secret-request__form">
          {#if error}
            <Banner variant="error" role="alert">{error}</Banner>
          {/if}
          <p class="secret-request__lead">
            {t('chat.secretRequest.lead', {
              kind: kindLabel(selected.kind),
              reference: secretReference(selected.name),
            })}
          </p>
          {#if selected.description}
            <p class="secret-request__description">{selected.description}</p>
          {/if}
          {#if selected.replaces}
            <p class="secret-request__hint">
              {t('chat.secretRequest.replaces')}
            </p>
          {/if}
          {#each selected.fields as field (field.name)}
            <FormField
              controlId={`${componentId}-${field.name}`}
              label={field.optional
                ? t('secrets.field.optional', { field: fieldLabel(field.name) })
                : fieldLabel(field.name)}
              error={missing.includes(field.name)
                ? t('secrets.field.required')
                : ''}
              required={!field.optional}
              full
            >
              {#snippet children(control)}
                <SecretFieldInput
                  id={control.controlId}
                  describedBy={control.describedBy}
                  invalid={control.invalid}
                  concealed={field.concealed}
                  multiline={selected.kind === 'text'}
                  value={values[field.name] ?? ''}
                  disabled={busy}
                  onInput={(next) => {
                    values = { ...values, [field.name]: next };
                    error = '';
                  }}
                />
              {/snippet}
            </FormField>
          {/each}
          <FormField
            controlId={`${componentId}-lifetime`}
            label={t('secrets.form.lifetime')}
            full
          >
            {#snippet children(control)}
              <Dropdown
                id={control.controlId}
                ariaLabelledby={control.labelId}
                value={lifetime}
                options={lifetimeChoices()}
                disabled={busy}
                onValueChange={(next) => (lifetime = next)}
              />
            {/snippet}
          </FormField>
          {#if selected.expires_at}
            <p class="secret-request__hint">
              {t('chat.secretRequest.waits', {
                time: formatAbsoluteTime(selected.expires_at),
              })}
            </p>
          {/if}
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={busy}
        onClick={() => decline(selected)}
      >
        {t('chat.secretRequest.decline')}
      </Button>
      <Button type="submit" form={FORM_ID} variant="primary" loading={busy}>
        {t('chat.secretRequest.store')}
      </Button>
    {/snippet}
  </Modal>
{/if}

<style>
  .secret-request__text {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .secret-request__actions {
    display: inline-flex;
    flex-shrink: 0;
    gap: 4px;
  }

  .secret-request__form p {
    margin: 0;
  }

  .secret-request__description {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .secret-request__hint {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }
</style>
