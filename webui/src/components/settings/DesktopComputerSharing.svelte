<script>
  import { onDestroy, onMount } from 'svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    getComputerSharing,
    isDesktopDeclined,
    onComputerSharing,
    setComputerSharing,
  } from '$lib/desktopBridge.js';
  import { t } from '$lib/i18n.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';

  const componentId = $props.id();
  const nameId = `${componentId}-computer-name`;
  let status = $state(null);
  let baseline = $state(null);
  let draft = $state({ name: '', terminal: false, computer_use: false });
  let loadError = $state(false);
  let saveError = $state('');
  let saving = $state(false);
  let disposed = false;
  let revision = 0;

  const values = (next) => ({
    name: next.name,
    terminal: next.capabilities.terminal,
    computer_use: next.capabilities.computer_use,
  });
  const snapshot = () => ({ ...draft, name: draft.name.trim() });
  const changed = () =>
    baseline !== null &&
    Object.entries(snapshot()).some(([key, value]) => value !== baseline[key]);

  function applyStatus(next, submitted = null) {
    const nextValues = values(next);
    const old = submitted ?? baseline;
    const pending = Object.fromEntries(
      Object.entries(draft).filter(([key, value]) => old && value !== old[key]),
    );
    status = next;
    baseline = nextValues;
    draft = { ...nextValues, ...pending };
  }

  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges: changed,
    save: async () => {
      const submitted = snapshot();
      if (!submitted.name) {
        saveError = t('computers.nameRequired');
        return false;
      }
      const changes = Object.fromEntries(
        Object.entries(submitted).filter(
          ([key, value]) => value !== baseline[key],
        ),
      );
      if (!Object.keys(changes).length) return true;
      saving = true;
      saveError = '';
      try {
        const result = await setComputerSharing(changes);
        if (!disposed) {
          revision += 1;
          applyStatus(result, submitted);
        }
        return true;
      } catch (error) {
        if (disposed) return false;
        if (isDesktopDeclined(error)) {
          applyStatus(status, submitted);
          return true;
        }
        saveError = t('computers.sharingSaveError');
        return false;
      } finally {
        saving = false;
      }
    },
  });
  const unregister = useAutosaveContext().register(autosave.participant);
  onDestroy(() => {
    disposed = true;
    autosave.cancelPendingTimer();
    unregister();
  });

  $effect(() => {
    if (baseline && changed()) autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });

  async function load() {
    const request = ++revision;
    loadError = false;
    try {
      const next = await getComputerSharing();
      if (!disposed && request === revision) applyStatus(next);
    } catch {
      if (!disposed && request === revision) loadError = true;
    }
  }

  function toggle(key, enabled) {
    draft[key] = enabled;
    void autosave.participant.runSave();
  }

  onMount(() => {
    void load();
    return onComputerSharing((next) => {
      revision += 1;
      applyStatus(next);
    });
  });
</script>

{#if loadError}
  <Banner variant="error" role="alert">
    {t('computers.sharingLoadError')}
    <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
  </Banner>
{:else if status}
  <div class="s-group">
    <div class="s-group__block">
      <p class="s-row-desc">{t('computers.sharingHelp')}</p>
      <FormField controlId={nameId} label={t('computers.name')}>
        <TextField
          id={nameId}
          value={draft.name}
          onInput={(name) => (draft.name = name)}
        />
      </FormField>
    </div>
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">{t('computers.shareTerminal')}</div>
        <div class="s-row-desc">{t('computers.shareTerminalHelp')}</div>
        {#if !status.available.terminal}<div class="s-row-desc">
            {t('computers.capabilityUnavailable')}
          </div>{/if}
      </div>
      <div class="s-row-control">
        <Toggle
          checked={draft.terminal}
          disabled={!status.available.terminal && !draft.terminal}
          ariaLabel={t('computers.shareTerminal')}
          onChange={(enabled) => toggle('terminal', enabled)}
        />
      </div>
    </div>
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">{t('computers.shareComputerUse')}</div>
        <div class="s-row-desc">{t('computers.shareComputerUseHelp')}</div>
        {#if !status.available.computer_use}<div class="s-row-desc">
            {t('computers.capabilityUnavailable')}
          </div>{/if}
      </div>
      <div class="s-row-control">
        <Toggle
          checked={draft.computer_use}
          disabled={!status.available.computer_use && !draft.computer_use}
          ariaLabel={t('computers.shareComputerUse')}
          onChange={(enabled) => toggle('computer_use', enabled)}
        />
      </div>
    </div>
    <div class="s-group__block">
      <p class="s-row-desc" role="status">
        {status.connected
          ? t('computers.connectedTo', { server: status.server_url })
          : t('computers.notConnected')}
      </p>
      {#if status.active_terminals > 0}
        <p class="s-row-desc">
          {t('computers.activeTerminals', { count: status.active_terminals })}
        </p>
      {/if}
      {#if status.error_code}<Banner variant="warn"
          >{t('computers.sharingError', { reason: status.error_code })}</Banner
        >{/if}
      {#if saveError}<Banner variant="error" role="alert">{saveError}</Banner
        >{/if}
      <SaveStatus
        {saving}
        pending={changed()}
        onClick={() => autosave.participant.runSave('manual')}
      />
    </div>
  </div>
{/if}
