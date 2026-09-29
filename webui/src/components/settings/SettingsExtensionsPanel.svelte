<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';

  import SettingsMcpPanel from './SettingsMcpPanel.svelte';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    listExtensions,
    reloadExtensions as reloadExtensionsRequest,
    setExtensionSecret,
    updateSettings,
  } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    scheduleAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    applyExtensionsPanelList,
    buildExtensionsUpdatePayload,
    buildSchemaConfigFromForm,
    buildSchemaFormState,
    describeExtensionWaiting,
    extensionStatusChip,
    hasSettingsSchema,
    extensionCapabilityParts,
  } from '$lib/settingsView.js';
  import { tooltip } from '$lib/tooltip.js';

  const noop = () => {};
  const AUTO_SAVE_DEBOUNCE_MS = 800;

  let { onToast = noop, onError = noop } = $props();
  const uid = $props.id();

  let extensions = $state([]);
  let mcpLoaded = $derived(
    extensions.some(
      (extension) => extension.name === 'mcp' && extension.status === 'loaded',
    ),
  );
  let loading = $state(true);
  let loadError = $state('');
  let reloading = $state(false);
  let actionName = $state('');
  let savingConfigNames = $state([]);
  let formStates = $state({});
  let formFieldErrors = $state({});
  let secretDrafts = $state({});
  let savingSecret = $state('');
  // Per-extension non-secret-config autosave timers, keyed by extension name.
  // Non-secret config joins the standard settings autosave regime (secrets never
  // do); each extension's editable form debounces independently. (SvelteMap per
  // the project's reactivity-safe collection convention, as in App.svelte.)
  const autoSaveTimers = new SvelteMap();
  // Disclosure state: each Extension is one collapsed row; its version,
  // capabilities, override path and configuration form live in its details.
  // The details stay in the DOM so settings search still matches them.
  const expandedNames = new SvelteSet();

  function toggleDetails(extension) {
    if (expandedNames.has(extension.name)) {
      expandedNames.delete(extension.name);
    } else {
      expandedNames.add(extension.name);
    }
  }

  function visibleCapabilityParts(extension) {
    // The loaded MCP Extension's own Tools are its management internals; its
    // connections render as their own sub-topic below the list.
    return extension.name === 'mcp' && extension.status === 'loaded'
      ? []
      : extensionCapabilityParts(extension.capabilities);
  }

  function hasDetails(extension) {
    return (
      Boolean(extension.version) ||
      (extension.status === 'overridden' && Boolean(extension.overriddenBy)) ||
      visibleCapabilityParts(extension).length > 0 ||
      (extension.status !== 'overridden' && hasSettingsSchema(extension))
    );
  }

  let anyDetails = $derived(extensions.some(hasDetails));

  // The row head toggles its details like a Provider row; the chevron button
  // stays the keyboard and screen-reader control.
  function handleHeadClick(event, extension) {
    if (
      !hasDetails(extension) ||
      event.target.closest('button, a, input, select, textarea') ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    toggleDetails(extension);
  }

  let panelBusy = $derived(
    loading ||
      reloading ||
      actionName.length > 0 ||
      savingConfigNames.length > 0 ||
      savingSecret.length > 0,
  );
  const autosaveContext = useAutosaveContext();
  const extensionConfigAutosave = createAutosaveParticipant({
    cancelPending: clearAllAutoSaveTimers,
    getSnapshot: extensionAutosaveSnapshot,
    hasChanges: () => extensions.some(extensionDraftHasChanges),
    save: saveExtensionConfigs,
  });
  const unregisterExtensionConfigAutosave = autosaveContext.register(
    extensionConfigAutosave,
  );

  onMount(() => {
    void loadExtensions();
  });

  onDestroy(() => {
    unregisterExtensionConfigAutosave();
    clearAllAutoSaveTimers();
  });

  function clearAutoSaveTimer(name) {
    const timer = autoSaveTimers.get(name);
    if (timer !== undefined) {
      timer();
      autoSaveTimers.delete(name);
    }
  }

  function clearAllAutoSaveTimers() {
    for (const timer of autoSaveTimers.values()) {
      timer();
    }
    autoSaveTimers.clear();
  }

  // Whether the extension's declared non-secret settings differ from what is
  // persisted — the dirty test that gates autosave. Extensions without a
  // schema expose no configuration surface.
  function extensionConfigDirty(extension) {
    if (!hasSettingsSchema(extension)) {
      return false;
    }
    const built = buildSchemaConfigFromForm(
      extension.settingsSchema,
      formStates[extension.name] ?? {},
    );
    if (!built.ok) {
      return false;
    }
    return !configsMatch(built.config, extension.config);
  }

  function configsMatch(left, right) {
    return (
      JSON.stringify(left ?? {}) ===
      JSON.stringify(right && typeof right === 'object' ? right : {})
    );
  }

  function extensionDraftHasChanges(extension) {
    if (!hasSettingsSchema(extension)) {
      return false;
    }
    return (
      JSON.stringify(formStates[extension.name] ?? {}) !==
      JSON.stringify(
        buildSchemaFormState(extension.settingsSchema, extension.config),
      )
    );
  }

  function extensionAutosaveSnapshot() {
    return extensions.filter(extensionDraftHasChanges).map((extension) => ({
      name: extension.name,
      value: formStates[extension.name] ?? {},
    }));
  }

  // Debounce a non-secret-config autosave for one extension after an edit. A
  // clean or invalid form never schedules a save; a fresh edit resets the timer.
  function scheduleExtensionAutoSave(extension) {
    clearAutoSaveTimer(extension.name);
    if (!extensionConfigDirty(extension)) {
      return;
    }
    const timer = scheduleAutosave(() => {
      autoSaveTimers.delete(extension.name);
      void extensionConfigAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);
    autoSaveTimers.set(extension.name, timer);
  }

  function extensionByName(name) {
    return extensions.find((extension) => extension.name === name) ?? null;
  }

  async function loadExtensions() {
    loading = true;
    loadError = '';
    onError('');
    clearAllAutoSaveTimers();

    try {
      const result = await listExtensions();
      extensions = applyExtensionsPanelList(result);
      formStates = Object.fromEntries(
        extensions
          .filter((extension) => hasSettingsSchema(extension))
          .map((extension) => [
            extension.name,
            buildSchemaFormState(extension.settingsSchema, extension.config),
          ]),
      );
      formFieldErrors = {};
      secretDrafts = {};
    } catch (error) {
      loadError = `${t('settings.loadError')} ${error.message}`;
    } finally {
      loading = false;
    }
  }

  async function reloadExtensions() {
    if (panelBusy) {
      return;
    }

    reloading = true;
    onError('');

    try {
      await reloadExtensionsRequest();
      onToast({
        title: t('settings.extensions.reloadSuccess'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      reloading = false;
    }
  }

  function setFormValue(name, key, value) {
    formStates = {
      ...formStates,
      [name]: { ...(formStates[name] ?? {}), [key]: value },
    };
    if (formFieldErrors[name]?.[key]) {
      const nextForExtension = { ...formFieldErrors[name] };
      delete nextForExtension[key];
      formFieldErrors = { ...formFieldErrors, [name]: nextForExtension };
    }
    const extension = extensionByName(name);
    if (extension) {
      scheduleExtensionAutoSave(extension);
    }
  }

  function setSecretDraft(name, key, value) {
    secretDrafts = {
      ...secretDrafts,
      [name]: { ...(secretDrafts[name] ?? {}), [key]: value },
    };
  }

  async function saveExtensionConfigs() {
    if (panelBusy) {
      return false;
    }

    const changedExtensions = extensions.filter(extensionDraftHasChanges);
    if (changedExtensions.length === 0) {
      return true;
    }

    let invalid = false;
    let hasPersistentChanges = false;
    const nextFormFieldErrors = { ...formFieldErrors };
    const nextConfigs = new SvelteMap();

    for (const extension of changedExtensions) {
      const built = buildSchemaConfigFromForm(
        extension.settingsSchema,
        formStates[extension.name] ?? {},
      );
      if (!built.ok) {
        nextFormFieldErrors[extension.name] = built.errors;
        invalid = true;
        continue;
      }
      delete nextFormFieldErrors[extension.name];
      nextConfigs.set(extension.name, built.config);
      hasPersistentChanges ||= !configsMatch(built.config, extension.config);
    }

    formFieldErrors = nextFormFieldErrors;
    if (invalid) {
      return false;
    }
    if (!hasPersistentChanges) {
      return true;
    }

    savingConfigNames = [...nextConfigs.keys()];
    onError('');
    const nextExtensions = extensions.map((extension) =>
      nextConfigs.has(extension.name)
        ? { ...extension, config: nextConfigs.get(extension.name) }
        : extension,
    );

    try {
      await updateSettings(buildExtensionsUpdatePayload(nextExtensions));
      // Update the persisted baseline without unmounting the form or replacing
      // drafts (including another extension edited during this request).
      extensions = nextExtensions;
      return true;
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
      return false;
    } finally {
      savingConfigNames = [];
    }
  }

  async function saveSecret(extension, field, value) {
    if (panelBusy) {
      return;
    }

    savingSecret = `${extension.name}:${field.key}`;
    onError('');

    try {
      await setExtensionSecret({
        name: extension.name,
        key: field.key,
        value,
      });
      onToast({
        title:
          value === ''
            ? t('settings.extensions.secretCleared')
            : t('settings.extensions.secretSaved'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      savingSecret = '';
    }
  }

  async function toggleExtension(extension) {
    if (panelBusy) {
      return;
    }

    actionName = extension.name;
    onError('');

    const payload = buildExtensionsUpdatePayload(extensions, {
      name: extension.name,
      disabled: !extension.disabled,
    });

    try {
      await updateSettings(payload);
      onToast({
        title: extension.disabled
          ? t('settings.extensions.enableSuccess')
          : t('settings.extensions.disableSuccess'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      actionName = '';
    }
  }
</script>

<div class="s-group-toolbar s-list-toolbar">
  {#if !loading && !loadError}
    <span class="s-group-toolbar__meta">
      {t('settings.extensions.count', {
        count: extensions.length,
      })}
    </span>
  {/if}
  <div class="s-group-toolbar__actions s-list-toolbar__end">
    <Button variant="tertiary" disabled={panelBusy} onClick={reloadExtensions}>
      {t('settings.extensions.reload')}
    </Button>
    <InfoHint
      ariaLabel={t('settings.extensions.reloadInfoAria')}
      text={t('settings.extensions.reloadHelp')}
    />
  </div>
</div>

{#if loading}
  <Banner variant="neutral">
    {t('common.loading')}
  </Banner>
{:else if loadError}
  <Banner variant="error" role="alert">
    <span>{loadError}</span>
    <Button variant="secondary" disabled={panelBusy} onClick={loadExtensions}>
      {t('common.retry')}
    </Button>
  </Banner>
{:else if extensions.length === 0}
  <EmptyState density="compact" description={t('settings.extensions.empty')} />
{:else}
  <!-- One row per Extension: its name, a one-line description, a status chip
       only when the state needs attention, its switch and a details
       disclosure. Failures, missing setup and capability warnings stay
       visible; version, capabilities and configuration live in the details. -->
  <div class="s-group s-ext-list">
    {#each extensions as extension, index (extension.name)}
      {@const rowBusy =
        loading ||
        reloading ||
        actionName.length > 0 ||
        savingSecret.length > 0}
      {@const isOverridden = extension.status === 'overridden'}
      {@const capabilityParts = visibleCapabilityParts(extension)}
      {@const waiting = describeExtensionWaiting(extension)}
      {@const chip = extensionStatusChip(extension)}
      {@const withDetails = hasDetails(extension)}
      {@const expanded = withDetails && expandedNames.has(extension.name)}
      <div class="s-ext-card s-entity">
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
        <div
          class="s-entity__head"
          class:s-entity__head--toggle={withDetails}
          onclick={(event) => handleHeadClick(event, extension)}
        >
          <div class="s-row-info">
            <div
              class="s-row-label s-ext-name"
              class:s-ext-name--off={extension.disabled}
            >
              {extension.name}
            </div>
            {#if extension.description}
              <div
                class="s-row-desc s-ext-desc"
                class:s-ext-desc--full={expanded}
                use:tooltip={{
                  text: extension.description,
                  whenTruncated: true,
                }}
              >
                {extension.description}
              </div>
            {/if}
            {#if extension.error}
              <div class="s-row-desc s-ext-error-text">
                {t('settings.extensions.error')}: {extension.error}
              </div>
            {/if}
            {#if !isOverridden && waiting?.waitingFor}
              <div class="s-row-desc s-ext-waiting">
                <span class="s-ext-waiting-for">{waiting.waitingFor}</span>
              </div>
            {/if}
            {#each extension.capabilityErrors as capabilityError (capabilityError)}
              <div class="s-row-desc s-ext-warning">
                {t('settings.extensions.warning')}: {capabilityError}
              </div>
            {/each}
          </div>

          <div class="s-entity__end">
            {#if chip}
              <StatusChip variant={chip.variant}>{chip.label}</StatusChip>
            {/if}
            {#if !isOverridden}
              <Toggle
                checked={!extension.disabled}
                disabled={rowBusy}
                ariaLabel={t('settings.extensions.enableAria', {
                  name: extension.name,
                })}
                onChange={() => toggleExtension(extension)}
              />
            {/if}
            {#if withDetails}
              <Button
                variant="tertiary"
                icon
                class="s-disclosure-btn"
                aria-controls={`${uid}-details-${index}`}
                ariaLabel={t('settings.extensions.detailsAria', {
                  name: extension.name,
                })}
                aria-expanded={expanded}
                onClick={() => toggleDetails(extension)}
              >
                <span
                  class="disclosure-chevron"
                  class:disclosure-chevron--open={expanded}
                  aria-hidden="true"
                ></span>
              </Button>
            {:else if anyDetails}
              <!-- Keeps the switches aligned with rows that have details. -->
              <span class="s-disclosure-spacer" aria-hidden="true"></span>
            {/if}
          </div>
        </div>

        {#if withDetails}
          <div
            id={`${uid}-details-${index}`}
            class="s-disclosure-sub s-ext-details"
            hidden={!expanded}
          >
            {#if extension.version || capabilityParts.length > 0}
              <div class="s-ext-facts">
                {#if extension.version}
                  <Badge variant="neutral">v{extension.version}</Badge>
                {/if}
                {#if capabilityParts.length > 0}
                  <div class="s-row-desc s-ext-capabilities">
                    {#each capabilityParts as part, index (part.label)}
                      {#if index > 0}<span
                          class="s-ext-capabilities__sep"
                          aria-hidden="true">·</span
                        >{/if}<span class="s-ext-capabilities__part"
                        >{part.value
                          ? `${part.label}: `
                          : part.label}{#if part.value}<code
                            class="s-ext-capabilities__value">{part.value}</code
                          >{/if}</span
                      >
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
            {#if isOverridden && extension.overriddenBy}
              <div class="s-row-desc s-ext-overridden-text">
                {t('settings.extensions.overriddenBy', {
                  path: extension.overriddenBy,
                })}
              </div>
            {/if}
            {#if !isOverridden && hasSettingsSchema(extension)}
              <div class="s-ext-schema">
                {#each extension.settingsSchema as field (field.key)}
                  {@const secretSaving =
                    savingSecret === `${extension.name}:${field.key}`}
                  {@const fieldControlId = `extension-${extension.name}-${field.key}`}
                  <FormField
                    controlId={fieldControlId}
                    full
                    class="s-ext-schema-field"
                    label={field.label}
                    help={field.description ?? ''}
                    error={formFieldErrors[extension.name]?.[field.key]
                      ? t('settings.extensions.numberInvalid')
                      : ''}
                  >
                    {#snippet children(formField)}
                      {#if field.type === 'toggle'}
                        <Toggle
                          id={formField.controlId}
                          checked={formStates[extension.name]?.[field.key] ===
                            true}
                          disabled={rowBusy}
                          ariaLabel={field.label}
                          aria-describedby={formField.describedBy}
                          onChange={(next) =>
                            setFormValue(extension.name, field.key, next)}
                        />
                      {:else if field.type === 'secret'}
                        <form
                          class="s-ext-secret"
                          onsubmit={(event) => {
                            event.preventDefault();
                            saveSecret(
                              extension,
                              field,
                              secretDrafts[extension.name]?.[field.key] ?? '',
                            );
                          }}
                        >
                          <StatusChip variant={field.set ? 'success' : 'warn'}>
                            {field.set
                              ? t('settings.extensions.secretSet')
                              : t('settings.extensions.secretUnset')}
                          </StatusChip>
                          <TextField
                            id={formField.controlId}
                            type="password"
                            autocomplete="off"
                            value={secretDrafts[extension.name]?.[field.key] ??
                              ''}
                            disabled={rowBusy}
                            aria-describedby={formField.describedBy}
                            placeholder={t(
                              'settings.extensions.secretPlaceholder',
                            )}
                            ariaLabel={t('settings.extensions.secretAria', {
                              label: field.label,
                              name: extension.name,
                            })}
                            onInput={(next) =>
                              setSecretDraft(extension.name, field.key, next)}
                          />
                          <div class="s-ext-secret-actions">
                            <Button
                              variant="primary"
                              type="submit"
                              disabled={rowBusy ||
                                !(
                                  secretDrafts[extension.name]?.[field.key] ??
                                  ''
                                )}
                            >
                              {secretSaving
                                ? t('common.saving')
                                : t('settings.extensions.secretSave')}
                            </Button>
                            <Button
                              variant="secondary"
                              disabled={rowBusy || !field.set}
                              onClick={() => saveSecret(extension, field, '')}
                            >
                              {t('settings.extensions.secretClear')}
                            </Button>
                          </div>
                        </form>
                      {:else}
                        <TextField
                          id={formField.controlId}
                          type={field.type === 'number' ? 'number' : 'text'}
                          value={formStates[extension.name]?.[field.key] ?? ''}
                          disabled={rowBusy}
                          invalid={formField.invalid}
                          aria-describedby={formField.describedBy}
                          placeholder={field.default === null ||
                          field.default === undefined
                            ? ''
                            : String(field.default)}
                          ariaLabel={t('settings.extensions.fieldAria', {
                            label: field.label,
                            name: extension.name,
                          })}
                          onInput={(next) =>
                            setFormValue(extension.name, field.key, next)}
                        />
                      {/if}
                    {/snippet}
                  </FormField>
                {/each}
                <div class="s-ext-config-actions">
                  <SaveStatus
                    disabled={rowBusy}
                    saving={savingConfigNames.includes(extension.name)}
                    pending={extensionDraftHasChanges(extension)}
                    onClick={() => extensionConfigAutosave.runSave('manual')}
                  />
                </div>
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <!-- The loaded MCP Extension contributes its connection manager as a
       sub-topic after the Extension list. -->
  {#if mcpLoaded}
    <SettingsMcpPanel />
  {/if}
{/if}
