<script>
  // Add device: the owner types the pairing code a new device shows, checks
  // what the server knows about that request (name, kind, address, time) and
  // approves or denies it. The new device then signs in on its own.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import { approvePairing, denyPairing, getPendingPairing } from '$lib/api.js';
  import { deviceKindLabel, pairingErrorMessage } from '$lib/settingsView.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';

  const noop = () => {};

  let { onClose = noop, onDecided = noop } = $props();

  let code = $state('');
  // The request the typed code names, once the server confirmed it waits.
  let pending = $state(null);
  let busy = $state('');
  let error = $state('');

  const requestedAt = $derived.by(() => {
    const date = new Date(pending?.requested_at ?? '');
    return Number.isNaN(date.getTime())
      ? ''
      : formatDateTimeInApplicationZone(date, activeLocaleTag(), {
          dateStyle: 'medium',
          timeStyle: 'short',
        });
  });

  async function lookUp(event) {
    event.preventDefault();
    const typed = code.trim();
    if (!typed || busy) {
      return;
    }
    busy = 'lookup';
    error = '';
    try {
      pending = await getPendingPairing(typed);
    } catch (failure) {
      error = pairingErrorMessage(failure);
    } finally {
      busy = '';
    }
  }

  async function decide(approved) {
    if (!pending || busy) {
      return;
    }
    busy = approved ? 'approve' : 'deny';
    error = '';
    try {
      const result = approved
        ? await approvePairing(pending.code)
        : await denyPairing(pending.code);
      onDecided({ approved, name: result?.name ?? pending.name });
    } catch (failure) {
      error = pairingErrorMessage(failure);
      busy = '';
    }
  }

  function back() {
    pending = null;
    error = '';
  }
</script>

<Modal
  title={t('settings.access.add.title')}
  description={pending
    ? t('settings.access.add.confirmIntro')
    : t('settings.access.add.intro')}
  labelledById="add-device-title"
  closeDisabled={busy !== ''}
  {onClose}
>
  {#snippet body()}
    {#if !pending}
      <form id="add-device-form" onsubmit={lookUp}>
        <div class="modal-body">
          <FormField
            controlId="add-device-code"
            label={t('settings.access.add.codeLabel')}
            help={t('settings.access.add.codeHelp')}
            {error}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                code
                autocomplete="off"
                autocapitalize="characters"
                spellcheck="false"
                maxlength="20"
                aria-describedby={field.describedBy}
                invalid={field.invalid}
                value={code}
                disabled={busy !== ''}
                placeholder={t('settings.access.add.codePlaceholder')}
                onInput={(next) => {
                  code = next.toUpperCase();
                  error = '';
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {:else}
      <div class="modal-body">
        <dl class="add-device__facts">
          <dt>{t('settings.access.add.name')}</dt>
          <dd>{pending.name}</dd>
          <dt>{t('settings.access.add.kind')}</dt>
          <dd>{deviceKindLabel(pending.kind)}</dd>
          <dt>{t('settings.access.add.address')}</dt>
          <dd class="add-device__mono">
            {pending.client || t('common.unknown')}
          </dd>
          <dt>{t('settings.access.add.requested')}</dt>
          <dd>{requestedAt}</dd>
          <dt>{t('settings.access.add.code')}</dt>
          <dd class="add-device__mono">{pending.code}</dd>
        </dl>
        <Banner variant="warn">{t('settings.access.add.warning')}</Banner>
        {#if error}
          <Banner variant="error" role="alert">{error}</Banner>
        {/if}
      </div>
    {/if}
  {/snippet}
  {#snippet footer()}
    {#if !pending}
      <Button variant="secondary" disabled={busy !== ''} onClick={onClose}>
        {t('common.cancel')}
      </Button>
      <Button
        type="submit"
        form="add-device-form"
        variant="primary"
        disabled={!code.trim()}
        loading={busy === 'lookup'}
      >
        {t('settings.access.add.continue')}
      </Button>
    {:else}
      <div class="modal-footer__start">
        <Button variant="tertiary" disabled={busy !== ''} onClick={back}>
          {t('common.back')}
        </Button>
      </div>
      <Button
        variant="secondary"
        disabled={busy !== '' && busy !== 'deny'}
        loading={busy === 'deny'}
        onClick={() => decide(false)}
      >
        {t('settings.access.add.deny')}
      </Button>
      <Button
        variant="primary"
        disabled={busy !== '' && busy !== 'approve'}
        loading={busy === 'approve'}
        onClick={() => decide(true)}
      >
        {t('settings.access.add.approve')}
      </Button>
    {/if}
  {/snippet}
</Modal>

<style>
  .add-device__facts {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 6px 16px;
    margin: 0;
  }

  .add-device__facts dt {
    color: var(--text-lo);
  }

  .add-device__facts dd {
    margin: 0;
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }

  .add-device__mono {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }
</style>
