<script>
  // The composer's Secret form, opened by a bare `/secret`: the user gives
  // the displayed Session's Agent a Secret it uses through its reference
  // without seeing the value (`agent_secrets.give`; a pending request of the
  // same name is answered). A draft has no Session yet, so its form sends the
  // equivalent `/secret` command instead, which creates the Session; that
  // text goes only to the server, never to the composer's drafts or history.
  // Typed values live only while the form is open.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SecretFieldInput from '../secrets/SecretFieldInput.svelte';
  import { giveAgentSecret } from '$lib/api.js';
  import {
    SECRET_KINDS,
    fieldLabel,
    kindLabel,
    lifetimeChoiceDuration,
    lifetimeChoiceParams,
    lifetimeChoices,
    normalizeSecretName,
    secretCommandText,
    secretReference,
  } from '$lib/agentSecrets.js';
  import { t } from '$lib/i18n.js';

  const componentId = $props.id();
  const FORM_ID = `${componentId}-form`;
  const MAX_DESCRIPTION = 500;
  // Shorter values are not recognized in output (`MIN_MASKED_LENGTH`).
  const SHORT_VALUE = 8;
  // The composer form gives what `/secret` can: a login's username and
  // password; Settings -> Secrets adds a URL or a TOTP seed.
  const FORM_FIELDS = {
    token: [{ name: 'value', concealed: true, optional: false }],
    login: [
      { name: 'username', concealed: false, optional: true },
      { name: 'password', concealed: true, optional: false },
    ],
    text: [{ name: 'value', concealed: true, optional: false }],
  };

  const noop = () => {};

  let {
    // The bare Agent id and its Project (null for an Identity Agent).
    agentId = '',
    projectId = null,
    // The displayed Session; empty for a draft.
    sessionId = '',
    // A draft's send: resolves truthy once the command was accepted.
    onSendCommand = null,
    // The notice to show once the Secret was stored.
    onGiven = noop,
    onClose = noop,
  } = $props();

  let name = $state('');
  let kind = $state('token');
  let values = $state({});
  let lifetime = $state('session');
  let description = $state('');
  let submitted = $state(false);
  let busy = $state(false);
  let error = $state('');
  let commandProblem = $state('');

  const fields = $derived(FORM_FIELDS[kind] ?? FORM_FIELDS.token);
  const normalizedName = $derived(normalizeSecretName(name));
  const nameProblem = $derived(
    name.trim() && !normalizedName
      ? t('secrets.form.nameInvalid')
      : submitted && !name.trim()
        ? t('secrets.form.nameRequired')
        : '',
  );
  const missing = $derived(
    submitted
      ? fields
          .filter((field) => !field.optional && !values[field.name])
          .map((field) => field.name)
      : [],
  );
  const descriptionProblem = $derived(
    description.length > MAX_DESCRIPTION
      ? t('secrets.form.descriptionTooLong', { max: MAX_DESCRIPTION })
      : '',
  );
  const kindOptions = $derived(
    SECRET_KINDS.map((value) => ({ value, label: kindLabel(value) })),
  );

  function edited() {
    error = '';
    commandProblem = '';
  }

  function close() {
    if (busy) return;
    values = {};
    onClose();
  }

  function givenValues() {
    return Object.fromEntries(
      fields
        .map((field) => [field.name, values[field.name] ?? ''])
        .filter(([, value]) => value !== ''),
    );
  }

  function storedNotice(secret, replaced, given) {
    const short = fields.some(
      (field) =>
        field.concealed &&
        given[field.name] &&
        given[field.name].length < SHORT_VALUE,
    );
    const stored = replaced
      ? t('chat.secretForm.replaced', { name: secret.name })
      : t('chat.secretForm.stored', {
          name: secret.name,
          reference: secret.reference ?? secretReference(secret.name),
        });
    return short
      ? `${stored} ${t('chat.secretForm.shortValue', { min: SHORT_VALUE })}`
      : stored;
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    submitted = true;
    if (
      !normalizedName ||
      missing.length > 0 ||
      descriptionProblem ||
      nameProblem
    ) {
      return;
    }
    const given = givenValues();
    const text = description.trim();
    busy = true;
    error = '';
    try {
      if (sessionId) {
        const result = await giveAgentSecret({
          ...(projectId ? { project_id: projectId } : {}),
          agent_id: agentId,
          session_id: sessionId,
          name: normalizedName,
          kind,
          values: given,
          ...lifetimeChoiceParams(lifetime),
          ...(text ? { description: text } : {}),
        });
        values = {};
        onGiven(
          storedNotice(
            result?.secret ?? { name: normalizedName },
            result?.replaced === true,
            given,
          ),
        );
        onClose();
        return;
      }
      const command = secretCommandText({
        name: normalizedName,
        kind,
        values: given,
        lifetime: lifetimeChoiceParams(lifetime).lifetime,
        duration: lifetimeChoiceDuration(lifetime),
        description: text,
      });
      if (command.problem) {
        busy = false;
        commandProblem = command.problem;
        return;
      }
      const sent = await onSendCommand?.(command.text);
      if (!sent) {
        busy = false;
        error = t('chat.secretForm.sendError');
        return;
      }
      values = {};
      onClose();
    } catch (failure) {
      busy = false;
      error = `${t('chat.secretForm.storeError')} ${failure?.message ?? ''}`;
    }
  }
</script>

<Modal
  title={t('chat.secretForm.title')}
  description={t('chat.secretForm.description')}
  closeDisabled={busy}
  onClose={close}
>
  {#snippet body()}
    <form id={FORM_ID} novalidate onsubmit={submit}>
      <div class="modal-body">
        {#if error}
          <Banner variant="error" role="alert">{error}</Banner>
        {/if}
        <FormField
          controlId={`${componentId}-name`}
          label={t('secrets.form.name')}
          help={normalizedName
            ? t('secrets.form.referencePreview', {
                reference: secretReference(normalizedName),
              })
            : t('secrets.form.nameHelp')}
          error={nameProblem}
          required
          full
        >
          {#snippet children(control)}
            <TextField
              id={control.controlId}
              variant="modal"
              code
              autocomplete="off"
              spellcheck="false"
              aria-describedby={control.describedBy}
              invalid={control.invalid}
              value={name}
              disabled={busy}
              placeholder="GITHUB_TOKEN"
              onInput={(next) => {
                name = next;
                edited();
              }}
            />
          {/snippet}
        </FormField>
        <FormField
          controlId={`${componentId}-kind`}
          label={t('secrets.form.kind')}
          full
        >
          {#snippet children(control)}
            <Dropdown
              id={control.controlId}
              ariaLabelledby={control.labelId}
              value={kind}
              options={kindOptions}
              disabled={busy}
              onValueChange={(next) => {
                kind = next;
                values = {};
                edited();
              }}
            />
          {/snippet}
        </FormField>
        {#each fields as field (`${kind}:${field.name}`)}
          <FormField
            controlId={`${componentId}-${field.name}`}
            label={field.optional
              ? t('secrets.field.optional', { field: fieldLabel(field.name) })
              : fieldLabel(field.name)}
            error={missing.includes(field.name)
              ? t('secrets.field.required')
              : commandProblem === field.name
                ? t('chat.secretForm.usernameSpace')
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
                multiline={kind === 'text'}
                value={values[field.name] ?? ''}
                disabled={busy}
                onInput={(next) => {
                  values = { ...values, [field.name]: next };
                  edited();
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
        <FormField
          controlId={`${componentId}-description`}
          label={t('secrets.form.descriptionOptional')}
          help={t('secrets.form.descriptionHelp')}
          error={descriptionProblem ||
            (commandProblem === 'description'
              ? t('chat.secretForm.descriptionQuote')
              : '')}
          full
        >
          {#snippet children(control)}
            <TextField
              id={control.controlId}
              variant="modal"
              aria-describedby={control.describedBy}
              invalid={control.invalid}
              value={description}
              disabled={busy}
              onInput={(next) => {
                description = next;
                edited();
              }}
            />
          {/snippet}
        </FormField>
      </div>
    </form>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" disabled={busy} onClick={close}>
      {t('common.cancel')}
    </Button>
    <Button type="submit" form={FORM_ID} variant="primary" loading={busy}>
      {t('chat.secretForm.give')}
    </Button>
  {/snippet}
</Modal>
