<script>
  import { t } from '$lib/i18n.js';
  import { AGENT_FORM_MODE_EDIT } from '$lib/agentForm.js';
  import Button from '../ui/Button.svelte';
  import Dropdown from '../Dropdown.svelte';
  import TextField from '../ui/TextField.svelte';
  import AgentModelSettings from './AgentModelSettings.svelte';
  let {
    availableModels,
    availableConnections,
    projectOptions,
    projectCatalogError,
    onModelDropdownOpenChange,
    formValues = $bindable(),
    formMode,
    formErrors,
    fieldError,
    navigateToAgentDefaults,
    inheritSource,
    inheritDisplayValue,
  } = $props();

  let projectDropdownOptions = $derived(buildProjectDropdownOptions());

  function buildProjectDropdownOptions() {
    const options = [
      {
        value: '',
        label: t('agents.form.noProject'),
      },
      ...(Array.isArray(projectOptions) ? projectOptions : []),
    ];
    const selected = formValues.root_project_id;
    if (selected && !options.some((option) => option.value === selected)) {
      options.push({
        value: selected,
        label: t('agents.form.unavailableProject'),
        secondaryLabel: selected,
        disabled: true,
      });
    }
    return options;
  }
</script>

<div class="agents-view__part" id="agent-detail-panel-overview">
  <section class="s-section" aria-labelledby="agent-section-identity">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-identity">
        {t('agents.detail.identity')}
      </h3>
    </header>
    <div class="s-section__body">
      <div class="s-group">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-name">
              {t('agents.form.name')}
            </label>
            {#if formErrors.name}
              <p
                class="agents-view__row-error"
                id="agent-name-error"
                role="alert"
              >
                {fieldError('name')}
              </p>
            {/if}
          </div>
          <div class="s-row-control">
            <TextField
              id="agent-name"
              invalid={Boolean(formErrors.name)}
              aria-describedby={formErrors.name
                ? 'agent-name-error'
                : undefined}
              value={formValues.name}
              onInput={(next) => (formValues.name = next)}
            />
          </div>
        </div>

        {#if formMode === AGENT_FORM_MODE_EDIT}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="agent-project">
                {t('agents.form.project')}
              </label>
              <div class="s-row-desc" id="agent-project-help">
                {projectCatalogError
                  ? t('agents.form.projectUnavailableHelp')
                  : t('agents.form.projectHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="agent-project"
                value={formValues.root_project_id ?? ''}
                options={projectDropdownOptions}
                disabled={Boolean(projectCatalogError)}
                ariaLabel={t('agents.form.project')}
                ariaDescribedby="agent-project-help"
                triggerClass="agents-view__dropdown"
                onValueChange={(selectedValue) => {
                  formValues.root_project_id = selectedValue || null;
                }}
              />
            </div>
          </div>
        {/if}
      </div>
    </div>
  </section>

  <section class="s-section" aria-labelledby="agent-section-model">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-model">
        {t('agents.detail.model')}
      </h3>
      {#if formMode === AGENT_FORM_MODE_EDIT}
        <div class="s-section__aside">
          <Button variant="tertiary" onClick={navigateToAgentDefaults}>
            {t('inherit.editGlobalDefaults')}
          </Button>
        </div>
      {/if}
    </header>
    <div class="s-section__body">
      <AgentModelSettings
        bind:formValues
        {availableModels}
        {availableConnections}
        {formErrors}
        {fieldError}
        {inheritSource}
        {inheritDisplayValue}
        {onModelDropdownOpenChange}
      />
    </div>
  </section>
</div>
