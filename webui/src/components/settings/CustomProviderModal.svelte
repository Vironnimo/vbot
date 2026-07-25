<script>
  import { untrack } from 'svelte';

  import { saveCustomProvider } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';

  const noop = () => {};
  const PROVIDER_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

  let {
    provider = null,
    onSaved = noop,
    onToast = noop,
    onClose = noop,
  } = $props();

  function csv(values) {
    return Array.isArray(values) ? values.join(', ') : '';
  }

  function modelDraft(modelId = '', model = {}) {
    const capabilities = model?.capabilities ?? {};
    return {
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
  let saving = $state(false);
  let errorMessage = $state('');
  let editing = $derived(provider !== null);

  function addModel() {
    models = [...models, modelDraft()];
  }

  function removeModel(index) {
    models = models.filter((_, modelIndex) => modelIndex !== index);
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

  function optionalPositiveInteger(value, label) {
    const normalized = String(value ?? '').trim();
    if (!normalized) {
      return null;
    }
    const parsed = Number(normalized);
    if (!Number.isInteger(parsed) || parsed <= 0) {
      throw new Error(
        t(
          'settings.providers.custom.validationPositiveInteger',
          '{label} must be a positive whole number.',
          { label },
        ),
      );
    }
    return parsed;
  }

  function validateBaseUrl(value) {
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      throw new Error(
        t(
          'settings.providers.custom.validationBaseUrl',
          'Enter an absolute HTTP(S) endpoint URL.',
        ),
      );
    }
    if (
      !['http:', 'https:'].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error(
        t(
          'settings.providers.custom.validationBaseUrl',
          'Enter an absolute HTTP(S) endpoint URL.',
        ),
      );
    }
  }

  function buildProvider() {
    const id = providerId.trim();
    if (!PROVIDER_ID_PATTERN.test(id)) {
      throw new Error(
        t(
          'settings.providers.custom.validationId',
          'Provider id must use lowercase letters and digits in hyphen-separated segments.',
        ),
      );
    }
    if (!name.trim()) {
      throw new Error(
        t('settings.providers.custom.validationName', 'Enter a Provider name.'),
      );
    }
    validateBaseUrl(baseUrl.trim());

    const modelMap = {};
    for (const [index, model] of models.entries()) {
      const modelLabel = t(
        'settings.providers.custom.modelNumber',
        'Model {number}',
        { number: index + 1 },
      );
      const modelId = model.modelId.trim();
      if (!modelId || modelId.includes('::')) {
        throw new Error(
          t(
            'settings.providers.custom.validationModelId',
            'Every Model needs a wire id without "::".',
          ),
        );
      }
      if (modelMap[modelId]) {
        throw new Error(
          t(
            'settings.providers.custom.validationDuplicateModel',
            'Model ids must be unique.',
          ),
        );
      }
      modelMap[modelId] = {
        name: model.name.trim() || modelId,
        context_window: optionalPositiveInteger(
          model.contextWindow,
          `${modelLabel} ${t(
            'settings.providers.custom.contextWindow',
            'context window',
          ).toLocaleLowerCase()}`,
        ),
        max_output_tokens: optionalPositiveInteger(
          model.maxOutputTokens,
          `${modelLabel} ${t(
            'settings.providers.custom.maxOutput',
            'max output tokens',
          ).toLocaleLowerCase()}`,
        ),
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

    return {
      id,
      name: name.trim(),
      adapter,
      base_url: baseUrl.trim(),
      auth,
      models_endpoint: modelsEndpoint.trim() || null,
      defaults: provider?.defaults ?? {},
      models: modelMap,
    };
  }

  async function submit(event) {
    event.preventDefault();
    if (saving) {
      return;
    }

    let customProvider;
    try {
      customProvider = buildProvider();
    } catch (error) {
      errorMessage = error.message;
      return;
    }

    saving = true;
    errorMessage = '';
    try {
      const params = { provider: customProvider };
      if (auth === 'api_key' && apiKey.trim()) {
        params.api_key = apiKey.trim();
      }
      await saveCustomProvider(params);
      onToast({
        title: t('settings.providers.custom.saved', 'Custom Provider saved.'),
        variant: 'success',
      });
      await onSaved();
      onClose();
    } catch (error) {
      errorMessage = `${t(
        'settings.providers.custom.saveError',
        'Custom Provider could not be saved.',
      )} ${error.message}`;
    } finally {
      saving = false;
    }
  }
</script>

<Modal
  title={editing
    ? t('settings.providers.custom.editTitle', 'Edit Custom Provider')
    : t('settings.providers.custom.addTitle', 'Add Custom Provider')}
  labelledById="custom-provider-modal-title"
  class="custom-provider-modal"
  closeDisabled={saving}
  {onClose}
>
  {#snippet body()}
    <form
      id="custom-provider-form"
      class="modal-body custom-provider-form"
      onsubmit={submit}
    >
      <div class="custom-provider-form__intro">
        <span class="custom-provider-form__eyebrow">
          {t('settings.providers.custom.eyebrow', 'OpenAI-compatible endpoint')}
        </span>
        <p>
          {t(
            'settings.providers.custom.intro',
            'Connect an endpoint you control and describe the Models it exposes. Secrets are stored separately in the data-directory .env.',
          )}
        </p>
      </div>

      <div class="custom-provider-form__grid">
        <FormField
          controlId="custom-provider-id"
          label={t('settings.providers.custom.id', 'Provider id')}
          help={t(
            'settings.providers.custom.idHint',
            'Stable id used in Model references, for example local-ai.',
          )}
          required
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              value={providerId}
              disabled={saving || editing}
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              onInput={(value) => {
                providerId = value;
                errorMessage = '';
              }}
            />
          {/snippet}
        </FormField>

        <FormField
          controlId="custom-provider-name"
          label={t('settings.providers.custom.name', 'Name')}
          required
        >
          <TextField
            id="custom-provider-name"
            value={name}
            disabled={saving}
            onInput={(value) => {
              name = value;
              errorMessage = '';
            }}
          />
        </FormField>

        <FormField
          controlId="custom-provider-adapter"
          label={t('settings.providers.custom.adapter', 'Adapter')}
        >
          <select
            id="custom-provider-adapter"
            class="s-input"
            bind:value={adapter}
            disabled={saving}
          >
            <option value="openai_compatible">
              {t(
                'settings.providers.custom.adapterOpenAiCompatible',
                'OpenAI compatible',
              )}
            </option>
          </select>
        </FormField>

        <FormField
          controlId="custom-provider-auth"
          label={t('settings.providers.custom.auth', 'Authentication')}
        >
          <select
            id="custom-provider-auth"
            class="s-input"
            bind:value={auth}
            disabled={saving}
          >
            <option value="api_key">
              {t('settings.providers.custom.authApiKey', 'Bearer API key')}
            </option>
            <option value="none">
              {t('settings.providers.custom.authNone', 'No API key')}
            </option>
          </select>
        </FormField>
      </div>

      <FormField
        controlId="custom-provider-base-url"
        label={t('settings.providers.custom.baseUrl', 'Endpoint URL')}
        help={t(
          'settings.providers.custom.baseUrlHint',
          'Base URL including the API prefix, for example http://127.0.0.1:8080/v1.',
        )}
        required
        full
      >
        <TextField
          id="custom-provider-base-url"
          value={baseUrl}
          placeholder={t(
            'settings.providers.custom.baseUrlPlaceholder',
            'http://127.0.0.1:8080/v1',
          )}
          disabled={saving}
          onInput={(value) => {
            baseUrl = value;
            errorMessage = '';
          }}
        />
      </FormField>

      <div class="custom-provider-form__grid">
        <FormField
          controlId="custom-provider-models-endpoint"
          label={t(
            'settings.providers.custom.modelsEndpoint',
            'Model discovery path',
          )}
          help={t(
            'settings.providers.custom.modelsEndpointHint',
            'Optional OpenAI-compatible path. Leave empty to use manual Models only.',
          )}
        >
          <TextField
            id="custom-provider-models-endpoint"
            value={modelsEndpoint}
            placeholder={t(
              'settings.providers.custom.modelsEndpointPlaceholder',
              '/models',
            )}
            disabled={saving}
            onInput={(value) => {
              modelsEndpoint = value;
              errorMessage = '';
            }}
          />
        </FormField>

        {#if auth === 'api_key'}
          <FormField
            controlId="custom-provider-api-key"
            label={editing
              ? t(
                  'settings.providers.custom.replaceApiKey',
                  'Replace API key (optional)',
                )
              : t('settings.providers.custom.apiKey', 'API key (optional)')}
            help={t(
              'settings.providers.custom.apiKeyHint',
              'Write-only. Leave empty to keep the existing key or connect it later.',
            )}
          >
            <TextField
              id="custom-provider-api-key"
              type="password"
              autocomplete="off"
              value={apiKey}
              disabled={saving}
              onInput={(value) => {
                apiKey = value;
                errorMessage = '';
              }}
            />
          </FormField>
        {/if}
      </div>

      <section class="custom-models" aria-labelledby="custom-models-title">
        <div class="custom-models__head">
          <div>
            <h4 id="custom-models-title">
              {t('settings.providers.custom.modelsTitle', 'Manual Models')}
            </h4>
            <p>
              {t(
                'settings.providers.custom.modelsHint',
                'Manual facts override discovered Models with the same wire id.',
              )}
            </p>
          </div>
          <Button variant="secondary" disabled={saving} onClick={addModel}>
            {t('settings.providers.custom.addModel', 'Add Model')}
          </Button>
        </div>

        {#if models.length === 0}
          <div class="custom-models__empty">
            {t(
              'settings.providers.custom.noModels',
              'No manual Models. Use discovery or add one here.',
            )}
          </div>
        {/if}

        {#each models as model, index (`${index}-${model.modelId}`)}
          <article class="custom-model-card">
            <div class="custom-model-card__head">
              <span>
                {t('settings.providers.custom.modelNumber', 'Model {number}', {
                  number: index + 1,
                })}
              </span>
              <Button
                variant="danger"
                disabled={saving}
                onClick={() => removeModel(index)}
              >
                {t('common.remove', 'Remove')}
              </Button>
            </div>

            <div class="custom-provider-form__grid">
              <FormField
                controlId={`custom-model-${index}-id`}
                label={t('settings.providers.custom.modelId', 'Wire id')}
                required
              >
                <TextField
                  id={`custom-model-${index}-id`}
                  value={model.modelId}
                  disabled={saving}
                  onInput={(value) => updateModel(index, { modelId: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-name`}
                label={t('settings.providers.custom.modelName', 'Display name')}
              >
                <TextField
                  id={`custom-model-${index}-name`}
                  value={model.name}
                  disabled={saving}
                  onInput={(value) => updateModel(index, { name: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-context`}
                label={t(
                  'settings.providers.custom.contextWindow',
                  'Context window',
                )}
              >
                <TextField
                  id={`custom-model-${index}-context`}
                  type="number"
                  min="1"
                  value={model.contextWindow}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { contextWindow: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-output`}
                label={t(
                  'settings.providers.custom.maxOutput',
                  'Max output tokens',
                )}
              >
                <TextField
                  id={`custom-model-${index}-output`}
                  type="number"
                  min="1"
                  value={model.maxOutputTokens}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { maxOutputTokens: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-input-modalities`}
                label={t(
                  'settings.providers.custom.inputModalities',
                  'Input modalities',
                )}
                help={t(
                  'settings.providers.custom.inputModalitiesHint',
                  'Comma-separated: text, image, audio, file, video',
                )}
              >
                <TextField
                  id={`custom-model-${index}-input-modalities`}
                  value={model.inputModalities}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { inputModalities: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-output-modalities`}
                label={t(
                  'settings.providers.custom.outputModalities',
                  'Output modalities',
                )}
                help={t(
                  'settings.providers.custom.outputModalitiesHint',
                  'Comma-separated: text, image, speech, transcription, embeddings',
                )}
              >
                <TextField
                  id={`custom-model-${index}-output-modalities`}
                  value={model.outputModalities}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { outputModalities: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-tasks`}
                label={t('settings.providers.custom.taskTypes', 'Task types')}
                help={t(
                  'settings.providers.custom.taskTypesHint',
                  'Optional comma-separated explicit task types',
                )}
              >
                <TextField
                  id={`custom-model-${index}-tasks`}
                  value={model.taskTypes}
                  disabled={saving}
                  onInput={(value) => updateModel(index, { taskTypes: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-parameters`}
                label={t(
                  'settings.providers.custom.parameters',
                  'Supported parameters',
                )}
                help={t(
                  'settings.providers.custom.parametersHint',
                  'Optional comma-separated wire parameter names',
                )}
              >
                <TextField
                  id={`custom-model-${index}-parameters`}
                  value={model.supportedParameters}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { supportedParameters: value })}
                />
              </FormField>
              <FormField
                controlId={`custom-model-${index}-voices`}
                label={t(
                  'settings.providers.custom.voices',
                  'Supported voices',
                )}
                help={t(
                  'settings.providers.custom.voicesHint',
                  'Optional comma-separated voice ids',
                )}
              >
                <TextField
                  id={`custom-model-${index}-voices`}
                  value={model.supportedVoices}
                  disabled={saving}
                  onInput={(value) =>
                    updateModel(index, { supportedVoices: value })}
                />
              </FormField>
            </div>

            <div class="custom-model-card__capabilities">
              {#each [['tools', t('settings.providers.custom.tools', 'Tools'), model.tools], ['vision', t('settings.providers.custom.vision', 'Vision'), model.vision], ['jsonMode', t('settings.providers.custom.jsonMode', 'JSON mode'), model.jsonMode], ['reasoning', t('settings.providers.custom.reasoning', 'Reasoning'), model.reasoning]] as capability (capability[0])}
                <div class="custom-model-capability">
                  <span>{capability[1]}</span>
                  <Toggle
                    size="sm"
                    checked={capability[2]}
                    disabled={saving}
                    ariaLabel={t(
                      'settings.providers.custom.capabilityAria',
                      '{capability} for {model}',
                      {
                        capability: capability[1],
                        model:
                          model.modelId ||
                          t(
                            'settings.providers.custom.modelNumber',
                            'Model {number}',
                            { number: index + 1 },
                          ),
                      },
                    )}
                    onChange={(checked) =>
                      updateModel(index, { [capability[0]]: checked })}
                  />
                </div>
              {/each}
            </div>
          </article>
        {/each}
      </section>

      {#if errorMessage}
        <p class="custom-provider-form__error" role="alert">
          {errorMessage}
        </p>
      {/if}
    </form>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" disabled={saving} onClick={onClose}>
      {t('common.cancel', 'Cancel')}
    </Button>
    <Button
      type="submit"
      form="custom-provider-form"
      variant="primary"
      disabled={saving}
    >
      {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
    </Button>
  {/snippet}
</Modal>

<style>
  :global(.custom-provider-modal) {
    width: min(880px, calc(100vw - 2 * var(--space-lg)));
    max-height: min(860px, calc(100vh - 2 * var(--space-lg)));
  }

  .custom-provider-form {
    display: grid;
    gap: var(--space-lg);
    overflow-y: auto;
  }

  .custom-provider-form__intro {
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    background: var(--surface-2);
  }

  .custom-provider-form__intro p,
  .custom-models__head p {
    margin: var(--space-xs) 0 0;
    color: var(--text-med);
    font-size: 0.875rem;
    line-height: 1.45;
  }

  .custom-provider-form__eyebrow {
    color: var(--text-hi);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .custom-provider-form__grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-md);
  }

  .custom-models {
    display: grid;
    gap: var(--space-md);
    padding-top: var(--space-xs);
    border-top: 1px solid var(--border);
  }

  .custom-models__head,
  .custom-model-card__head,
  .custom-model-capability {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .custom-models__head h4 {
    margin: 0;
    font-size: 0.95rem;
  }

  .custom-models__empty {
    padding: var(--space-lg);
    border: 1px dashed var(--border);
    color: var(--text-med);
    text-align: center;
  }

  .custom-model-card {
    display: grid;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  .custom-model-card__head {
    color: var(--text-hi);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .custom-model-card__capabilities {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-sm);
  }

  .custom-model-capability {
    padding: var(--space-sm);
    border: 1px solid var(--border);
    color: var(--text-med);
    font-size: 0.8rem;
  }

  .custom-provider-form__error {
    margin: 0;
    color: var(--red);
    font-size: 0.875rem;
  }

  @media (max-width: 720px) {
    .custom-provider-form__grid,
    .custom-model-card__capabilities {
      grid-template-columns: 1fr;
    }
  }
</style>
