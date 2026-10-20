<script>
  // One manual Model of the Custom Provider editor: a compact entity row
  // (name and a one-line summary) whose fields open inside the same row,
  // like a Provider or Channel row on the Settings pages. The editor owns
  // the drafts and validation; this row only renders and reports edits.
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatTokens } from '$lib/statisticsView.js';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import TextField from '../ui/TextField.svelte';

  const noop = () => {};

  let {
    model,
    index = 0,
    expanded = false,
    errors = {},
    disabled = false,
    onToggle = noop,
    onChange = noop,
    onRemove = noop,
  } = $props();

  let prefix = $derived(`custom-model-${index}`);
  let fallbackName = $derived(
    t('settings.providers.custom.modelNumber', { number: index + 1 }),
  );
  let displayName = $derived(
    model.name.trim() || model.modelId.trim() || fallbackName,
  );
  let capabilityOptions = $derived([
    ['tools', t('settings.providers.custom.tools')],
    ['vision', t('settings.providers.custom.vision')],
    ['jsonMode', t('settings.providers.custom.jsonMode')],
    ['reasoning', t('settings.providers.custom.reasoning')],
  ]);
  // The collapsed row says what the Model is without opening it: the wire id
  // when the display name differs, the context window and the capabilities.
  let summary = $derived(
    [
      model.modelId.trim() && model.modelId.trim() !== displayName
        ? model.modelId.trim()
        : '',
      contextSummary(model.contextWindow),
      capabilityOptions
        .filter(([key]) => model[key])
        .map(([, label]) => label)
        .join(', '),
    ]
      .filter(Boolean)
      .join(' · '),
  );
  let hasErrors = $derived(Object.values(errors).some(Boolean));

  function contextSummary(value) {
    const tokens = Number(String(value ?? '').trim());
    if (!Number.isInteger(tokens) || tokens <= 0) {
      return '';
    }
    return t('settings.providers.custom.modelContext', {
      tokens: formatTokens(tokens, activeLocaleTag()),
    });
  }

  // The whole head toggles the details; controls and selected text keep
  // their own behavior.
  function handleHeadClick(event) {
    if (event.target.closest('button, a, input, select, textarea')) {
      return;
    }
    if (window.getSelection?.()?.toString()) {
      return;
    }
    onToggle();
  }

  function describedBy(field, help = true) {
    return (
      [
        help && `${prefix}-${field}-desc`,
        errors[field] && `${prefix}-${field}-error`,
      ]
        .filter(Boolean)
        .join(' ') || undefined
    );
  }
</script>

<div class="s-entity custom-model">
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
  <div class="s-entity__head s-entity__head--toggle" onclick={handleHeadClick}>
    <div class="s-row-info">
      <div class="s-row-label custom-model__name">{displayName}</div>
      {#if summary}
        <div class="s-row-desc custom-model__summary">{summary}</div>
      {/if}
      {#if hasErrors && !expanded}
        <p class="s-row-error">
          {t('settings.providers.custom.modelHasErrors')}
        </p>
      {/if}
    </div>
    <div class="s-entity__end">
      <Button
        variant="tertiary"
        icon
        class="s-disclosure-btn"
        ariaLabel={t('settings.providers.custom.modelDetailsAria', {
          model: displayName,
        })}
        aria-expanded={expanded}
        aria-controls={`${prefix}-details`}
        onClick={onToggle}
      >
        <span
          class="disclosure-chevron"
          class:disclosure-chevron--open={expanded}
          aria-hidden="true"
        ></span>
      </Button>
    </div>
  </div>

  <div
    id={`${prefix}-details`}
    class="s-disclosure-sub custom-model__details"
    hidden={!expanded}
  >
    <div class="s-group__rows custom-model__rows">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for={`${prefix}-id`}>
            {t('settings.providers.custom.modelId')}
          </label>
          <div class="s-row-desc" id={`${prefix}-id-desc`}>
            {t('settings.providers.custom.modelIdHint')}
          </div>
          {#if errors.id}
            <p class="s-row-error" id={`${prefix}-id-error`} role="alert">
              {errors.id}
            </p>
          {/if}
        </div>
        <div class="s-row-control">
          <TextField
            id={`${prefix}-id`}
            code
            value={model.modelId}
            invalid={Boolean(errors.id)}
            aria-required="true"
            aria-describedby={describedBy('id')}
            {disabled}
            onInput={(value) => onChange({ modelId: value })}
          />
        </div>
      </div>

      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for={`${prefix}-name`}>
            {t('settings.providers.custom.modelName')}
          </label>
        </div>
        <div class="s-row-control">
          <TextField
            id={`${prefix}-name`}
            value={model.name}
            {disabled}
            onInput={(value) => onChange({ name: value })}
          />
        </div>
      </div>

      {#each [['context', 'contextWindow', t('settings.providers.custom.contextWindow')], ['output', 'maxOutputTokens', t('settings.providers.custom.maxOutput')]] as [field, key, label] (field)}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <label class="s-row-label" for={`${prefix}-${field}`}>
              {label}
            </label>
            {#if errors[field]}
              <p
                class="s-row-error"
                id={`${prefix}-${field}-error`}
                role="alert"
              >
                {errors[field]}
              </p>
            {/if}
          </div>
          <div class="s-row-control s-row-control--number">
            <TextField
              id={`${prefix}-${field}`}
              type="number"
              min="1"
              inputmode="numeric"
              value={model[key]}
              invalid={Boolean(errors[field])}
              aria-describedby={describedBy(field, false)}
              {disabled}
              onInput={(value) => onChange({ [key]: value })}
            />
          </div>
        </div>
      {/each}

      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label" id={`${prefix}-capabilities-label`}>
            {t('settings.providers.custom.capabilities')}
          </div>
        </div>
        <div
          class="s-row-control custom-model__capabilities"
          role="group"
          aria-labelledby={`${prefix}-capabilities-label`}
        >
          {#each capabilityOptions as [key, label] (key)}
            <Checkbox
              checked={model[key]}
              {disabled}
              ariaLabel={t('settings.providers.custom.capabilityAria', {
                capability: label,
                model: displayName,
              })}
              onChange={(checked) => onChange({ [key]: checked })}
            >
              {label}
            </Checkbox>
          {/each}
        </div>
      </div>

      {#each [['input-modalities', 'inputModalities', t('settings.providers.custom.inputModalities'), t('settings.providers.custom.inputModalitiesHint')], ['output-modalities', 'outputModalities', t('settings.providers.custom.outputModalities'), t('settings.providers.custom.outputModalitiesHint')], ['tasks', 'taskTypes', t('settings.providers.custom.taskTypes'), t('settings.providers.custom.taskTypesHint')], ['parameters', 'supportedParameters', t('settings.providers.custom.parameters'), t('settings.providers.custom.parametersHint')], ['voices', 'supportedVoices', t('settings.providers.custom.voices'), t('settings.providers.custom.voicesHint')]] as [field, key, label, hint] (field)}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for={`${prefix}-${field}`}>
              {label}
            </label>
            <div class="s-row-desc" id={`${prefix}-${field}-desc`}>
              {hint}
            </div>
          </div>
          <div class="s-row-control">
            <TextField
              id={`${prefix}-${field}`}
              code
              value={model[key]}
              aria-describedby={`${prefix}-${field}-desc`}
              {disabled}
              onInput={(value) => onChange({ [key]: value })}
            />
          </div>
        </div>
      {/each}
    </div>

    <div class="custom-model__actions">
      <Button variant="danger" {disabled} onClick={onRemove}>
        {t('settings.providers.custom.removeModel')}
      </Button>
    </div>
  </div>
</div>

<style>
  .custom-model__name,
  .custom-model__summary {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .custom-model__capabilities {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 16px;
  }

  /* The rows sit on the details' own indent; hairlines keep the
     label/control pairs readable. */
  .custom-model__rows > :global(.s-row) {
    padding: 10px 0;
  }

  .custom-model__rows > :global(.s-row + .s-row) {
    border-top: 1px solid var(--border);
  }

  .custom-model__actions {
    display: flex;
  }
</style>
