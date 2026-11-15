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
    reloadDesktopExtensions,
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
  let draft = $state({ name: '' });
  let loadError = $state(false);
  let saveError = $state('');
  let saving = $state(false);
  let reloading = $state(false);
  let disposed = false;
  let revision = 0;

  const values = (next) => ({
    name: next.name,
    ...next.capabilities,
  });
  const capabilityLabel = (capability) => {
    const labels = {
      terminal: t('computers.shareTerminal'),
      computer_use: t('computers.shareComputerUse'),
    };
    return Object.hasOwn(labels, capability.id)
      ? labels[capability.id]
      : capability.label;
  };
  const capabilityDescription = (capability) => {
    const descriptions = {
      terminal: t('computers.shareTerminalHelp'),
      computer_use: t('computers.shareComputerUseHelp'),
    };
    return Object.hasOwn(descriptions, capability.id)
      ? descriptions[capability.id]
      : capability.description;
  };
  const snapshot = () => ({ ...draft, name: draft.name.trim() });
  const changed = () =>
    baseline !== null &&
    Object.entries(snapshot()).some(([key, value]) => value !== baseline[key]);

  function applyStatus(next, submitted = null) {
    const nextValues = values(next);
    const old = submitted ?? baseline;
    const pending = Object.fromEntries(
      Object.entries(draft).filter(
        ([key, value]) =>
          old && Object.hasOwn(nextValues, key) && value !== old[key],
      ),
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

  async function reload() {
    if (reloading || status.reloading) return;
    reloading = true;
    saveError = '';
    try {
      await autosave.participant.runSave('manual');
      if (changed() || disposed) return;
      const next = await reloadDesktopExtensions();
      if (!disposed) {
        revision += 1;
        applyStatus(next);
      }
    } catch (error) {
      if (!disposed && !isDesktopDeclined(error))
        saveError = t('computers.reloadError');
    } finally {
      reloading = false;
    }
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
    {#each status.capability_catalog as capability (capability.id)}
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">{capabilityLabel(capability)}</div>
          {#if capabilityDescription(capability)}<div class="s-row-desc">
              {capabilityDescription(capability)}
            </div>{/if}
          {#if status.capability_errors[capability.id]}<div
              class="s-row-desc"
              role="status"
            >
              {t('computers.capabilityError', {
                reason: status.capability_errors[capability.id],
              })}
            </div>{:else if !status.available[capability.id]}<div
              class="s-row-desc"
            >
              {t('computers.capabilityUnavailable')}
            </div>{/if}
        </div>
        <div class="s-row-control">
          <Toggle
            checked={draft[capability.id]}
            disabled={reloading ||
              status.reloading ||
              (!status.available[capability.id] && !draft[capability.id])}
            ariaLabel={capabilityLabel(capability)}
            onChange={(enabled) => toggle(capability.id, enabled)}
          />
        </div>
      </div>
    {/each}
    {#each status.extensions.filter((extension) => extension.status === 'failed' || extension.capability_errors.length) as extension, index (`${extension.name}:${index}`)}
      <Banner variant="warn"
        >{t('computers.extensionIssue', {
          name: extension.name,
          reason: extension.error || extension.capability_errors.join(', '),
        })}</Banner
      >
    {/each}
    <div class="s-group__block">
      {#if status.reload_supported}
        <Button
          variant="secondary"
          disabled={saving || reloading || status.reloading}
          onClick={reload}
        >
          {reloading || status.reloading
            ? t('computers.reloading')
            : t('computers.reloadExtensions')}
        </Button>
        <p class="s-row-desc">{t('computers.reloadHelp')}</p>
        {#each status.extensions.filter((extension) => ['added', 'updated'].includes(extension.change) || extension.status === 'removed') as extension, index (`reload:${extension.name}:${index}`)}
          <p class="s-row-desc">
            {extension.status === 'removed'
              ? t('computers.extensionRemoved', { name: extension.name })
              : extension.change === 'updated'
                ? t('computers.extensionUpdated', { name: extension.name })
                : t('computers.extensionAdded', { name: extension.name })}
          </p>
        {/each}
      {/if}
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
