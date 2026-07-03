<script>
  import { onMount } from 'svelte';

  import Button from '../ui/Button.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    applyExtensionsPanelList,
    buildExtensionsUpdatePayload,
    buildSchemaConfigFromForm,
    buildSchemaFormState,
    describeExtensionWaiting,
    extensionStatusChipVariant,
    formatExtensionConfig,
    hasSettingsSchema,
    parseExtensionConfigDraft,
    summarizeExtensionCapabilities,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let { onToast = noop } = $props();

  let extensions = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let actionError = $state('');
  let reloading = $state(false);
  let actionName = $state('');
  let savingConfigName = $state('');
  let configDrafts = $state({});
  let configErrors = $state({});
  let formStates = $state({});
  let formFieldErrors = $state({});
  let secretDrafts = $state({});
  let savingSecret = $state('');

  let panelBusy = $derived(
    loading ||
      reloading ||
      actionName.length > 0 ||
      savingConfigName.length > 0 ||
      savingSecret.length > 0,
  );

  onMount(() => {
    void loadExtensions();
  });

  async function loadExtensions() {
    loading = true;
    loadError = '';
    actionError = '';

    try {
      const result = await rpc('extensions.list');
      extensions = applyExtensionsPanelList(result);
      configDrafts = Object.fromEntries(
        extensions.map((extension) => [
          extension.name,
          formatExtensionConfig(extension.config),
        ]),
      );
      formStates = Object.fromEntries(
        extensions
          .filter((extension) => hasSettingsSchema(extension))
          .map((extension) => [
            extension.name,
            buildSchemaFormState(extension.settingsSchema, extension.config),
          ]),
      );
      configErrors = {};
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
    actionError = '';

    try {
      await rpc('extensions.reload');
      onToast({
        title: t('settings.extensions.reloadSuccess', 'Extensions reloaded.'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      actionError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
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
  }

  function setSecretDraft(name, key, value) {
    secretDrafts = {
      ...secretDrafts,
      [name]: { ...(secretDrafts[name] ?? {}), [key]: value },
    };
  }

  async function saveSchemaConfig(extension) {
    if (panelBusy) {
      return;
    }

    const built = buildSchemaConfigFromForm(
      extension.settingsSchema,
      formStates[extension.name] ?? {},
    );
    if (!built.ok) {
      formFieldErrors = { ...formFieldErrors, [extension.name]: built.errors };
      return;
    }

    savingConfigName = extension.name;
    actionError = '';

    const payload = buildExtensionsUpdatePayload(extensions, {
      name: extension.name,
      config: built.config,
    });

    try {
      await rpc('settings.update', payload);
      onToast({
        title: t(
          'settings.extensions.configSaveSuccess',
          'Extension config saved.',
        ),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      actionError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      savingConfigName = '';
    }
  }

  async function saveSecret(extension, field, value) {
    if (panelBusy) {
      return;
    }

    savingSecret = `${extension.name}:${field.key}`;
    actionError = '';

    try {
      await rpc('extensions.set_secret', {
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
      actionError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
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

  function setConfigDraft(name, value) {
    configDrafts = { ...configDrafts, [name]: value };
    if (configErrors[name]) {
      const next = { ...configErrors };
      delete next[name];
      configErrors = next;
    }
  }

  async function toggleExtension(extension) {
    if (panelBusy) {
      return;
    }

    actionName = extension.name;
    actionError = '';

    const payload = buildExtensionsUpdatePayload(extensions, {
      name: extension.name,
      disabled: !extension.disabled,
    });

    try {
      await rpc('settings.update', payload);
      onToast({
        title: extension.disabled
          ? t('settings.extensions.enableSuccess', 'Extension enabled.')
          : t('settings.extensions.disableSuccess', 'Extension disabled.'),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      actionError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      actionName = '';
    }
  }

  async function saveExtensionConfig(extension) {
    if (panelBusy) {
      return;
    }

    const parsed = parseExtensionConfigDraft(configDrafts[extension.name]);
    if (!parsed.ok) {
      configErrors = {
        ...configErrors,
        [extension.name]: t(
          'settings.extensions.configInvalid',
          'Config must be a JSON object.',
        ),
      };
      return;
    }

    savingConfigName = extension.name;
    actionError = '';

    const payload = buildExtensionsUpdatePayload(extensions, {
      name: extension.name,
      config: parsed.value,
    });

    try {
      await rpc('settings.update', payload);
      onToast({
        title: t(
          'settings.extensions.configSaveSuccess',
          'Extension config saved.',
        ),
        variant: 'success',
      });
      await loadExtensions();
    } catch (error) {
      actionError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      savingConfigName = '';
    }
  }
</script>

<div class="s-row s-row--stacked s-row--channels-header">
  <div class="s-row-control">
    <div class="s-row-actions s-row-actions--channel-header">
      <Button variant="secondary" disabled={panelBusy} onClick={loadExtensions}>
        {t('common.refresh', 'Refresh')}
      </Button>
      <Button
        variant="secondary"
        disabled={panelBusy}
        onClick={reloadExtensions}
      >
        {t('settings.extensions.reload', 'Reload extensions')}
      </Button>
    </div>
  </div>
</div>

{#if actionError}
  <div class="s-feedback s-feedback--error">{actionError}</div>
{/if}

{#if loading}
  <div class="s-feedback s-feedback--neutral">
    {t('common.loading', 'Loading…')}
  </div>
{:else if loadError}
  <div class="s-feedback s-feedback--error">{loadError}</div>
{:else if extensions.length === 0}
  <div class="s-feedback s-feedback--neutral">
    {t('settings.extensions.empty', 'No extensions discovered.')}
  </div>
{:else}
  <div class="s-ext-list">
    {#each extensions as extension (extension.name)}
      {@const rowBusy = panelBusy}
      {@const isOverridden = extension.status === 'overridden'}
      {@const capabilities = summarizeExtensionCapabilities(
        extension.capabilities,
        t,
      )}
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
                <span class="s-ext-version">v{extension.version}</span>
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
            </div>
          {/if}
        </div>

        {#if !isOverridden && hasSettingsSchema(extension)}
          <div class="s-ext-schema">
            {#each extension.settingsSchema as field (field.key)}
              {@const secretSaving =
                savingSecret === `${extension.name}:${field.key}`}
              <div class="s-field s-field--full s-ext-schema-field">
                <span class="s-field-label">{field.label}</span>
                {#if field.description}
                  <span class="s-field-hint">{field.description}</span>
                {/if}
                {#if field.type === 'toggle'}
                  <label class="s-ext-toggle">
                    <input
                      type="checkbox"
                      checked={formStates[extension.name]?.[field.key] === true}
                      disabled={rowBusy}
                      onchange={(event) =>
                        setFormValue(
                          extension.name,
                          field.key,
                          event.currentTarget.checked,
                        )}
                    />
                  </label>
                {:else if field.type === 'secret'}
                  <div class="s-ext-secret">
                    <StatusChip variant={field.set ? 'success' : 'warn'}>
                      {field.set
                        ? t('settings.extensions.secretSet', 'Set')
                        : t('settings.extensions.secretUnset', 'Not set')}
                    </StatusChip>
                    <TextField
                      type="password"
                      autocomplete="off"
                      value={secretDrafts[extension.name]?.[field.key] ?? ''}
                      disabled={rowBusy}
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
                        disabled={rowBusy ||
                          !(secretDrafts[extension.name]?.[field.key] ?? '')}
                        onClick={() =>
                          saveSecret(
                            extension,
                            field,
                            secretDrafts[extension.name]?.[field.key] ?? '',
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
                  </div>
                {:else}
                  <TextField
                    type={field.type === 'number' ? 'number' : 'text'}
                    value={formStates[extension.name]?.[field.key] ?? ''}
                    disabled={rowBusy}
                    invalid={Boolean(
                      formFieldErrors[extension.name]?.[field.key],
                    )}
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
                  {#if formFieldErrors[extension.name]?.[field.key]}
                    <span class="s-field-error">
                      {t(
                        'settings.extensions.numberInvalid',
                        'Enter a valid number.',
                      )}
                    </span>
                  {/if}
                {/if}
              </div>
            {/each}
            <div class="s-ext-config-actions">
              <Button
                variant="primary"
                disabled={rowBusy}
                onClick={() => saveSchemaConfig(extension)}
              >
                {savingConfigName === extension.name
                  ? t('common.saving', 'Saving…')
                  : t('settings.extensions.saveSettings', 'Save settings')}
              </Button>
            </div>
          </div>
        {:else if !isOverridden}
          <div class="s-field s-field--full s-ext-config">
            <span class="s-field-label">
              {t('settings.extensions.config', 'Config (JSON)')}
            </span>
            <textarea
              class={`s-input s-textarea s-textarea--json${
                configErrors[extension.name] ? ' s-textarea--invalid' : ''
              }`}
              spellcheck="false"
              value={configDrafts[extension.name] ?? ''}
              disabled={rowBusy}
              aria-label={t(
                'settings.extensions.configAria',
                'Config for extension {name}',
                { name: extension.name },
              )}
              oninput={(event) =>
                setConfigDraft(extension.name, event.currentTarget.value)}
            ></textarea>
            {#if configErrors[extension.name]}
              <span class="s-field-error">{configErrors[extension.name]}</span>
            {/if}
            <div class="s-ext-config-actions">
              <Button
                variant="primary"
                disabled={rowBusy}
                onClick={() => saveExtensionConfig(extension)}
              >
                {savingConfigName === extension.name
                  ? t('common.saving', 'Saving…')
                  : t('settings.extensions.saveConfig', 'Save config')}
              </Button>
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}
