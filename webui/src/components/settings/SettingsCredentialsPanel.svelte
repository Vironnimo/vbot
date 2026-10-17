<script>
  // Settings -> Integrations -> Credentials: the named credentials of the
  // server's Secret Store (`secrets.list`), every saved name with its state
  // and never a value. A value is typed into the add or replace dialog, sent
  // once with `secrets.set` and dropped when the dialog closes. A name set in
  // the server's process environment takes precedence over the saved value,
  // so its row offers no action. The list reloads on mount and whenever
  // `credentialsRefreshToken` bumps: the server's `data_store` or `providers`
  // invalidation, one of which every credential change publishes.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import { listCredentials, setCredential, unsetCredential } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_STORE_UNAVAILABLE,
    credentialNameProblem,
    credentialValueProblem,
    describeCredential,
    normalizeCredentialList,
  } from '$lib/settingsView.js';

  const noop = () => {};
  const FORM_ID = 'settings-credential-form';

  let { credentialsRefreshToken = 0, onToast = noop } = $props();

  let store = $state({ state: '', reason: '', credentials: [] });
  let loaded = $state(false);
  let loadError = $state('');
  // Only the newest read may replace the list.
  let loadGeneration = 0;

  // The open add or replace dialog: `{ mode: 'add' }` or
  // `{ mode: 'replace', name }`. The typed value lives only while it is open.
  let editor = $state(null);
  let draftName = $state('');
  let draftValue = $state('');
  let submitted = $state(false);
  let removeTarget = $state('');
  let busy = $state(false);
  let dialogError = $state('');

  const unavailable = $derived(store.state === CREDENTIAL_STORE_UNAVAILABLE);
  const unavailableReason = $derived(
    unavailable ? t('settings.credentials.unavailableAction') : '',
  );
  const rows = $derived(
    store.credentials.map((credential) => ({
      credential,
      ...describeCredential(credential),
    })),
  );
  const adding = $derived(editor?.mode === 'add');
  // A typed name is checked as it is typed; an empty field only on submit.
  const nameProblem = $derived(
    adding && (submitted || draftName.trim())
      ? credentialNameProblem(draftName.trim(), store.credentials)
      : '',
  );
  const valueProblem = $derived(
    editor && submitted ? credentialValueProblem(draftValue) : '',
  );

  $effect(() => {
    void credentialsRefreshToken;
    void load();
  });

  async function load() {
    const generation = ++loadGeneration;
    try {
      const result = await listCredentials();
      if (generation !== loadGeneration) {
        return;
      }
      store = normalizeCredentialList(result);
      loadError = '';
      loaded = true;
    } catch (error) {
      if (generation === loadGeneration) {
        loadError = `${t('settings.credentials.loadError')} ${error?.message ?? ''}`;
      }
    }
  }

  function closeDialogs() {
    editor = null;
    removeTarget = '';
    draftName = '';
    draftValue = '';
    submitted = false;
    busy = false;
    dialogError = '';
  }

  function openAdd() {
    closeDialogs();
    editor = { mode: 'add' };
  }

  function openReplace(name) {
    closeDialogs();
    editor = { mode: 'replace', name };
  }

  function openRemove(name) {
    closeDialogs();
    removeTarget = name;
  }

  async function submitEditor(event) {
    event.preventDefault();
    if (!editor || busy) {
      return;
    }
    submitted = true;
    const name = adding ? draftName.trim() : editor.name;
    const value = draftValue.trim();
    if (
      (adding && credentialNameProblem(name, store.credentials)) ||
      credentialValueProblem(value)
    ) {
      return;
    }
    busy = true;
    dialogError = '';
    try {
      const result = await setCredential(name, value);
      // Closing drops the typed value.
      closeDialogs();
      onToast(
        result?.source === CREDENTIAL_SOURCE_ENVIRONMENT
          ? {
              title: t('settings.credentials.savedShadowed', { name }),
              variant: 'warn',
            }
          : {
              title: t('settings.credentials.saved', { name }),
              variant: 'success',
            },
      );
      void load();
    } catch (error) {
      busy = false;
      dialogError = `${t('settings.credentials.saveError')} ${error?.message ?? ''}`;
    }
  }

  async function confirmRemove() {
    const name = removeTarget;
    if (!name || busy) {
      return;
    }
    busy = true;
    dialogError = '';
    try {
      await unsetCredential(name);
      closeDialogs();
      onToast({
        title: t('settings.credentials.removed', { name }),
        variant: 'success',
      });
      void load();
    } catch (error) {
      busy = false;
      dialogError = `${t('settings.credentials.removeError')} ${error?.message ?? ''}`;
    }
  }
</script>

{#if loadError}
  <Banner variant="error" role="alert">
    <span>{loadError}</span>
    <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
  </Banner>
{/if}

{#if unavailable}
  <Banner variant="warn" data-credential-store="unavailable">
    <span class="credentials-unavailable">
      <span>{t('settings.credentials.unavailable')}</span>
      {#if store.reason}
        <span class="credentials-unavailable__reason">
          {t('settings.credentials.unavailableReason', {
            reason: store.reason,
          })}
        </span>
      {/if}
    </span>
  </Banner>
{/if}

<div class="s-group-toolbar s-list-toolbar">
  <span class="s-group-toolbar__meta credentials-meta">
    {#if store.credentials.length > 0}
      <span>
        {t('settings.credentials.count', { count: store.credentials.length })}
      </span>
    {/if}
    <InfoHint text={t('settings.credentials.help')} />
  </span>
  <div class="s-group-toolbar__actions s-list-toolbar__end">
    <Button
      variant="secondary"
      disabled={!loaded || unavailable}
      disabledReason={unavailableReason}
      onClick={openAdd}
    >
      {t('settings.credentials.add')}
    </Button>
  </div>
</div>

{#if !loaded}
  {#if !loadError}
    <Banner variant="neutral">{t('common.loading')}</Banner>
  {/if}
{:else if rows.length === 0}
  <EmptyState density="compact" description={t('settings.credentials.empty')} />
{:else}
  <div
    class="s-group"
    role="list"
    aria-label={t('settings.credentials.listLabel')}
  >
    {#each rows as row (row.credential.name)}
      {@const name = row.credential.name}
      <div
        class="s-entity"
        role="listitem"
        data-credential={name}
        data-credential-source={row.credential.source ?? 'none'}
        data-credential-configured={row.credential.configured}
        data-credential-damaged={row.credential.damaged}
      >
        <div class="s-entity__head">
          <div class="s-row-info">
            <div class="s-row-label credentials-name">{name}</div>
            <div class="s-row-desc">{row.description}</div>
          </div>
          <div class="s-entity__end">
            {#if row.chip}
              <StatusChip variant={row.chip.variant}>
                {row.chip.label}
              </StatusChip>
            {/if}
            {#if row.editable}
              <Button
                variant="tertiary"
                ariaLabel={t('settings.credentials.replaceAria', { name })}
                disabled={unavailable}
                disabledReason={unavailableReason}
                onClick={() => openReplace(name)}
              >
                {t('settings.credentials.replace')}
              </Button>
              <Button
                variant="secondary"
                ariaLabel={t('settings.credentials.removeAria', { name })}
                disabled={unavailable}
                disabledReason={unavailableReason}
                onClick={() => openRemove(name)}
              >
                {t('common.remove')}
              </Button>
            {/if}
          </div>
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if editor}
  <Modal
    title={adding
      ? t('settings.credentials.addTitle')
      : t('settings.credentials.replaceTitle', { name: editor.name })}
    closeDisabled={busy}
    onClose={closeDialogs}
  >
    {#snippet body()}
      <form id={FORM_ID} novalidate onsubmit={submitEditor}>
        <div class="modal-body">
          {#if dialogError}
            <Banner variant="error" role="alert">{dialogError}</Banner>
          {/if}
          {#if adding}
            <FormField
              controlId="settings-credential-name"
              label={t('settings.credentials.name')}
              help={t('settings.credentials.nameHelp')}
              error={nameProblem}
              required
            >
              {#snippet children(field)}
                <TextField
                  id={field.controlId}
                  variant="modal"
                  code
                  autocomplete="off"
                  spellcheck="false"
                  aria-describedby={field.describedBy}
                  invalid={field.invalid}
                  value={draftName}
                  disabled={busy}
                  onInput={(next) => {
                    draftName = next;
                    dialogError = '';
                  }}
                />
              {/snippet}
            </FormField>
          {/if}
          <FormField
            controlId="settings-credential-value"
            label={t('settings.credentials.value')}
            help={t('settings.credentials.valueHelp')}
            error={valueProblem}
            required
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                type="password"
                autocomplete="off"
                aria-describedby={field.describedBy}
                invalid={field.invalid}
                value={draftValue}
                disabled={busy}
                placeholder={t('settings.credentials.valuePlaceholder')}
                onInput={(next) => {
                  draftValue = next;
                  dialogError = '';
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" disabled={busy} onClick={closeDialogs}>
        {t('common.cancel')}
      </Button>
      <Button type="submit" form={FORM_ID} variant="primary" loading={busy}>
        {t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if removeTarget}
  <ConfirmDialog
    title={t('settings.credentials.removeTitle', { name: removeTarget })}
    body={t('settings.credentials.removeBody', { name: removeTarget })}
    confirmLabel={t('common.remove')}
    {busy}
    error={dialogError}
    onConfirm={confirmRemove}
    onCancel={closeDialogs}
  />
{/if}

<style>
  .credentials-meta {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .credentials-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }

  .credentials-unavailable {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .credentials-unavailable__reason {
    overflow-wrap: anywhere;
  }
</style>
