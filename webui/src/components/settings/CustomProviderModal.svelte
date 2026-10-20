<script>
  // Custom Provider editor: a sheet in the Settings section grammar —
  // Connection, Authentication, Manual Models and the Advanced wire profile.
  // Save stays enabled; a click on it checks every field and shows each
  // problem under its row, opening the Model or the wire profile it is in.
  import { tick, untrack } from 'svelte';

  import { saveCustomProvider } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { isSettingsConflict } from '$lib/settingsSave.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import CustomModelEntry from './CustomModelEntry.svelte';

  const noop = () => {};
  const PROVIDER_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
  const NO_ERRORS = Object.freeze({ models: [] });

  let {
    provider = null,
    onSaved = noop,
    onToast = noop,
    onClose = noop,
  } = $props();

  const ADAPTER_OPTIONS = [
    {
      value: 'openai_compatible',
      label: t('settings.providers.custom.adapterOpenAiCompatible'),
    },
  ];
  const AUTH_OPTIONS = [
    { value: 'api_key', label: t('settings.providers.custom.authApiKey') },
    { value: 'none', label: t('settings.providers.custom.authNone') },
  ];
  // The wire profile fields most endpoints need; names are code values.
  const WIRE_FIELDS = [
    {
      name: 'reasoning.dialect',
      text: t('settings.providers.custom.wireFieldDialect'),
    },
    {
      name: 'reasoning.levels',
      text: t('settings.providers.custom.wireFieldLevels'),
    },
    {
      name: 'reasoning.off',
      text: t('settings.providers.custom.wireFieldOff'),
    },
    {
      name: 'reasoning.unset',
      text: t('settings.providers.custom.wireFieldUnset'),
    },
    {
      name: 'reasoning.supported',
      text: t('settings.providers.custom.wireFieldSupported'),
    },
    {
      name: 'response.reasoning_fields',
      text: t('settings.providers.custom.wireFieldResponse'),
    },
    {
      name: 'replay.history_field',
      text: t('settings.providers.custom.wireFieldHistory'),
    },
    {
      name: 'replay.scope',
      text: t('settings.providers.custom.wireFieldScope'),
    },
    {
      name: 'replay.fidelity',
      text: t('settings.providers.custom.wireFieldFidelity'),
    },
    {
      name: 'request.output_limit_field',
      text: t('settings.providers.custom.wireFieldOutputLimit'),
    },
    {
      name: 'request.parameters',
      text: t('settings.providers.custom.wireFieldParameters'),
    },
  ];

  let nextModelKey = 0;

  function csv(values) {
    return Array.isArray(values) ? values.join(', ') : '';
  }

  function modelDraft(modelId = '', model = {}) {
    const capabilities = model?.capabilities ?? {};
    nextModelKey += 1;
    return {
      key: nextModelKey,
      modelId,
      name: model?.name ?? modelId,
      contextWindow:
        model?.context_window === null || model?.context_window === undefined
          ? ''
          : String(model.context_window),
      maxOutputTokens:
        model?.max_output_tokens === null ||
        model?.max_output_tokens === undefined
          ? ''
          : String(model.max_output_tokens),
      vision: capabilities.vision === true,
      tools: capabilities.tools !== false,
      jsonMode: capabilities.json_mode === true,
      reasoning: capabilities.reasoning === true,
      inputModalities: csv(capabilities.input_modalities ?? ['text']),
      outputModalities: csv(capabilities.output_modalities ?? ['text']),
      taskTypes: csv(capabilities.task_types),
      supportedParameters: csv(capabilities.supported_parameters),
      supportedVoices: csv(capabilities.supported_voices),
      taskOptions:
        capabilities.task_options &&
        typeof capabilities.task_options === 'object'
          ? capabilities.task_options
          : {},
    };
  }

  function initialModels() {
    return Object.entries(provider?.models ?? {}).map(([modelId, model]) =>
      modelDraft(modelId, model),
    );
  }

  function initialWireText() {
    const wire = provider?.wire;
    return wire === undefined || wire === null
      ? ''
      : JSON.stringify(wire, null, 2);
  }

  // The wire block is optional JSON: empty means no block (the Provider uses
  // the protocol defaults). The server checks its fields on save.
  function parseWire(text) {
    const trimmed = String(text ?? '').trim();
    if (!trimmed) {
      return { value: undefined, error: '' };
    }
    let value;
    try {
      value = JSON.parse(trimmed);
    } catch (error) {
      return {
        value: undefined,
        error: t('settings.providers.custom.wireInvalidJson', {
          detail: error.message,
        }),
      };
    }
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return {
        value: undefined,
        error: t('settings.providers.custom.wireNotObject'),
      };
    }
    return { value, error: '' };
  }

  let providerId = $state(untrack(() => provider?.id ?? ''));
  let name = $state(untrack(() => provider?.name ?? ''));
  let adapter = $state(untrack(() => provider?.adapter ?? 'openai_compatible'));
  let baseUrl = $state(untrack(() => provider?.base_url ?? ''));
  let auth = $state(untrack(() => provider?.auth ?? 'api_key'));
  let apiKey = $state('');
  let modelsEndpoint = $state(
    untrack(() => provider?.models_endpoint ?? '/models'),
  );
  let models = $state(untrack(initialModels));
  let expandedModels = $state([]);
  let wireText = $state(untrack(initialWireText));
  let wireOpen = $state(untrack(() => initialWireText() !== ''));
  let wireIssues = $state([]);
  let attempted = $state(false);
  let saving = $state(false);
  let errorMessage = $state('');
  let errorBanner = $state();
  let editing = $derived(provider !== null);
  let wireError = $derived(parseWire(wireText).error);
  // Field problems show once Save was clicked and then follow every edit.
  let errors = $derived(attempted ? collectErrors() : NO_ERRORS);

  function isExpanded(model) {
    return expandedModels.includes(model.key);
  }

  function toggleModel(model) {
    expandedModels = isExpanded(model)
      ? expandedModels.filter((key) => key !== model.key)
      : [...expandedModels, model.key];
  }

  async function addModel() {
    const draft = modelDraft();
    models = [...models, draft];
    expandedModels = [...expandedModels, draft.key];
    await tick();
    document.getElementById(`custom-model-${models.length - 1}-id`)?.focus();
  }

  function removeModel(index) {
    const removed = models[index];
    models = models.filter((_, modelIndex) => modelIndex !== index);
    expandedModels = expandedModels.filter((key) => key !== removed?.key);
    errorMessage = '';
  }

  function updateModel(index, patch) {
    models = models.map((model, modelIndex) =>
      modelIndex === index ? { ...model, ...patch } : model,
    );
    errorMessage = '';
  }

  function parseCsv(value) {
    return [
      ...new Set(
        String(value ?? '')
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      ),
    ];
  }

  function optionalPositiveInteger(value) {
    const normalized = String(value ?? '').trim();
    return normalized ? Number(normalized) : null;
  }

  function isOptionalPositiveInteger(value) {
    const parsed = optionalPositiveInteger(value);
    return parsed === null || (Number.isInteger(parsed) && parsed > 0);
  }

  function isValidBaseUrl(value) {
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      return false;
    }
    return (
      ['http:', 'https:'].includes(parsed.protocol) &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash
    );
  }

  function collectErrors() {
    const found = { models: [] };
    if (!providerId.trim()) {
      found.id = t('settings.providers.custom.validationIdRequired');
    } else if (!PROVIDER_ID_PATTERN.test(providerId.trim())) {
      found.id = t('settings.providers.custom.validationId');
    }
    if (!name.trim()) {
      found.name = t('settings.providers.custom.validationName');
    }
    if (!isValidBaseUrl(baseUrl.trim())) {
      found.baseUrl = t('settings.providers.custom.validationBaseUrl');
    }
    const seen = [];
    for (const model of models) {
      const modelErrors = {};
      const modelId = model.modelId.trim();
      if (!modelId || modelId.includes('::')) {
        modelErrors.id = t('settings.providers.custom.validationModelId');
      } else if (seen.includes(modelId)) {
        modelErrors.id = t(
          'settings.providers.custom.validationDuplicateModel',
        );
      }
      seen.push(modelId);
      if (!isOptionalPositiveInteger(model.contextWindow)) {
        modelErrors.context = t(
          'settings.providers.custom.validationPositiveInteger',
        );
      }
      if (!isOptionalPositiveInteger(model.maxOutputTokens)) {
        modelErrors.output = t(
          'settings.providers.custom.validationPositiveInteger',
        );
      }
      found.models.push(modelErrors);
    }
    return found;
  }

  // The first control with a problem, in reading order.
  function firstInvalidControl(found) {
    if (found.id) return 'custom-provider-id';
    if (found.name) return 'custom-provider-name';
    if (found.baseUrl) return 'custom-provider-base-url';
    for (const [index, modelErrors] of found.models.entries()) {
      for (const field of ['id', 'context', 'output']) {
        if (modelErrors[field]) return `custom-model-${index}-${field}`;
      }
    }
    return wireError ? 'custom-provider-wire' : '';
  }

  async function revealProblems(found) {
    const modelsWithErrors = models.filter((_, index) =>
      Object.values(found.models[index] ?? {}).some(Boolean),
    );
    expandedModels = [
      ...new Set([
        ...expandedModels,
        ...modelsWithErrors.map((model) => model.key),
      ]),
    ];
    if (wireError) {
      wireOpen = true;
    }
    await tick();
    const controlId = firstInvalidControl(found);
    document.getElementById(controlId)?.focus();
  }

  function buildProvider() {
    const id = providerId.trim();
    const modelMap = {};
    for (const model of models) {
      const modelId = model.modelId.trim();
      modelMap[modelId] = {
        name: model.name.trim() || modelId,
        context_window: optionalPositiveInteger(model.contextWindow),
        max_output_tokens: optionalPositiveInteger(model.maxOutputTokens),
        capabilities: {
          vision: model.vision,
          tools: model.tools,
          json_mode: model.jsonMode,
          reasoning: model.reasoning,
          input_modalities: parseCsv(model.inputModalities),
          output_modalities: parseCsv(model.outputModalities),
          task_types: parseCsv(model.taskTypes),
          supported_parameters: parseCsv(model.supportedParameters),
          supported_voices: parseCsv(model.supportedVoices),
          task_options: model.taskOptions,
        },
      };
    }

    const customProvider = {
      id,
      name: name.trim(),
      adapter,
      base_url: baseUrl.trim(),
      auth,
      models_endpoint: modelsEndpoint.trim() || null,
      defaults: provider?.defaults ?? {},
      models: modelMap,
    };
    const wire = parseWire(wireText);
    if (wire.value !== undefined) {
      customProvider.wire = wire.value;
    }
    return customProvider;
  }

  async function showError(message) {
    errorMessage = message;
    await tick();
    errorBanner?.scrollIntoView?.({ block: 'nearest' });
  }

  async function submit(event) {
    event.preventDefault();
    if (saving) {
      return;
    }

    attempted = true;
    const found = collectErrors();
    if (firstInvalidControl(found)) {
      await revealProblems(found);
      return;
    }

    saving = true;
    errorMessage = '';
    wireIssues = [];
    try {
      const params = { provider: buildProvider() };
      if (provider?.revision) {
        // The server refuses the save if the record changed after this modal opened.
        params.expected_revision = provider.revision;
      }
      if (auth === 'api_key' && apiKey.trim()) {
        params.api_key = apiKey.trim();
      }
      await saveCustomProvider(params);
      onToast({
        title: t('settings.providers.custom.saved'),
        variant: 'success',
      });
      await onSaved();
      onClose();
    } catch (error) {
      const issues = error.details?.data?.wire_issues;
      if (isSettingsConflict(error)) {
        showError(t('settings.providers.custom.changedElsewhere'));
      } else if (Array.isArray(issues) && issues.length > 0) {
        wireIssues = issues.map(String);
        wireOpen = true;
        showError(
          `${t('settings.providers.custom.saveError')} ${t(
            'settings.providers.custom.wireIssuesHint',
          )}`,
        );
      } else {
        showError(
          `${t('settings.providers.custom.saveError')} ${error.message}`,
        );
      }
    } finally {
      saving = false;
    }
  }

  function describedBy(field, hasHelp = true) {
    return (
      [
        hasHelp && `custom-provider-${field}-desc`,
        errors[field === 'base-url' ? 'baseUrl' : field] &&
          `custom-provider-${field}-error`,
      ]
        .filter(Boolean)
        .join(' ') || undefined
    );
  }
</script>

<Modal
  title={editing
    ? t('settings.providers.custom.editTitle')
    : t('settings.providers.custom.addTitle')}
  description={t('settings.providers.custom.intro')}
  labelledById="custom-provider-modal-title"
  size="lg"
  variant="sheet"
  closeDisabled={saving}
  {onClose}
>
  {#snippet body()}
    <form id="custom-provider-form" novalidate onsubmit={submit}>
      <div class="modal-body custom-provider-form">
        <section
          class="s-section"
          aria-labelledby="custom-provider-connection-title"
        >
          <div class="s-section__head">
            <h4 class="s-section__title" id="custom-provider-connection-title">
              {t('settings.providers.custom.connectionTitle')}
            </h4>
          </div>
          <div class="s-group">
            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="custom-provider-id">
                  {t('settings.providers.custom.id')}
                </label>
                <div class="s-row-desc" id="custom-provider-id-desc">
                  {t('settings.providers.custom.idHint')}
                </div>
                {#if errors.id}
                  <p
                    class="s-row-error"
                    id="custom-provider-id-error"
                    role="alert"
                  >
                    {errors.id}
                  </p>
                {/if}
              </div>
              <div class="s-row-control">
                <TextField
                  id="custom-provider-id"
                  code
                  value={providerId}
                  disabled={saving || editing}
                  invalid={Boolean(errors.id)}
                  aria-required="true"
                  aria-describedby={describedBy('id')}
                  onInput={(value) => {
                    providerId = value;
                    errorMessage = '';
                  }}
                />
              </div>
            </div>

            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="custom-provider-name">
                  {t('settings.providers.custom.name')}
                </label>
                {#if errors.name}
                  <p
                    class="s-row-error"
                    id="custom-provider-name-error"
                    role="alert"
                  >
                    {errors.name}
                  </p>
                {/if}
              </div>
              <div class="s-row-control">
                <TextField
                  id="custom-provider-name"
                  value={name}
                  disabled={saving}
                  invalid={Boolean(errors.name)}
                  aria-required="true"
                  aria-describedby={describedBy('name', false)}
                  onInput={(value) => {
                    name = value;
                    errorMessage = '';
                  }}
                />
              </div>
            </div>

            <div class="s-row">
              <div class="s-row-info">
                <label
                  class="s-row-label"
                  id="custom-provider-adapter-label"
                  for="custom-provider-adapter"
                >
                  {t('settings.providers.custom.adapter')}
                </label>
              </div>
              <div class="s-row-control">
                <Dropdown
                  id="custom-provider-adapter"
                  value={adapter}
                  options={ADAPTER_OPTIONS}
                  ariaLabelledby="custom-provider-adapter-label"
                  disabled={saving}
                  onValueChange={(value) => (adapter = value)}
                />
              </div>
            </div>

            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="custom-provider-base-url">
                  {t('settings.providers.custom.baseUrl')}
                </label>
                <div class="s-row-desc" id="custom-provider-base-url-desc">
                  {t('settings.providers.custom.baseUrlHint')}
                </div>
                {#if errors.baseUrl}
                  <p
                    class="s-row-error"
                    id="custom-provider-base-url-error"
                    role="alert"
                  >
                    {errors.baseUrl}
                  </p>
                {/if}
              </div>
              <div class="s-row-control">
                <TextField
                  id="custom-provider-base-url"
                  code
                  value={baseUrl}
                  placeholder={t(
                    'settings.providers.custom.baseUrlPlaceholder',
                  )}
                  disabled={saving}
                  invalid={Boolean(errors.baseUrl)}
                  aria-required="true"
                  aria-describedby={describedBy('base-url')}
                  onInput={(value) => {
                    baseUrl = value;
                    errorMessage = '';
                  }}
                />
              </div>
            </div>

            <div class="s-row">
              <div class="s-row-info">
                <label
                  class="s-row-label"
                  for="custom-provider-models-endpoint"
                >
                  {t('settings.providers.custom.modelsEndpoint')}
                </label>
                <div
                  class="s-row-desc"
                  id="custom-provider-models-endpoint-desc"
                >
                  {t('settings.providers.custom.modelsEndpointHint')}
                </div>
              </div>
              <div class="s-row-control">
                <TextField
                  id="custom-provider-models-endpoint"
                  code
                  value={modelsEndpoint}
                  placeholder={t(
                    'settings.providers.custom.modelsEndpointPlaceholder',
                  )}
                  disabled={saving}
                  aria-describedby="custom-provider-models-endpoint-desc"
                  onInput={(value) => {
                    modelsEndpoint = value;
                    errorMessage = '';
                  }}
                />
              </div>
            </div>
          </div>
        </section>

        <section class="s-section" aria-labelledby="custom-provider-auth-title">
          <div class="s-section__head">
            <h4 class="s-section__title" id="custom-provider-auth-title">
              {t('settings.providers.custom.auth')}
            </h4>
          </div>
          <div class="s-group">
            <div class="s-row">
              <div class="s-row-info">
                <label
                  class="s-row-label"
                  id="custom-provider-auth-label"
                  for="custom-provider-auth"
                >
                  {t('settings.providers.custom.authMethod')}
                </label>
              </div>
              <div class="s-row-control">
                <Dropdown
                  id="custom-provider-auth"
                  value={auth}
                  options={AUTH_OPTIONS}
                  ariaLabelledby="custom-provider-auth-label"
                  disabled={saving}
                  onValueChange={(value) => {
                    auth = value;
                    errorMessage = '';
                  }}
                />
              </div>
            </div>

            {#if auth === 'api_key'}
              <div class="s-row">
                <div class="s-row-info">
                  <label class="s-row-label" for="custom-provider-api-key">
                    {editing
                      ? t('settings.providers.custom.replaceApiKey')
                      : t('settings.providers.custom.apiKey')}
                  </label>
                  <div class="s-row-desc" id="custom-provider-api-key-desc">
                    {editing
                      ? t('settings.providers.custom.apiKeyHint')
                      : t('settings.providers.custom.apiKeyNewHint')}
                  </div>
                </div>
                <div class="s-row-control">
                  <TextField
                    id="custom-provider-api-key"
                    type="password"
                    autocomplete="off"
                    value={apiKey}
                    disabled={saving}
                    aria-describedby="custom-provider-api-key-desc"
                    onInput={(value) => {
                      apiKey = value;
                      errorMessage = '';
                    }}
                  />
                </div>
              </div>
            {/if}
          </div>
        </section>

        <section
          class="s-section"
          aria-labelledby="custom-provider-models-title"
        >
          <div class="s-section__head">
            <h4 class="s-section__title" id="custom-provider-models-title">
              {t('settings.providers.custom.modelsTitle')}
            </h4>
            <InfoHint
              text={t('settings.providers.custom.modelsHint')}
              ariaLabel={t('settings.providers.custom.modelsHintAria')}
            />
            <div class="s-section__aside">
              <Button variant="secondary" disabled={saving} onClick={addModel}>
                {t('settings.providers.custom.addModel')}
              </Button>
            </div>
          </div>
          <div class="s-group">
            {#if models.length === 0}
              <p class="s-group__block s-group__note">
                {t('settings.providers.custom.noModels')}
              </p>
            {/if}
            {#each models as model, index (model.key)}
              <CustomModelEntry
                {model}
                {index}
                expanded={isExpanded(model)}
                errors={errors.models[index] ?? {}}
                disabled={saving}
                onToggle={() => toggleModel(model)}
                onChange={(patch) => updateModel(index, patch)}
                onRemove={() => removeModel(index)}
              />
            {/each}
          </div>
        </section>

        <section
          class="s-section"
          aria-labelledby="custom-provider-advanced-title"
        >
          <div class="s-section__head">
            <h4 class="s-section__title" id="custom-provider-advanced-title">
              {t('settings.providers.custom.advancedTitle')}
            </h4>
          </div>
          <div class="s-group">
            <button
              type="button"
              class="s-row s-row--compact s-disclosure-row custom-wire__toggle"
              aria-expanded={wireOpen}
              aria-controls="custom-provider-wire-block"
              onclick={() => (wireOpen = !wireOpen)}
            >
              <span class="s-row-info">
                <span class="s-row-label" id="custom-provider-wire-title">
                  <span
                    class="disclosure-chevron"
                    class:disclosure-chevron--open={wireOpen}
                    aria-hidden="true"
                  ></span>
                  {t('settings.providers.custom.wireTitle')}
                </span>
                <span class="s-row-desc">
                  {t('settings.providers.custom.wireIntro')}
                </span>
              </span>
              <span class="s-disclosure__meta">
                {wireText.trim()
                  ? t('settings.providers.custom.wireStateCustom')
                  : t('settings.providers.custom.wireStateDefault')}
              </span>
            </button>
            <div
              class="s-group__block custom-wire"
              id="custom-provider-wire-block"
              role="group"
              aria-labelledby="custom-provider-wire-title"
              hidden={!wireOpen}
            >
              <p class="s-group__note">
                {t('settings.providers.custom.wireWhen')}
              </p>
              <FormField controlId="custom-provider-wire" error={wireError}>
                {#snippet labelContent()}
                  {t('settings.providers.custom.wireLabel')}
                  <InfoHint
                    text={t('settings.providers.custom.wireHint')}
                    ariaLabel={t('settings.providers.custom.wireHintAria')}
                  />
                {/snippet}
                {#snippet children(field)}
                  <TextArea
                    id={field.controlId}
                    code
                    rows={10}
                    value={wireText}
                    invalid={field.invalid || wireIssues.length > 0}
                    placeholder={t('settings.providers.custom.wirePlaceholder')}
                    disabled={saving}
                    aria-describedby={field.describedBy}
                    onInput={(value) => {
                      wireText = value;
                      wireIssues = [];
                      errorMessage = '';
                    }}
                  />
                {/snippet}
              </FormField>
              {#if wireIssues.length > 0}
                <div class="custom-wire__issues" role="alert">
                  <p>{t('settings.providers.custom.wireIssues')}</p>
                  <ul>
                    {#each wireIssues as issue, index (`${index}-${issue}`)}
                      <li><code>{issue}</code></li>
                    {/each}
                  </ul>
                </div>
              {/if}
              <details class="custom-wire__reference">
                <summary
                  >{t('settings.providers.custom.wireFieldsTitle')}</summary
                >
                <dl>
                  {#each WIRE_FIELDS as field (field.name)}
                    <div>
                      <dt><code>{field.name}</code></dt>
                      <dd>{field.text}</dd>
                    </div>
                  {/each}
                </dl>
              </details>
            </div>
          </div>
        </section>

        {#if errorMessage}
          <div bind:this={errorBanner}>
            <Banner variant="error" role="alert">{errorMessage}</Banner>
          </div>
        {/if}
      </div>

      <div class="modal-footer">
        <Button variant="secondary" disabled={saving} onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button type="submit" variant="primary" disabled={saving}>
          {saving ? t('common.saving') : t('common.save')}
        </Button>
      </div>
    </form>
  {/snippet}
</Modal>

<style>
  /* The wire profile toggle carries a description under its label; both
     line up after the chevron. */
  .custom-wire__toggle .s-row-label {
    display: flex;
    align-items: center;
  }

  .custom-wire__toggle .s-row-desc {
    display: block;
    padding-left: 15px;
    color: var(--text-lo);
    font-weight: 400;
  }

  .custom-wire {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .custom-wire[hidden] {
    display: none;
  }

  .custom-wire > .s-group__note {
    margin: 0;
  }

  .custom-wire__issues {
    display: grid;
    gap: 4px;
    padding: 10px 14px;
    border: 1px solid var(--red);
    border-radius: var(--r-md);
  }

  .custom-wire__issues p {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .custom-wire__issues ul {
    display: grid;
    gap: 4px;
    margin: 0;
    padding-left: 18px;
  }

  .custom-wire__issues li {
    color: var(--red);
    font-size: var(--fs-body-sm);
    overflow-wrap: anywhere;
  }

  .custom-wire__reference > :global(summary) {
    display: inline-flex;
    align-items: center;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    font-weight: 500;
    cursor: pointer;
  }

  .custom-wire__reference > :global(summary:hover) {
    color: var(--text-hi);
  }

  .custom-wire__reference dl {
    display: grid;
    gap: 10px;
    margin: 12px 0 0;
  }

  .custom-wire__reference dt {
    margin: 0;
  }

  .custom-wire__reference dt code {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .custom-wire__reference dd {
    margin: 2px 0 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }
</style>
