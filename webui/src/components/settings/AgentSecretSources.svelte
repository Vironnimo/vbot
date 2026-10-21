<script>
  // The password managers of Settings -> Secrets (Secret Sources, which
  // Extensions contribute): each Source's state with Unlock, Lock and Browse.
  // Unlock asks for the Source's `unlock_fields` (concealed ones masked) and
  // sends them once with `agent_secrets.source_unlock`; the typed inputs are
  // dropped when the dialog closes. Browse lists an unlocked Source's entries
  // (`agent_secrets.source_entries`, never a concealed value) and hands the
  // one the user picks to `onBind`, which creates a Secret bound to it.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SecretFieldInput from '../secrets/SecretFieldInput.svelte';
  import {
    listSecretSourceEntries,
    lockSecretSource,
    unlockSecretSource,
  } from '$lib/api.js';
  import { kindLabel } from '$lib/agentSecrets.js';
  import { t } from '$lib/i18n.js';

  const componentId = $props.id();
  const UNLOCK_FORM_ID = `${componentId}-unlock`;
  const noop = () => {};

  let {
    sources = [],
    // `({ source, entry })`: create a Secret bound to this entry.
    onBind = noop,
    // A Source's state changed here; the Secrets overview reads it again.
    onChanged = noop,
    onToast = noop,
  } = $props();

  const STATE_CHIPS = {
    available: {
      variant: 'success',
      label: () => t('settings.agentSecrets.sourceAvailable'),
    },
    locked: {
      variant: 'warn',
      label: () => t('settings.agentSecrets.sourceLocked'),
    },
    unavailable: {
      variant: 'neutral',
      label: () => t('settings.agentSecrets.sourceUnavailable'),
    },
  };

  let busySource = $state('');
  let rowError = $state('');
  let unlocking = $state(null);
  let unlockInputs = $state({});
  let unlockSubmitted = $state(false);
  let unlockError = $state('');
  let browsing = $state(null);
  let listing = $state(null);
  let container = $state('');
  let browseError = $state('');
  let browseLoading = $state(false);
  let browseGeneration = 0;

  const unlockMissing = $derived(
    unlocking && unlockSubmitted
      ? (unlocking.unlock_fields ?? [])
          .filter(
            (field) => field.required !== false && !unlockInputs[field.name],
          )
          .map((field) => field.name)
      : [],
  );
  const containerOptions = $derived([
    { value: '', label: t('settings.agentSecrets.allContainers') },
    ...(listing?.containers ?? []).map((value) => ({ value, label: value })),
  ]);

  function sourceLabel(source) {
    return source.label || source.name;
  }

  async function unlock(source, inputs) {
    busySource = source.name;
    rowError = '';
    try {
      await unlockSecretSource(source.name, inputs);
      closeUnlock();
      onToast({
        title: t('settings.agentSecrets.unlocked', {
          source: sourceLabel(source),
        }),
        variant: 'success',
      });
      onChanged();
    } catch (failure) {
      const message = `${t('settings.agentSecrets.unlockError')} ${failure?.message ?? ''}`;
      if (unlocking) unlockError = message;
      else rowError = message;
    } finally {
      busySource = '';
    }
  }

  function openUnlock(source) {
    if (!(source.unlock_fields ?? []).length) {
      void unlock(source, {});
      return;
    }
    unlocking = source;
    unlockInputs = {};
    unlockSubmitted = false;
    unlockError = '';
  }

  function closeUnlock() {
    unlocking = null;
    unlockInputs = {};
    unlockSubmitted = false;
    unlockError = '';
  }

  function submitUnlock(event) {
    event.preventDefault();
    if (!unlocking || busySource) return;
    unlockSubmitted = true;
    if (unlockMissing.length > 0) return;
    void unlock(unlocking, { ...unlockInputs });
  }

  async function lock(source) {
    busySource = source.name;
    rowError = '';
    try {
      await lockSecretSource(source.name);
      onToast({
        title: t('settings.agentSecrets.locked', {
          source: sourceLabel(source),
        }),
        variant: 'success',
      });
      onChanged();
    } catch (failure) {
      rowError = `${t('settings.agentSecrets.lockError')} ${failure?.message ?? ''}`;
    } finally {
      busySource = '';
    }
  }

  function openBrowse(source) {
    browsing = source;
    listing = null;
    container = '';
    void loadEntries();
  }

  function closeBrowse() {
    browseGeneration += 1;
    browsing = null;
    listing = null;
    browseError = '';
    browseLoading = false;
  }

  async function loadEntries() {
    const source = browsing;
    if (!source) return;
    const generation = ++browseGeneration;
    browseLoading = true;
    browseError = '';
    try {
      const result = await listSecretSourceEntries(source.name, container);
      if (generation !== browseGeneration) return;
      listing = {
        containers: Array.isArray(result?.containers)
          ? result.containers
          : (listing?.containers ?? []),
        entries: Array.isArray(result?.entries) ? result.entries : [],
      };
    } catch (failure) {
      if (generation === browseGeneration) {
        browseError = `${t('settings.agentSecrets.entriesError')} ${failure?.message ?? ''}`;
      }
    } finally {
      if (generation === browseGeneration) browseLoading = false;
    }
  }

  function bind(entry) {
    const source = browsing;
    closeBrowse();
    onBind({ source, entry });
  }
</script>

<div class="s-subhead">
  <h4 class="s-subhead__title">{t('settings.agentSecrets.sourcesTitle')}</h4>
  <p class="s-subhead__desc">{t('settings.agentSecrets.sourcesHelp')}</p>
</div>

{#if rowError}
  <Banner variant="error" role="alert">{rowError}</Banner>
{/if}

{#if sources.length === 0}
  <EmptyState
    density="compact"
    description={t('settings.agentSecrets.sourcesEmpty')}
  />
{:else}
  <div
    class="s-group"
    role="list"
    aria-label={t('settings.agentSecrets.sourcesTitle')}
  >
    {#each sources as source (source.name)}
      {@const chip = STATE_CHIPS[source.state] ?? STATE_CHIPS.unavailable}
      <div
        class="s-entity"
        role="listitem"
        data-secret-source={source.name}
        data-secret-source-state={source.state}
      >
        <div class="s-entity__head">
          <div class="s-row-info">
            <div class="s-row-label">{sourceLabel(source)}</div>
            {#if source.reason}
              <div class="s-row-desc">{source.reason}</div>
            {/if}
          </div>
          <div class="s-entity__end">
            <StatusChip variant={chip.variant}>{chip.label()}</StatusChip>
            {#if source.state === 'locked'}
              <Button
                variant="secondary"
                ariaLabel={t('settings.agentSecrets.unlockAria', {
                  source: sourceLabel(source),
                })}
                loading={busySource === source.name}
                disabled={Boolean(busySource)}
                onClick={() => openUnlock(source)}
              >
                {t('settings.agentSecrets.unlock')}
              </Button>
            {:else if source.state === 'available'}
              <Button
                variant="secondary"
                ariaLabel={t('settings.agentSecrets.browseAria', {
                  source: sourceLabel(source),
                })}
                disabled={Boolean(busySource)}
                onClick={() => openBrowse(source)}
              >
                {t('settings.agentSecrets.browse')}
              </Button>
              {#if (source.unlock_fields ?? []).length > 0}
                <Button
                  variant="tertiary"
                  ariaLabel={t('settings.agentSecrets.lockAria', {
                    source: sourceLabel(source),
                  })}
                  loading={busySource === source.name}
                  disabled={Boolean(busySource)}
                  onClick={() => lock(source)}
                >
                  {t('settings.agentSecrets.lock')}
                </Button>
              {/if}
            {/if}
          </div>
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if unlocking}
  <Modal
    title={t('settings.agentSecrets.unlockTitle', {
      source: sourceLabel(unlocking),
    })}
    closeDisabled={Boolean(busySource)}
    onClose={closeUnlock}
  >
    {#snippet body()}
      <form id={UNLOCK_FORM_ID} novalidate onsubmit={submitUnlock}>
        <div class="modal-body">
          {#if unlockError}
            <Banner variant="error" role="alert">{unlockError}</Banner>
          {/if}
          {#each unlocking.unlock_fields as field (field.name)}
            <FormField
              controlId={`${componentId}-unlock-${field.name}`}
              label={field.label || field.name}
              error={unlockMissing.includes(field.name)
                ? t('secrets.field.required')
                : ''}
              required={field.required !== false}
              full
            >
              {#snippet children(control)}
                {#if field.concealed !== false}
                  <SecretFieldInput
                    id={control.controlId}
                    describedBy={control.describedBy}
                    invalid={control.invalid}
                    value={unlockInputs[field.name] ?? ''}
                    disabled={Boolean(busySource)}
                    onInput={(next) => {
                      unlockInputs = { ...unlockInputs, [field.name]: next };
                      unlockError = '';
                    }}
                  />
                {:else}
                  <TextField
                    id={control.controlId}
                    variant="modal"
                    autocomplete="off"
                    aria-describedby={control.describedBy}
                    invalid={control.invalid}
                    value={unlockInputs[field.name] ?? ''}
                    disabled={Boolean(busySource)}
                    onInput={(next) => {
                      unlockInputs = { ...unlockInputs, [field.name]: next };
                      unlockError = '';
                    }}
                  />
                {/if}
              {/snippet}
            </FormField>
          {/each}
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={Boolean(busySource)}
        onClick={closeUnlock}
      >
        {t('common.cancel')}
      </Button>
      <Button
        type="submit"
        form={UNLOCK_FORM_ID}
        variant="primary"
        loading={Boolean(busySource)}
      >
        {t('settings.agentSecrets.unlock')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if browsing}
  <Modal
    title={t('settings.agentSecrets.browseTitle', {
      source: sourceLabel(browsing),
    })}
    description={t('settings.agentSecrets.browseHelp')}
    size="lg"
    onClose={closeBrowse}
  >
    {#snippet body()}
      <div class="modal-body">
        {#if browseError}
          <Banner variant="error" role="alert">{browseError}</Banner>
        {/if}
        {#if (listing?.containers ?? []).length > 0}
          <FormField
            controlId={`${componentId}-container`}
            label={t('settings.agentSecrets.container')}
            full
          >
            {#snippet children(control)}
              <Dropdown
                id={control.controlId}
                ariaLabelledby={control.labelId}
                value={container}
                options={containerOptions}
                disabled={browseLoading}
                onValueChange={(next) => {
                  container = next;
                  void loadEntries();
                }}
              />
            {/snippet}
          </FormField>
        {/if}
        {#if browseLoading && !listing}
          <Banner variant="neutral">{t('common.loading')}</Banner>
        {:else if listing && listing.entries.length === 0}
          <EmptyState
            density="compact"
            description={t('settings.agentSecrets.entriesEmpty')}
          />
        {:else if listing}
          <div
            class="s-group"
            role="list"
            aria-label={t('settings.agentSecrets.entriesLabel')}
          >
            {#each listing.entries as entry (entry.reference)}
              <div
                class="s-entity"
                role="listitem"
                data-source-entry={entry.reference}
              >
                <div class="s-entity__head">
                  <div class="s-row-info">
                    <div class="s-row-label">{entry.title}</div>
                    <div class="s-row-desc">
                      {[
                        kindLabel(entry.kind),
                        entry.container,
                        (entry.fields ?? [])
                          .map((field) => field.name)
                          .join(', '),
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </div>
                  </div>
                  <div class="s-entity__end">
                    <Button
                      variant="secondary"
                      ariaLabel={t('settings.agentSecrets.useEntryAria', {
                        title: entry.title,
                      })}
                      onClick={() => bind(entry)}
                    >
                      {t('settings.agentSecrets.useEntry')}
                    </Button>
                  </div>
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={closeBrowse}>
        {t('common.close')}
      </Button>
    {/snippet}
  </Modal>
{/if}
