<script>
  import { t, formatFileSize } from '$lib/i18n.js';
  import { openDesktopExternalUrl } from '$lib/desktopBridge.js';
  import Modal from '../ui/Modal.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';
  import { describeDownloadProgress } from '$lib/settingsView.js';
  import TextField from '../ui/TextField.svelte';
  import Button from '../ui/Button.svelte';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';

  let {
    models = [],
    selectedIds = [],
    activeIds = [],
    busy = false,
    addingModelId = null,
    download = null,
    onCancelDownload,
    error = null,
    onAdd,
    onImport,
    onDelete,
    onClose,
  } = $props();
  let progress = $derived(describeDownloadProgress(download));
  let search = $state('');
  let linkError = $state(false);
  let filtered = $derived(
    models.filter((model) =>
      model.label
        .toLocaleLowerCase()
        .includes(search.trim().toLocaleLowerCase()),
    ),
  );

  async function openSource(url) {
    linkError = false;
    try {
      await openDesktopExternalUrl(url);
    } catch {
      linkError = true;
    }
  }
</script>

<Modal
  title={t('settings.voice.catalogTitle')}
  description={t('settings.voice.catalogDescription')}
  size="lg"
  closeDisabled={busy}
  {onClose}
>
  {#snippet body()}
    <div class="modal-body wakeword-catalog">
      <TextField
        type="search"
        variant="modal"
        value={search}
        onInput={(value) => (search = value)}
        ariaLabel={t('settings.voice.catalogSearch')}
        placeholder={t('settings.voice.catalogSearch')}
      />
      {#if download}
        <div class="wakeword-catalog__download">
          <span
            >{models.find((model) => model.id === download.model_id)
              ?.label}</span
          >
          {#if progress}<ProgressBar
              label={t('settings.localModel.progressLabel')}
              percent={progress.percent}
              text={progress.text}
            />{/if}
          <Button variant="tertiary" onClick={onCancelDownload}
            >{t('common.cancel')}</Button
          >
        </div>
      {/if}
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
      {#if linkError}<Banner variant="error" role="alert"
          >{t('settings.voice.sourceFailed')}</Banner
        >{/if}
      <div class="wakeword-catalog__list">
        {#each filtered as model (model.id)}
          {@const selected = selectedIds.includes(model.id)}
          <div class="wakeword-catalog__row" data-catalog-model-id={model.id}>
            <div class="wakeword-catalog__info">
              <div class="wakeword-catalog__name">
                <strong>{model.label}</strong>
                {#if model.bundled}<Badge>{t('settings.voice.included')}</Badge>
                {:else if model.source === 'imported'}<Badge
                    >{t('settings.voice.modelImported')}</Badge
                  >
                {:else if model.community}<Badge
                    >{t('settings.voice.communityModel')}</Badge
                  >{/if}
              </div>
              <div class="wakeword-catalog__metadata">
                {#if model.language === 'en'}<span
                    >{t('settings.voice.languageEnglish')}</span
                  >{/if}
                {#if model.size_bytes}<span
                    >{formatFileSize(model.size_bytes)}</span
                  >{/if}
                {#if model.license}<span>{model.license}</span>{/if}
                {#if model.homepage}
                  <Button
                    variant="tertiary"
                    onClick={() => openSource(model.homepage)}
                    ariaLabel={t('settings.voice.sourceAria', {
                      name: model.label,
                    })}>{t('settings.voice.modelSource')}</Button
                  >
                {/if}
              </div>
            </div>
            <div class="wakeword-catalog__actions">
              {#if model.removable}
                <Button
                  variant="tertiary"
                  disabled={busy || activeIds.includes(model.id)}
                  ariaLabel={t('settings.voice.deleteAria', {
                    name: model.label,
                  })}
                  onClick={() => onDelete(model)}
                  >{t('settings.voice.removeModel')}</Button
                >
              {/if}
              <Button
                variant={selected ? 'tertiary' : 'secondary'}
                disabled={busy || (selected && model.installed !== false)}
                loading={addingModelId === model.id}
                onClick={() => onAdd(model.id)}
                ariaLabel={t('settings.voice.addAria', { name: model.label })}
              >
                {#if addingModelId === model.id}{t(
                    'settings.voice.addingWakeword',
                  )}
                {:else if model.installed === false}{t(
                    'settings.voice.downloadAndAdd',
                  )}
                {:else if selected}{t('settings.voice.added')}
                {:else}{t('settings.voice.addWakewordShort')}{/if}
              </Button>
            </div>
          </div>
        {:else}
          <p class="wakeword-catalog__empty">
            {t('settings.voice.catalogEmpty')}
          </p>
        {/each}
      </div>
      <p class="wakeword-catalog__help">{t('settings.voice.communityHelp')}</p>
    </div>
  {/snippet}
  {#snippet footer()}
    <div class="wakeword-catalog__footer">
      <Button variant="tertiary" disabled={busy} onClick={onImport}
        >{t('settings.voice.importModel')}</Button
      >
      <span>{t('settings.voice.importHelp')}</span>
    </div>
  {/snippet}
</Modal>

<style>
  .wakeword-catalog {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .wakeword-catalog__row {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }
  .wakeword-catalog__info {
    flex: 1;
    min-width: 0;
  }
  .wakeword-catalog__name,
  .wakeword-catalog__metadata,
  .wakeword-catalog__actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  .wakeword-catalog__name {
    color: var(--text-hi);
  }
  .wakeword-catalog__metadata {
    margin-top: 4px;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }
  .wakeword-catalog__help,
  .wakeword-catalog__empty,
  .wakeword-catalog__footer {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }
  .wakeword-catalog__footer {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .wakeword-catalog__footer :global(.btn-tertiary) {
    flex-shrink: 0;
  }
  @media (max-width: 600px) {
    .wakeword-catalog__row,
    .wakeword-catalog__footer {
      align-items: flex-start;
      flex-direction: column;
      gap: 8px;
    }
  }
</style>
