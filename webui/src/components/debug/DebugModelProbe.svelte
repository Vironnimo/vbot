<script>
  import { debugModelProbe } from '$lib/api.js';
  import {
    applyModelProbeResult,
    modelProbeCanProbe,
    modelProbeConnectionOptions,
    rawBodyText,
    selectModelProbeConnection,
    selectModelProbeProvider,
  } from '$lib/debugView.js';
  import { t } from '$lib/i18n.js';

  let { viewState } = $props();

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
      <select
        class="debug-view__probe-select"
        value={viewState.modelProbeProvider}
        onchange={(event) => handleProviderChange(event.currentTarget.value)}
        disabled={viewState.modelProbeLoading}
      >
        <option value="">
          {t('debug.modelProbe.selectProvider', 'Select a provider')}
        </option>
        {#each viewState.modelProbeProviders as provider (provider.id)}
          <option value={provider.id}>{provider.name}</option>
        {/each}
      </select>
    </label>

    <label class="debug-view__probe-field">
      <span class="debug-view__probe-label">
        {t('debug.modelProbe.connection', 'Connection')}
      </span>
      <select
        class="debug-view__probe-select"
        value={viewState.modelProbeConnection}
        onchange={(event) => handleConnectionChange(event.currentTarget.value)}
        disabled={!viewState.modelProbeProvider || viewState.modelProbeLoading}
      >
        <option value="">
          {t('debug.modelProbe.selectConnection', 'Select a connection')}
        </option>
        {#each connectionOptions as option (option.value)}
          <option value={option.value}>{option.label}</option>
        {/each}
      </select>
    </label>

    <button
      type="button"
      class="btn-primary debug-view__probe-btn"
      onclick={handleProbe}
      disabled={!canProbe || viewState.modelProbeLoading}
    >
      {viewState.modelProbeLoading
        ? t('common.loading', 'Loading\u2026')
        : t('debug.modelProbe.run', 'Probe')}
    </button>
  </div>

  {#if viewState.modelProbeError}
    <div
      class="debug-view__feedback debug-view__feedback--error"
      aria-live="polite"
    >
      <span>{viewState.modelProbeError}</span>
    </div>
  {/if}

  {#if viewState.modelProbeResult}
    <div class="debug-view__probe-results">
      <div class="debug-view__probe-result-section">
        <h4 class="debug-view__detail-heading">
          {t('debug.modelProbe.rawResponse', 'Raw Response')}
        </h4>
        <pre
          class="debug-view__code-block debug-view__code-block--raw"
          title={viewState.modelProbeResult.raw || ''}>{viewState
            .modelProbeResult.raw || '—'}</pre>
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
    font-size: 15px;
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
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .debug-view__probe-select {
    width: 200px;
    padding: 7px 28px 7px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background-color: var(--surface-2);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%239a8c7e'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    font-family: var(--font-mono);
    font-size: 12px;
    cursor: pointer;
    appearance: none;
  }

  .debug-view__probe-select:focus-visible {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
    outline: none;
  }

  .debug-view__probe-select:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .debug-view__probe-btn {
    align-self: flex-end;
  }

  .debug-view__feedback {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .debug-view__feedback--error {
    border-color: rgba(252, 129, 129, 0.2);
    color: var(--red);
    background: rgba(252, 129, 129, 0.08);
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
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
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
    font-size: 11.5px;
    line-height: 1.55;
    user-select: text;
    -webkit-user-select: text;
  }

  .debug-view__code-block--raw {
    overflow-wrap: normal;
    white-space: pre;
    word-break: normal;
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
    border-radius: 12px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .debug-view__probe-model-count {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  @media (max-width: 860px) {
    .debug-view__probe-controls {
      align-items: stretch;
      flex-direction: column;
    }

    .debug-view__probe-select {
      width: 100%;
    }
  }
</style>
