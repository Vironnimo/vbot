<script>
  import { onMount } from 'svelte';

  import Button from '../ui/Button.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    applyExtensionsPanelList,
    buildExtensionsUpdatePayload,
    extensionStatusChipVariant,
    formatExtensionConfig,
    parseExtensionConfigDraft,
    summarizeExtensionCapabilities,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let { onToast = noop } = $props();

  let extensions = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let actionError = $state('');
  let restartRequired = $state(false);
  let actionName = $state('');
  let savingConfigName = $state('');
  let configDrafts = $state({});
  let configErrors = $state({});

  let panelBusy = $derived(
    loading || actionName.length > 0 || savingConfigName.length > 0,
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
      configErrors = {};
    } catch (error) {
      loadError = `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`;
    } finally {
      loading = false;
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
      const response = await rpc('settings.update', payload);
      if (response?.restart_required) {
        restartRequired = true;
      }
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
      const response = await rpc('settings.update', payload);
      if (response?.restart_required) {
        restartRequired = true;
      }
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
    </div>
  </div>
</div>

{#if restartRequired}
  <div class="s-feedback s-ext-restart">
    {t(
      'settings.extensions.restartRequired',
      'Extension changes apply after a restart.',
    )}
    <code>vbot server restart</code>
  </div>
{/if}

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
      {@const capabilities = summarizeExtensionCapabilities(
        extension.capabilities,
        t,
      )}
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
            {#if capabilities}
              <div class="s-row-desc s-ext-capabilities">{capabilities}</div>
            {/if}
            {#each extension.capabilityErrors as capabilityError (capabilityError)}
              <div class="s-row-desc s-ext-warning">
                {t('settings.extensions.warning', 'Warning')}: {capabilityError}
              </div>
            {/each}
          </div>

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
        </div>

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
      </div>
    {/each}
  </div>
{/if}
