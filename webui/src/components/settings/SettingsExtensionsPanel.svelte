<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';

  import SettingsMcpPanel from './SettingsMcpPanel.svelte';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
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
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    applyExtensionsPanelList,
    buildExtensionsUpdatePayload,
    buildSchemaConfigFromForm,
    buildSchemaFormState,
    describeExtensionWaiting,
    extensionStatusChipVariant,
    hasSettingsSchema,
    summarizeExtensionCapabilities,
  } from '$lib/settingsView.js';

  const noop = () => {};
  const AUTO_SAVE_DEBOUNCE_MS = 800;

  let { onToast = noop, onError = noop } = $props();

  let extensions = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let reloading = $state(false);
  let actionName = $state('');
  let savingConfigName = $state('');
  let formStates = $state({});
  let formFieldErrors = $state({});
  let secretDrafts = $state({});
  let savingSecret = $state('');
  // Per-extension non-secret-config autosave timers, keyed by extension name.
  // Non-secret config joins the standard settings autosave regime (secrets never
  // do); each extension's editable form debounces independently. (SvelteMap per
  // the project's reactivity-safe collection convention, as in App.svelte.)
  const autoSaveTimers = new SvelteMap();
  // Disclosure state: per-extension config forms start collapsed; the sub
  // stays in the DOM so settings search still matches field labels.
  const expandedConfigNames = new SvelteSet();

  function toggleConfigDetails(extension) {
    if (expandedConfigNames.has(extension.name)) {
      expandedConfigNames.delete(extension.name);
    } else {
      expandedConfigNames.add(extension.name);
    }
  }

  let panelBusy = $derived(
    loading ||
      reloading ||
      actionName.length > 0 ||
      savingConfigName.length > 0 ||
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
      clearTimeout(timer);
      autoSaveTimers.delete(name);
    }
  }

  function clearAllAutoSaveTimers() {
    for (const timer of autoSaveTimers.values()) {
      clearTimeout(timer);
    }
    autoSaveTimers.clear();
  }

  // Whether the extension's declared non-secret settings differ from what is
  // persisted — the dirty test that gates autosave and the "Already saved"
  // toast. Extensions without a schema expose no configuration surface.
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
    const timer = setTimeout(() => {
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
      loadError = `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`;
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
        title: t('settings.extensions.reloadSuccess', 'Extensions reloaded.'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
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

  // Explicit Save on the schema form: a clean form confirms trust with the
  // shared "Already saved" toast (the same behavior as every autosave surface);
  // a dirty one persists immediately, cancelling the pending debounce.
  function handleManualSchemaConfigSave(extension) {
    if (panelBusy) {
      return;
    }
    if (!extensionConfigDirty(extension)) {
      // A form with an invalid field is not "already saved" — surface its
      // validation errors instead of a success toast.
      const built = buildSchemaConfigFromForm(
        extension.settingsSchema,
        formStates[extension.name] ?? {},
      );
      if (!built.ok) {
        formFieldErrors = {
          ...formFieldErrors,
          [extension.name]: built.errors,
        };
        return;
      }
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }
    clearAutoSaveTimer(extension.name);
    void extensionConfigAutosave.runSave('manual');
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

    savingConfigName = changedExtensions[0].name;
    onError('');
    const nextExtensions = extensions.map((extension) =>
      nextConfigs.has(extension.name)
        ? { ...extension, config: nextConfigs.get(extension.name) }
        : extension,
    );

    try {
      await updateSettings(buildExtensionsUpdatePayload(nextExtensions));
      onToast({
        title: t(
          'settings.extensions.settingsSaveSuccess',
          'Extension settings saved.',
        ),
        variant: 'success',
      });
      await loadExtensions();
      return true;
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
    } finally {
      savingConfigName = '';
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
            ? t('settings.extensions.secretCleared', 'Secret cleared.')
            : t('settings.extensions.secretSaved', 'Secret saved.'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      savingSecret = '';
    }
  }

  function statusLabel(status) {
    if (status === 'loaded') {
      return t('settings.extensions.statusLoaded', 'Loaded');
    }
    if (status === 'failed') {
      return t('settings.extensions.statusFailed', 'Failed');
    }
    if (status === 'disabled') {
      return t('settings.extensions.statusDisabled', 'Disabled');
    }
    if (status === 'overridden') {
      return t('settings.extensions.statusOverridden', 'Overridden');
    }
    return status;
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
          ? t('settings.extensions.enableSuccess', 'Extension enabled.')
          : t('settings.extensions.disableSuccess', 'Extension disabled.'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      actionName = '';
    }
  }
</script>

<div class="s-list-head">
  <span class="s-list-head-info">
    {#if !loading && !loadError}
      {t('settings.extensions.count', '{count} discovered', {
        count: extensions.length,
      })}
    {/if}
  </span>
  <div class="s-list-head-actions">
    <Button variant="secondary" disabled={panelBusy} onClick={reloadExtensions}>
      {t('settings.extensions.reload', 'Reload extensions')}
    </Button>
    <InfoHint
      ariaLabel={t(
        'settings.extensions.reloadInfoAria',
        'About reloading extensions',
      )}
      text={t(
        'settings.extensions.reloadHelp',
        'Rebuilds all extensions from disk — picks up code edits, new and removed extensions.',
      )}
    />
  </div>
</div>

{#if loading}
  <Banner variant="neutral">
    {t('common.loading', 'Loading…')}
  </Banner>
{:else if loadError}
  <Banner variant="error" role="alert">
    <span>{loadError}</span>
    <Button variant="secondary" disabled={panelBusy} onClick={loadExtensions}>
      {t('common.retry', 'Retry')}
    </Button>
  </Banner>
{:else if extensions.length === 0}
  <EmptyState
    density="compact"
    description={t('settings.extensions.empty', 'No extensions discovered.')}
  />
{:else}
  <div class="s-ext-list">
    {#each extensions as extension (extension.name)}
      {@const rowBusy = panelBusy}
      {@const isOverridden = extension.status === 'overridden'}
      {@const capabilities =
        extension.name === 'mcp' && extension.status === 'loaded'
          ? ''
          : summarizeExtensionCapabilities(extension.capabilities, t)}
      {@const waiting = describeExtensionWaiting(extension, t)}
      <div class="s-ext-card">
        <div class="s-ext-head">
          <div class="s-row-info">
            <div class="s-ext-name-row">
              <span class="s-row-label s-ext-name">{extension.name}</span>
              <StatusChip
                variant={extensionStatusChipVariant(extension.status)}
              >
                {statusLabel(extension.status)}
              </StatusChip>
              {#if extension.version}
                <Badge variant="neutral">v{extension.version}</Badge>
              {/if}
            </div>
            {#if extension.description}
              <div class="s-row-desc">{extension.description}</div>
            {/if}
            {#if extension.error}
              <div class="s-row-desc s-ext-error-text">
                {t('settings.extensions.error', 'Error')}: {extension.error}
              </div>
            {/if}
            {#if isOverridden && extension.overriddenBy}
              <div class="s-row-desc s-ext-overridden-text">
                {t(
                  'settings.extensions.overriddenBy',
                  'Overridden by your copy at {path}',
                  { path: extension.overriddenBy },
                )}
              </div>
            {/if}
            {#if !isOverridden && waiting}
              <div class="s-row-desc s-ext-waiting">
                {waiting.hint}
                {#if waiting.waitingFor}
                  <span class="s-ext-waiting-for">{waiting.waitingFor}</span>
                {/if}
              </div>
            {/if}
            {#if capabilities}
              <div class="s-row-desc s-ext-capabilities">{capabilities}</div>
            {/if}
            {#each extension.capabilityErrors as capabilityError (capabilityError)}
              <div class="s-row-desc s-ext-warning">
                {t('settings.extensions.warning', 'Warning')}: {capabilityError}
              </div>
            {/each}
          </div>

          {#if !isOverridden}
            <div class="s-ext-controls">
              <Button
                variant="secondary"
                disabled={rowBusy}
                ariaLabel={extension.disabled
                  ? t(
                      'settings.extensions.enableAria',
                      'Enable extension {name}',
                      {
                        name: extension.name,
                      },
                    )
                  : t(
                      'settings.extensions.disableAria',
                      'Disable extension {name}',
                      { name: extension.name },
                    )}
                onClick={() => toggleExtension(extension)}
              >
                {extension.disabled
                  ? t('settings.extensions.enable', 'Enable')
                  : t('settings.extensions.disable', 'Disable')}
              </Button>
              {#if hasSettingsSchema(extension)}
                <Button
                  variant="tertiary"
                  icon
                  class="s-disclosure-btn"
                  ariaLabel={t(
                    'settings.extensions.configToggleAria',
                    'Configuration for extension {name}',
                    { name: extension.name },
                  )}
                  aria-expanded={expandedConfigNames.has(extension.name)}
                  onClick={() => toggleConfigDetails(extension)}
                >
                  ▸
                </Button>
              {/if}
            </div>
          {/if}
        </div>

        {#if extension.name === 'mcp' && extension.status === 'loaded'}
          <SettingsMcpPanel />
        {/if}

        {#if !isOverridden && hasSettingsSchema(extension)}
          <div
            class="s-disclosure-sub"
            hidden={!expandedConfigNames.has(extension.name)}
          >
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
                    ? t(
                        'settings.extensions.numberInvalid',
                        'Enter a valid number.',
                      )
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
                            ? t('settings.extensions.secretSet', 'Set')
                            : t('settings.extensions.secretUnset', 'Not set')}
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
                            'Enter a new value',
                          )}
                          ariaLabel={t(
                            'settings.extensions.secretAria',
                            'Secret {label} for extension {name}',
                            { label: field.label, name: extension.name },
                          )}
                          onInput={(next) =>
                            setSecretDraft(extension.name, field.key, next)}
                        />
                        <div class="s-ext-secret-actions">
                          <Button
                            variant="primary"
                            type="submit"
                            disabled={rowBusy ||
                              !(
                                secretDrafts[extension.name]?.[field.key] ?? ''
                              )}
                          >
                            {secretSaving
                              ? t('common.saving', 'Saving…')
                              : t('settings.extensions.secretSave', 'Save')}
                          </Button>
                          <Button
                            variant="secondary"
                            disabled={rowBusy || !field.set}
                            onClick={() => saveSecret(extension, field, '')}
                          >
                            {t('settings.extensions.secretClear', 'Clear')}
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
                        ariaLabel={t(
                          'settings.extensions.fieldAria',
                          '{label} for extension {name}',
                          { label: field.label, name: extension.name },
                        )}
                        onInput={(next) =>
                          setFormValue(extension.name, field.key, next)}
                      />
                    {/if}
                  {/snippet}
                </FormField>
              {/each}
              <div class="s-ext-config-actions">
                <Button
                  variant="primary"
                  disabled={rowBusy}
                  onClick={() => handleManualSchemaConfigSave(extension)}
                >
                  {savingConfigName === extension.name
                    ? t('common.saving', 'Saving…')
                    : t('settings.extensions.saveSettings', 'Save settings')}
                </Button>
              </div>
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}
