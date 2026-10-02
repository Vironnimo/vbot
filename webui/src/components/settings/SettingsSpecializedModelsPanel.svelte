<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import LocalSpeechSupport from './LocalSpeechSupport.svelte';
  import TaskModelOptions from './TaskModelOptions.svelte';
  import { createTaskModelEditor } from './taskModelEditor.svelte.js';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import { getLocalSpeechMemory, unloadLocalSpeech } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onError = noop,
    modelsRefreshToken = 0,
    taskTypes = null,
    showTaskLabels = true,
  } = $props();

  const editor = createTaskModelEditor({
    taskTypes: untrack(() => taskTypes),
    getSettings: () => settings,
    getModelsRefreshToken: () => modelsRefreshToken,
    onCommit: (next) => onCommit(next),
    onError: (message) => onError(message),
  });
  const ownsSpeech = editor.rows.some((row) =>
    ['speech_to_text', 'text_to_speech'].includes(row.taskType),
  );

  let destroyed = false;
  let speechMemory = $state(null);
  let speechMemoryError = $state('');
  const speechUnloading = new SvelteSet();
  let speechUnloadErrors = $state({});
  let speechMemoryTimer;
  let speechMemoryRequest = 0;
  let selectedSpeechTargets = $derived(
    ['speech_to_text', 'text_to_speech']
      .map((task) => editor.bindings[task]?.target)
      .filter((target) => target?.startsWith('local/')),
  );
  let speechMemoryModels = $derived(
    (speechMemory?.models ?? []).filter(
      (model) =>
        model.loaded ||
        model.busy ||
        selectedSpeechTargets.includes(model.target),
    ),
  );
  let showSpeechMemory = $derived(
    speechMemoryModels.length > 0 || selectedSpeechTargets.length > 0,
  );

  async function refreshSpeechMemory() {
    const request = ++speechMemoryRequest;
    try {
      const result = await getLocalSpeechMemory();
      if (destroyed || request !== speechMemoryRequest) return;
      speechMemory = result;
      speechMemoryError = '';
    } catch {
      if (destroyed || request !== speechMemoryRequest) return;
      speechMemory = null;
      speechMemoryError = t('settings.localSpeech.memoryError');
    } finally {
      if (!destroyed && request === speechMemoryRequest)
        speechMemoryTimer = setTimeout(refreshSpeechMemory, 2000);
    }
  }

  async function unloadSpeechMemory(model) {
    const target = model.target;
    if (speechUnloading.has(target) || !model.loaded || model.busy) return;
    speechUnloading.add(target);
    speechMemoryRequest += 1;
    clearTimeout(speechMemoryTimer);
    speechUnloadErrors = { ...speechUnloadErrors, [target]: '' };
    try {
      const result = await unloadLocalSpeech(target);
      if (!destroyed) {
        const updated = result.models.find((entry) => entry.target === target);
        if (updated)
          speechMemory = {
            models: speechMemory.models.map((entry) =>
              entry.target === target ? updated : entry,
            ),
          };
      }
    } catch {
      if (!destroyed)
        speechUnloadErrors = {
          ...speechUnloadErrors,
          [target]: t('settings.localSpeech.unloadError'),
        };
    } finally {
      speechUnloading.delete(target);
      if (!destroyed && speechUnloading.size === 0)
        speechMemoryTimer = setTimeout(refreshSpeechMemory, 2000);
    }
  }

  onMount(() => {
    if (ownsSpeech) void refreshSpeechMemory();
  });

  onDestroy(() => {
    destroyed = true;
    speechMemoryRequest += 1;
    clearTimeout(speechMemoryTimer);
  });
</script>

{#if editor.loading}
  <Banner variant="neutral">
    {t('settings.specializedModels.loading')}
  </Banner>
{/if}

<div class="s-group s-task-model-list">
  {#each editor.rows as row (row.taskType)}
    {@const binding = editor.binding(row.taskType)}
    {@const hasOptions =
      editor.visibleFields(row.taskType).length > 0 ||
      editor.canReset(row.taskType)}
    {@const selectedTarget = editor
      .targets(row.taskType)
      .find((target) => target.id === binding.target)}
    {@const localSpeech =
      ['speech_to_text', 'text_to_speech'].includes(row.taskType) &&
      selectedTarget?.kind === 'local'}
    <div class="s-row s-task-model-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {showTaskLabels || !row.label ? row.title() : row.label()}
          <InfoHint
            text={row.help()}
            ariaLabel={t('settings.specializedModels.aboutAria', {
              name: row.title(),
            })}
          />
        </div>
        {#if row.description}
          <div class="s-row-desc">{row.description()}</div>
        {/if}
      </div>
      <div class="s-row-control s-row-control--task-model">
        <SearchableDropdown
          id={`settings-specialized-${row.taskType}`}
          value={binding.target}
          options={editor.targetOptions(row.taskType)}
          placeholder={t('settings.specializedModels.noTarget')}
          ariaLabel={row.title()}
          disabled={editor.loading}
          triggerClass="settings-view__dropdown"
          onValueChange={(value) => editor.setTarget(row.taskType, value)}
        />
      </div>
    </div>

    {#if binding.target && (hasOptions || localSpeech)}
      <!-- The chosen target's options continue its row, followed by local
           speech setup. -->
      <div class="s-group__block s-group__block--attached s-task-model-details">
        <TaskModelOptions
          {editor}
          taskType={row.taskType}
          title={row.title()}
        />

        {#if localSpeech}
          {#key binding.target}
            <LocalSpeechSupport
              target={binding.target}
              metadata={selectedTarget?.metadata}
              tts={row.taskType === 'text_to_speech'}
              taskSurfaceBusy={editor.surfaceBusy}
              onReady={() => editor.refreshTargets(row.taskType)}
            />
          {/key}
        {/if}
      </div>
    {/if}
  {/each}
</div>

{#if showSpeechMemory}
  <div class="s-task-memory" data-local-speech-memory>
    <div class="s-subhead s-task-memory__head">
      <h4 class="s-subhead__title">{t('settings.localSpeech.memoryTitle')}</h4>
      <InfoHint
        text={t('settings.localSpeech.memoryHelp')}
        ariaLabel={t('settings.specializedModels.aboutAria', {
          name: t('settings.localSpeech.memoryTitle'),
        })}
      />
    </div>
    {#if speechMemoryError}<div role="alert">{speechMemoryError}</div>{/if}
    {#if !speechMemory && !speechMemoryError}
      <div class="s-group__note" role="status">
        {t('settings.localSpeech.memoryChecking')}
      </div>
    {/if}
    {#if speechMemoryModels.length}
      <div class="s-group">
        {#each speechMemoryModels as model (model.target)}
          <div
            class="s-row s-row--compact"
            data-speech-memory-target={model.target}
          >
            <div class="s-row-info">
              <div class="s-row-label">{model.label}</div>
              <div class="s-row-desc" role="status" aria-live="polite">
                {#if speechUnloading.has(model.target)}
                  {t('settings.localSpeech.unloading')}
                {:else if model.busy}
                  {t('settings.localSpeech.memoryBusy')}
                {:else if model.loaded}
                  {t('settings.localSpeech.memoryLoaded')}
                {:else}
                  {t('settings.localSpeech.memoryEmpty')}
                {/if}
              </div>
              {#if speechUnloadErrors[model.target]}
                <div role="alert">{speechUnloadErrors[model.target]}</div>
              {/if}
            </div>
            <div class="s-row-control">
              <Button
                disabled={speechUnloading.has(model.target) ||
                  !model.loaded ||
                  model.busy}
                ariaLabel={t('settings.localSpeech.unloadAria', {
                  model: model.label,
                })}
                onClick={() => unloadSpeechMemory(model)}
                >{t('settings.localSpeech.unloadButton')}</Button
              >
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<div class="s-footer">
  <SaveStatus
    saving={editor.saving}
    pending={editor.participant.hasChanges()}
    onClick={() => editor.participant.runSave('manual')}
  />
</div>

<style>
  .s-task-memory__head {
    display: flex;
    align-items: center;
    gap: 6px;
  }
</style>
