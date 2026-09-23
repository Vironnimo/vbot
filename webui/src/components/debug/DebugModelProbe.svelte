<script>
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import { debugModelProbe } from '$lib/api.js';
  import {
    applyModelProbeResult,
    formattedBodyText,
    modelProbeCanProbe,
    modelProbeConnectionOptions,
    rawBodyText,
    selectModelProbeConnection,
    selectModelProbeProvider,
  } from '$lib/debugView.js';
  import { t } from '$lib/i18n.js';

  let { viewState = $bindable() } = $props();

  let providerOptions = $derived(
    (viewState.modelProbeProviders ?? []).map((provider) => ({
      value: provider.id,
      label: provider.name,
    })),
  );
  let connectionOptions = $derived(modelProbeConnectionOptions(viewState));
  let canProbe = $derived(modelProbeCanProbe(viewState));

  function handleProviderChange(providerId) {
    selectModelProbeProvider(viewState, providerId);
  }

  function handleConnectionChange(connectionId) {
    selectModelProbeConnection(viewState, connectionId);
  }

  async function handleProbe() {
    if (!canProbe) {
      return;
    }

    viewState.modelProbeLoading = true;
    viewState.modelProbeError = '';
    viewState.modelProbeResult = null;

    try {
      const result = await debugModelProbe(
        viewState.modelProbeProvider,
        viewState.modelProbeConnection,
      );
      applyModelProbeResult(viewState, result);
    } catch (error) {
      viewState.modelProbeError = errorMessageText(
        error,
        t('errors.generic', 'Something went wrong. Try again.'),
      );
      viewState.modelProbeLoading = false;
    }
  }

  function errorMessageText(error, fallback) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }
    return fallback;
  }
</script>

<section class="debug-view__probe" aria-labelledby="probe-title">
  <h3 id="probe-title" class="debug-view__probe-title">
    {t('debug.modelProbe', 'Model Endpoint Probe')}
  </h3>

  <div class="debug-view__probe-controls">
    <label class="debug-view__probe-field">
      <span class="debug-view__probe-label">
        {t('debug.modelProbe.provider', 'Provider')}
      </span>
      <Dropdown
        id="debug-probe-provider"
        value={viewState.modelProbeProvider}
        options={providerOptions}
        placeholder={t('debug.modelProbe.selectProvider', 'Select a provider')}
        ariaLabel={t('debug.modelProbe.provider', 'Provider')}
        disabled={viewState.modelProbeLoading}
        triggerClass="debug-view__probe-dropdown"
        onValueChange={handleProviderChange}
      />
    </label>

    <label class="debug-view__probe-field">
      <span class="debug-view__probe-label">
        {t('debug.modelProbe.connection', 'Connection')}
      </span>
      <Dropdown
        id="debug-probe-connection"
        value={viewState.modelProbeConnection}
        options={connectionOptions}
        placeholder={t(
          'debug.modelProbe.selectConnection',
          'Select a connection',
        )}
        ariaLabel={t('debug.modelProbe.connection', 'Connection')}
        disabled={!viewState.modelProbeProvider || viewState.modelProbeLoading}
        triggerClass="debug-view__probe-dropdown"
        onValueChange={handleConnectionChange}
      />
    </label>

    <Button
      variant="primary"
      class="debug-view__probe-btn"
      onClick={handleProbe}
      disabled={!canProbe || viewState.modelProbeLoading}
    >
      {viewState.modelProbeLoading
        ? t('common.loading', 'Loading\u2026')
        : t('debug.modelProbe.run', 'Probe')}
    </Button>
  </div>

  {#if viewState.modelProbeError}
    <Banner variant="error" aria-live="polite">
      <span>{viewState.modelProbeError}</span>
    </Banner>
  {/if}

  {#if viewState.modelProbeResult}
    <div class="debug-view__probe-results">
      <div class="debug-view__probe-result-section">
        <h4 class="debug-view__detail-heading">
          {t('debug.modelProbe.rawResponse', 'Raw Response')}
        </h4>
        <pre
          class="debug-view__code-block debug-view__code-block--formatted">{formattedBodyText(
            viewState.modelProbeResult.raw,
          ) || '—'}</pre>
      </div>

      <div class="debug-view__probe-result-section">
        <h4 class="debug-view__detail-heading">
          {t('debug.modelProbe.normalizedPreview', 'Normalized Preview')}
        </h4>
        {#if viewState.modelProbeResult.normalized?.preview?.length > 0}
          <div class="debug-view__probe-model-list">
            {#each viewState.modelProbeResult.normalized.preview as model (model.id)}
              <span class="debug-view__probe-model-chip">{model.name}</span>
            {/each}
          </div>
          <p class="debug-view__probe-model-count">
            {t('debug.modelProbe.modelCount', '{count} models', {
              count: viewState.modelProbeResult.normalized.modelCount,
            })}
          </p>
        {:else}
          <pre
            class="debug-view__code-block debug-view__code-block--formatted">{rawBodyText(
              viewState.modelProbeResult.normalized,
            ) || '—'}</pre>
        {/if}
      </div>
    </div>
  {/if}
</section>

<style>
  .debug-view__probe {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .debug-view__probe-title {
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-heading-sm);
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .debug-view__probe-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px;
  }

  .debug-view__probe-field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
  }

  .debug-view__probe-label {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 500;
  }

  .debug-view__probe-field :global(.debug-view__probe-dropdown) {
    display: block;
    width: 200px;
  }

  :global(.debug-view__probe-btn) {
    align-self: flex-end;
  }

  .debug-view__probe-results {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .debug-view__probe-result-section {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .debug-view__detail-heading {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .debug-view__code-block {
    box-sizing: border-box;
    max-width: 100%;
    max-height: 400px;
    margin: 0;
    overflow: auto;
    overflow-y: auto;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-med);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.55;
    user-select: text;
    -webkit-user-select: text;
  }

  .debug-view__code-block--formatted {
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .debug-view__probe-model-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .debug-view__probe-model-chip {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .debug-view__probe-model-count {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }

  @media (max-width: 860px) {
    .debug-view__probe-controls {
      align-items: stretch;
      flex-direction: column;
    }

    .debug-view__probe-field :global(.debug-view__probe-dropdown) {
      width: 100%;
    }
  }
</style>
