<script>
  // The selected Task Model target's option controls: ordinary options
  // (fields another option's value makes irrelevant stay hidden), then the
  // closed JSON disclosures with the quiet reset action on their line. The
  // placement supplies the surrounding block.
  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import PathField from '../ui/PathField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import { JSON_OPTION_TYPE } from '$lib/taskModelSettings.js';

  // `editor` is a `createTaskModelEditor` controller; `title` names the task
  // in the reset action's accessible name.
  let { editor, taskType, title } = $props();

  let visibleFields = $derived(editor.visibleFields(taskType));
  let plainFields = $derived(
    visibleFields.filter((field) => field.type !== JSON_OPTION_TYPE),
  );
  let jsonFields = $derived(
    visibleFields.filter((field) => field.type === JSON_OPTION_TYPE),
  );
  let canReset = $derived(editor.canReset(taskType));
  const booleanChoices = [
    { value: '', label: t('settings.specializedModels.providerDefault') },
    { value: 'true', label: t('settings.specializedModels.booleanOn') },
    { value: 'false', label: t('settings.specializedModels.booleanOff') },
  ];
</script>

{#snippet optionField(field)}
  {@const jsonError =
    field.type === JSON_OPTION_TYPE ? editor.jsonError(taskType, field) : ''}
  {@const fieldControlId = `task-model-${taskType}-${field.name}`}
  <!-- The field's explanation from the backend schema sits behind the
       label's "?"; the controls name themselves, so the hint does not become
       part of their accessible name. -->
  <FormField
    controlId={fieldControlId}
    full={field.type === JSON_OPTION_TYPE}
    error={jsonError
      ? t('settings.specializedModels.jsonInvalid', {
          error: jsonError,
        })
      : ''}
  >
    {#snippet labelContent()}
      {field.label}
      {#if field.description}
        <InfoHint
          text={field.description}
          ariaLabel={t('settings.specializedModels.aboutAria', {
            name: field.label,
          })}
        />
      {/if}
    {/snippet}
    {#snippet children(formField)}
      {#if field.type === 'select'}
        <Dropdown
          id={formField.controlId}
          ariaLabelledby={formField.labelId}
          value={editor.optionValue(taskType, field)}
          options={editor.fieldChoices(taskType, field)}
          ariaLabel={field.label}
          ariaDescribedby={formField.describedBy}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => editor.setOption(taskType, field, value)}
        />
      {:else if field.type === 'textarea'}
        <TextArea
          id={formField.controlId}
          rows="3"
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          value={editor.optionValue(taskType, field)}
          onInput={(_value, event) =>
            editor.handleOptionInput(taskType, field, event)}
        />
      {:else if field.type === JSON_OPTION_TYPE}
        <TextArea
          id={formField.controlId}
          code
          invalid={formField.invalid}
          rows="4"
          spellcheck="false"
          autocapitalize="off"
          autocorrect="off"
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          placeholder={field.placeholder ||
            t('settings.specializedModels.jsonPlaceholder')}
          value={editor.optionValue(taskType, field)}
          onInput={(_value, event) =>
            editor.handleOptionInput(taskType, field, event)}
        />
      {:else if field.type === 'number'}
        <TextField
          id={formField.controlId}
          type="number"
          ariaLabel={field.label}
          placeholder={field.placeholder}
          aria-describedby={formField.describedBy}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          step={field.step ?? 'any'}
          value={editor.optionValue(taskType, field)}
          onInput={(_next, event) =>
            editor.handleOptionInput(taskType, field, event)}
        />
      {:else if field.type === 'boolean' && !editor.hasBooleanDefault(field)}
        <Dropdown
          id={formField.controlId}
          ariaLabelledby={formField.labelId}
          value={editor.booleanChoice(taskType, field)}
          options={booleanChoices}
          ariaLabel={field.label}
          ariaDescribedby={formField.describedBy}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) =>
            editor.setBooleanChoice(taskType, field, value)}
        />
      {:else if field.type === 'boolean'}
        <Toggle
          id={formField.controlId}
          checked={editor.optionValue(taskType, field) === true}
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          onChange={(next) => editor.setOption(taskType, field, next)}
        />
      {:else if field.serverPath}
        <!-- A path on the vBot server ('directory', 'file' or 'any'). -->
        <PathField
          id={formField.controlId}
          mode={field.serverPath}
          value={editor.optionValue(taskType, field)}
          ariaLabel={field.label}
          placeholder={field.placeholder}
          aria-describedby={formField.describedBy}
          onInput={(next) => editor.setOption(taskType, field, next)}
        />
      {:else}
        <TextField
          id={formField.controlId}
          value={editor.optionValue(taskType, field)}
          ariaLabel={field.label}
          placeholder={field.placeholder}
          aria-describedby={formField.describedBy}
          onInput={(_next, event) =>
            editor.handleOptionInput(taskType, field, event)}
        />
      {/if}
    {/snippet}
  </FormField>
{/snippet}

{#if plainFields.length > 0}
  <div class="s-task-model-options">
    {#each plainFields as field (field.name)}
      {@render optionField(field)}
    {/each}
  </div>
{/if}

{#if jsonFields.length > 0 || canReset}
  <div class="s-task-model-more">
    {#if jsonFields.length > 0}
      <div class="s-task-model-more__json">
        {#each jsonFields as field (field.name)}
          <details class="s-task-model-advanced">
            <summary>{field.label}<span aria-hidden="true">JSON</span></summary>
            {@render optionField(field)}
          </details>
        {/each}
      </div>
    {/if}
    {#if canReset}
      <Button
        variant="tertiary"
        class="s-task-model-reset"
        ariaLabel={t('settings.specializedModels.resetOptionsAria', {
          task: title,
        })}
        disabled={editor.saving || editor.loading}
        onClick={() => editor.resetOptions(taskType)}
        >{t('settings.specializedModels.resetOptions')}</Button
      >
    {/if}
  </div>
{/if}

<style>
  /* The JSON disclosures and the reset action share one line below the
     ordinary options; an opened disclosure grows downwards while the reset
     action stays at the line's end. */
  .s-task-model-more {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }

  .s-task-model-more__json {
    display: grid;
    flex: 1 1 auto;
    min-width: 0;
  }

  .s-task-model-more :global(.s-task-model-reset) {
    flex: 0 0 auto;
    margin-left: auto;
  }
</style>
