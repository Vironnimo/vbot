<script>
  // The add, edit and bind dialog of Settings -> Secrets. Adding stores the
  // typed values (`agent_secrets.create`); binding creates a Secret whose
  // values stay in a password manager (`source`, `source_reference`,
  // `source_fields`, no values); editing sends only what changed
  // (`agent_secrets.update`). A concealed value of an existing Secret is never
  // shown here: its field starts empty and replaces the value only when
  // filled. Settings gives Secrets that outlive a Session (permanent or until
  // a time); a Session's own Secret can be made permanent. Typed values live
  // only while the dialog is open.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import DateField from '../ui/DateField.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import TimeField from '../ui/TimeField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SecretFieldInput from '../secrets/SecretFieldInput.svelte';
  import { untrack } from 'svelte';
  import { createAgentSecret, updateAgentSecret } from '$lib/api.js';
  import {
    SECRET_KINDS,
    SECRET_SINKS,
    defaultSinks,
    fieldLabel,
    kindFields,
    kindLabel,
    lifetimeLabel,
    normalizeFieldName,
    normalizeSecretName,
    secretReference,
    sinkLabel,
  } from '$lib/agentSecrets.js';
  import {
    dateTimePartsInZone,
    dateTimePrefs,
  } from '$lib/dateTimePrefs.svelte.js';
  import { zonedDateTime } from '$lib/extensionInputs.js';
  import { t } from '$lib/i18n.js';

  const componentId = $props.id();
  const FORM_ID = `${componentId}-form`;
  const MAX_DESCRIPTION = 500;
  const noop = () => {};

  let {
    // 'create', 'edit' (with `secret`) or 'bind' (with `binding`:
    // `{ source, entry }` from `agent_secrets.source_entries`).
    mode = 'create',
    secret = null,
    binding = null,
    // The Identity Agents a Secret can be granted to (`{ id, name }`).
    agents = [],
    onSaved = noop,
    onClose = noop,
  } = $props();

  // The dialog edits one Secret or entry from open to close.
  const initial = untrack(() => {
    if (mode === 'edit' && secret) {
      const expiry = expiryParts(secret.expires_at);
      return {
        name: secret.name,
        kind: secret.kind,
        values: Object.fromEntries(
          (secret.fields ?? [])
            .filter((field) => !field.concealed && field.value !== undefined)
            .map((field) => [field.name, field.value]),
        ),
        lifetime: secret.lifetime,
        date: expiry.date,
        time: expiry.time,
        agents: [...(secret.agents ?? [])],
        sinks: [...(secret.sinks ?? [])],
        description: secret.description ?? '',
      };
    }
    const kind = mode === 'bind' ? (binding?.entry?.kind ?? 'login') : 'token';
    return {
      name: mode === 'bind' ? normalizeSecretName(binding?.entry?.title) : '',
      kind,
      values: {},
      lifetime: 'permanent',
      date: '',
      time: '',
      agents: [],
      sinks: defaultSinks(kind),
      description: '',
    };
  });

  let name = $state(initial.name);
  let kind = $state(initial.kind);
  let values = $state(initial.values);
  let lifetime = $state(initial.lifetime);
  let expiryDate = $state(initial.date);
  let expiryTime = $state(initial.time);
  let grants = $state(initial.agents);
  let sinks = $state(initial.sinks);
  let sinksTouched = false;
  let description = $state(initial.description);
  // Bind: the entry fields the Secret uses (source field names).
  let included = $state(
    untrack(() =>
      (binding?.entry?.fields ?? [])
        .filter((field) => normalizeFieldName(field.name))
        .map((field) => field.name),
    ),
  );
  let submitted = $state(false);
  let busy = $state(false);
  let error = $state('');

  const editing = $derived(mode === 'edit');
  const bound = $derived(mode === 'bind' || Boolean(secret?.source));
  const normalizedName = $derived(
    editing ? (secret?.name ?? '') : normalizeSecretName(name),
  );
  // A Secret of one Session keeps its lifetime unless made permanent.
  const sessionOwned = $derived(
    editing && !['permanent', 'until'].includes(secret?.lifetime),
  );
  const outlivesSession = $derived(['permanent', 'until'].includes(lifetime));
  const valueFields = $derived(
    editing
      ? (secret?.fields ?? []).map((field) => ({
          name: field.name,
          concealed: field.concealed,
          optional: true,
        }))
      : kindFields(kind),
  );
  const entryFields = $derived(
    (binding?.entry?.fields ?? []).filter((field) =>
      normalizeFieldName(field.name),
    ),
  );
  const agentOptions = $derived([
    ...agents.map((agent) => ({
      id: agent.id,
      label: agent.name || agent.id,
    })),
    ...grants
      .filter((id) => !agents.some((agent) => agent.id === id))
      .map((id) => ({ id, label: id })),
  ]);
  const lifetimeOptions = $derived(
    sessionOwned
      ? [
          { value: secret.lifetime, label: lifetimeLabel(secret.lifetime) },
          { value: 'permanent', label: lifetimeLabel('permanent') },
        ]
      : [
          { value: 'permanent', label: lifetimeLabel('permanent') },
          { value: 'until', label: t('settings.agentSecrets.untilOption') },
        ],
  );
  const kindOptions = $derived(
    SECRET_KINDS.map((value) => ({ value, label: kindLabel(value) })),
  );

  const nameProblem = $derived(
    editing
      ? ''
      : name.trim() && !normalizedName
        ? t('secrets.form.nameInvalid')
        : submitted && !name.trim()
          ? t('secrets.form.nameRequired')
          : '',
  );
  const missing = $derived(
    submitted && mode === 'create'
      ? valueFields
          .filter((field) => !field.optional && !values[field.name])
          .map((field) => field.name)
      : [],
  );
  const fieldsProblem = $derived(
    submitted && mode === 'bind' && included.length === 0
      ? t('settings.agentSecrets.bindFieldsRequired')
      : '',
  );
  const expiresAt = $derived(
    lifetime === 'until' && expiryDate && expiryTime
      ? zonedDateTime(`${expiryDate}T${expiryTime}`, dateTimePrefs.timeZone)
      : '',
  );
  const expiryProblem = $derived(
    !submitted || lifetime !== 'until'
      ? ''
      : !expiresAt
        ? t('settings.agentSecrets.expiryRequired')
        : Date.parse(expiresAt) <= Date.now()
          ? t('settings.agentSecrets.expiryPast')
          : '',
  );
  const descriptionProblem = $derived(
    description.length > MAX_DESCRIPTION
      ? t('secrets.form.descriptionTooLong', { max: MAX_DESCRIPTION })
      : '',
  );

  function expiryParts(value) {
    if (!value || Number.isNaN(Date.parse(value)))
      return { date: '', time: '' };
    const part = dateTimePartsInZone(value, dateTimePrefs.timeZone);
    return {
      date: `${part.year}-${part.month}-${part.day}`,
      time: `${part.hour}:${part.minute}`,
    };
  }

  function edited() {
    error = '';
  }

  function toggle(list, item, checked) {
    return checked
      ? [...list.filter((value) => value !== item), item]
      : list.filter((value) => value !== item);
  }

  function close() {
    if (busy) return;
    values = {};
    onClose();
  }

  function sameSet(left, right) {
    return (
      left.length === right.length && left.every((item) => right.includes(item))
    );
  }

  function lifetimeParams() {
    return lifetime === 'until'
      ? { lifetime: 'until', expires_at: expiresAt }
      : { lifetime: 'permanent' };
  }

  function createParams() {
    const given = Object.fromEntries(
      valueFields
        .map((field) => [field.name, values[field.name] ?? ''])
        .filter(([, value]) => value !== ''),
    );
    const text = description.trim();
    return {
      name: normalizedName,
      kind,
      values: given,
      ...lifetimeParams(),
      agents: grants,
      sinks: SECRET_SINKS.filter((sink) => sinks.includes(sink)),
      ...(text ? { description: text } : {}),
    };
  }

  function bindParams() {
    const chosen = entryFields.filter((field) => included.includes(field.name));
    const text = description.trim();
    return {
      name: normalizedName,
      kind,
      values: {},
      ...lifetimeParams(),
      agents: grants,
      sinks: SECRET_SINKS.filter((sink) => sinks.includes(sink)),
      ...(text ? { description: text } : {}),
      source: binding.source.name,
      source_reference: binding.entry.reference,
      source_fields: Object.fromEntries(
        chosen.map((field) => [normalizeFieldName(field.name), field.name]),
      ),
      concealed: Object.fromEntries(
        chosen.map((field) => [
          normalizeFieldName(field.name),
          field.concealed !== false,
        ]),
      ),
    };
  }

  // Only what changed: an untouched concealed field keeps its value.
  function updateParams() {
    const params = { id: secret.id };
    if (!secret.source) {
      const changed = Object.fromEntries(
        valueFields
          .map((field) => [field.name, values[field.name] ?? ''])
          .filter(
            ([field, value]) => value !== '' && value !== initial.values[field],
          ),
      );
      if (Object.keys(changed).length > 0) params.values = changed;
    }
    const text = description.trim();
    if (text !== (secret.description ?? '')) params.description = text;
    if (!sameSet(sinks, secret.sinks ?? [])) {
      params.sinks = SECRET_SINKS.filter((sink) => sinks.includes(sink));
    }
    if (sessionOwned && lifetime === 'permanent') {
      params.permanent = true;
    } else if (!sessionOwned) {
      if (lifetime === 'permanent' && secret.lifetime === 'until') {
        params.clear_expiry = true;
      } else if (
        lifetime === 'until' &&
        expiresAt &&
        Date.parse(expiresAt) !== Date.parse(secret.expires_at ?? '')
      ) {
        params.expires_at = expiresAt;
      }
    }
    if (outlivesSession && !sameSet(grants, secret.agents ?? [])) {
      params.agents = grants;
    }
    return params;
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    submitted = true;
    if (
      nameProblem ||
      (!editing && !normalizedName) ||
      missing.length > 0 ||
      fieldsProblem ||
      expiryProblem ||
      descriptionProblem
    ) {
      return;
    }
    const params =
      mode === 'edit'
        ? updateParams()
        : mode === 'bind'
          ? bindParams()
          : createParams();
    if (mode === 'edit' && Object.keys(params).length === 1) {
      onClose();
      return;
    }
    busy = true;
    error = '';
    try {
      const result =
        mode === 'edit'
          ? await updateAgentSecret(params)
          : await createAgentSecret(params);
      values = {};
      onSaved(result?.secret ?? { name: normalizedName }, mode);
    } catch (failure) {
      busy = false;
      error = `${t('settings.agentSecrets.saveError')} ${failure?.message ?? ''}`;
    }
  }
</script>

<Modal
  title={mode === 'edit'
    ? t('settings.agentSecrets.editTitle', { name: secret?.name ?? '' })
    : mode === 'bind'
      ? t('settings.agentSecrets.bindTitle', {
          title: binding?.entry?.title ?? '',
        })
      : t('settings.agentSecrets.addTitle')}
  size="lg"
  closeDisabled={busy}
  onClose={close}
>
  {#snippet body()}
    <form id={FORM_ID} novalidate onsubmit={submit}>
      <div class="modal-body agent-secret-editor">
        {#if error}
          <Banner variant="error" role="alert">{error}</Banner>
        {/if}
        {#if editing}
          <p class="agent-secret-editor__lead">
            <span class="agent-secret-editor__mono">{secret.reference}</span>
            · {kindLabel(secret.kind)}
          </p>
        {:else}
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
        {/if}
        {#if mode === 'create'}
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
                  if (!sinksTouched) sinks = defaultSinks(next);
                  edited();
                }}
              />
            {/snippet}
          </FormField>
        {/if}

        {#if mode === 'bind'}
          <fieldset class="agent-secret-editor__group">
            <legend>{t('settings.agentSecrets.bindFields')}</legend>
            <p class="agent-secret-editor__hint">
              {t('settings.agentSecrets.bindFieldsHelp', {
                source: binding.source.label ?? binding.source.name,
              })}
            </p>
            {#each entryFields as field (field.name)}
              <Checkbox
                checked={included.includes(field.name)}
                disabled={busy}
                onChange={(checked) => {
                  included = toggle(included, field.name, checked);
                  edited();
                }}
              >
                <span class="agent-secret-editor__mono">
                  {normalizeFieldName(field.name)}
                </span>
                {#if field.concealed !== false}
                  · {t('settings.agentSecrets.concealed')}
                {:else if field.value}
                  · {field.value}
                {/if}
              </Checkbox>
            {/each}
            {#if fieldsProblem}
              <p class="agent-secret-editor__error" role="alert">
                {fieldsProblem}
              </p>
            {/if}
          </fieldset>
        {:else if bound}
          <p class="agent-secret-editor__hint">
            {t('settings.agentSecrets.boundValues', {
              source: secret.source.source,
            })}
          </p>
        {:else}
          {#each valueFields as field (`${kind}:${field.name}`)}
            <FormField
              controlId={`${componentId}-${field.name}`}
              label={field.optional && !editing
                ? t('secrets.field.optional', { field: fieldLabel(field.name) })
                : fieldLabel(field.name)}
              help={editing && field.concealed
                ? t('settings.agentSecrets.keepValue')
                : ''}
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
        {/if}

        <FormField
          controlId={`${componentId}-lifetime`}
          label={t('secrets.form.lifetime')}
          error={expiryProblem}
          full
        >
          {#snippet children(control)}
            <div class="agent-secret-editor__lifetime">
              <Dropdown
                id={control.controlId}
                ariaLabelledby={control.labelId}
                ariaDescribedby={control.describedBy}
                value={lifetime}
                options={lifetimeOptions}
                disabled={busy}
                onValueChange={(next) => {
                  lifetime = next;
                  edited();
                }}
              />
              {#if lifetime === 'until'}
                <DateField
                  ariaLabel={t('settings.agentSecrets.expiryDate')}
                  aria-describedby={control.describedBy}
                  invalid={control.invalid}
                  value={expiryDate}
                  disabled={busy}
                  onChange={(next) => {
                    expiryDate = next;
                    edited();
                  }}
                />
                <TimeField
                  ariaLabel={t('settings.agentSecrets.expiryTime')}
                  aria-describedby={control.describedBy}
                  invalid={control.invalid}
                  value={expiryTime}
                  disabled={busy}
                  onChange={(next) => {
                    expiryTime = next;
                    edited();
                  }}
                />
              {/if}
            </div>
          {/snippet}
        </FormField>

        {#if outlivesSession}
          <fieldset class="agent-secret-editor__group">
            <legend>{t('settings.agentSecrets.agents')}</legend>
            <p class="agent-secret-editor__hint">
              {t('settings.agentSecrets.agentsHelp')}
            </p>
            {#if agentOptions.length === 0}
              <p class="agent-secret-editor__hint">
                {t('settings.agentSecrets.noAgents')}
              </p>
            {/if}
            {#each agentOptions as agent (agent.id)}
              <Checkbox
                checked={grants.includes(agent.id)}
                disabled={busy}
                onChange={(checked) => {
                  grants = toggle(grants, agent.id, checked);
                  edited();
                }}>{agent.label}</Checkbox
              >
            {/each}
          </fieldset>
        {/if}

        <fieldset class="agent-secret-editor__group">
          <legend>{t('settings.agentSecrets.sinks')}</legend>
          <p class="agent-secret-editor__hint">
            {t('settings.agentSecrets.sinksHelp')}
          </p>
          {#each SECRET_SINKS as sink (sink)}
            <Checkbox
              checked={sinks.includes(sink)}
              disabled={busy}
              onChange={(checked) => {
                sinks = toggle(sinks, sink, checked);
                sinksTouched = true;
                edited();
              }}>{sinkLabel(sink)}</Checkbox
            >
          {/each}
        </fieldset>

        <FormField
          controlId={`${componentId}-description`}
          label={t('secrets.form.descriptionOptional')}
          help={t('secrets.form.descriptionHelp')}
          error={descriptionProblem}
          full
        >
          {#snippet children(control)}
            <TextArea
              id={control.controlId}
              aria-describedby={control.describedBy}
              invalid={control.invalid}
              rows={2}
              autoGrow
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
      {t('common.save')}
    </Button>
  {/snippet}
</Modal>

<style>
  .agent-secret-editor p {
    margin: 0;
  }

  .agent-secret-editor__mono {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }

  .agent-secret-editor__group {
    display: grid;
    gap: 6px;
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .agent-secret-editor__group legend {
    padding: 0;
    margin-bottom: 2px;
    font-weight: 600;
  }

  .agent-secret-editor__hint {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }

  .agent-secret-editor__error {
    color: var(--red);
    font-size: var(--fs-body-sm);
  }

  .agent-secret-editor__lifetime {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 8.5rem;
    gap: 8px;
  }

  @media (max-width: 640px) {
    .agent-secret-editor__lifetime {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
