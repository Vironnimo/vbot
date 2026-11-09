<script>
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import SortableList from '../ui/SortableList.svelte';
  import Button from '../ui/Button.svelte';
  import TaskModelOptions from './TaskModelOptions.svelte';
  import LocalSpeechSupport from './LocalSpeechSupport.svelte';
  import { t } from '$lib/i18n.js';

  let { editor, taskType, title, active = true } = $props();
  let entries = $derived(editor.binding(taskType).fallbacks ?? []);
  function label(entry) {
    return (
      editor.targets(taskType).find((target) => target.id === entry.target)
        ?.label ?? entry.target
    );
  }
</script>

<div class="s-group__block s-group__block--attached">
  <div class="s-row-label">{t('settings.specializedModels.fallbacks')}</div>
  <div class="s-row-desc">{t('settings.specializedModels.fallbacksHelp')}</div>
  <SortableList
    items={entries}
    getKey={(entry) => entry.target}
    getLabel={label}
    onReorder={(from, to) => editor.reorderFallback(taskType, from, to)}
    itemFocusable
  >
    {#snippet item(entry, index)}
      {@const key = editor.fallbackKey(taskType, entry.target)}
      {@const target = editor
        .targets(taskType)
        .find((candidate) => candidate.id === entry.target)}
      <div class="s-row">
        <div class="s-row-label">{index + 1}. {label(entry)}</div>
        <Button
          variant="tertiary"
          onClick={() => editor.removeFallback(taskType, entry.target)}
          ariaLabel={t('settings.specializedModels.removeFallbackAria', {
            name: label(entry),
          })}
        >
          {t('settings.specializedModels.removeFallback')}
        </Button>
      </div>
      <div data-sortable-ignore>
        <TaskModelOptions {editor} taskType={key} title={label(entry)} />
        {#if target?.kind === 'local' && ['speech_to_text', 'text_to_speech'].includes(taskType)}
          <LocalSpeechSupport
            {active}
            target={entry.target}
            metadata={target.metadata}
            tts={taskType === 'text_to_speech'}
            taskSurfaceBusy={editor.surfaceBusy}
            onReady={() => editor.refreshTargets(taskType)}
          />
        {/if}
      </div>
    {/snippet}
  </SortableList>
  {#if entries.length < 8}
    <SearchableDropdown
      id={`settings-fallback-${taskType}`}
      value=""
      options={editor.fallbackOptions(taskType)}
      placeholder={t('settings.specializedModels.addFallback')}
      ariaLabel={t('settings.specializedModels.addFallbackAria', {
        name: title,
      })}
      disabled={editor.loading}
      onValueChange={(target) => editor.addFallback(taskType, target)}
    />
  {/if}
</div>
