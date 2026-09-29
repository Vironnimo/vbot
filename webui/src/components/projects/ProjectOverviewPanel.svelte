<script>
  import { t } from '$lib/i18n.js';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import {
    PROJECT_SOURCE_FORMATS,
    buildDefaultAgentOptions,
    PROJECT_THINKING_EFFORT_NO_DEFAULT,
    PROJECT_THINKING_EFFORT_OPTIONS,
  } from '$lib/projectsView.js';
  import {
    selectModelValue,
    filterModelSelectOptions,
    buildModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
  } from '$lib/modelSelection.js';
  import { formatLabel } from './projectLabels.js';
  let {
    projectsState = $bindable(),
    projectsController,
    repositoryActions,
    onNavigateToSettingsPanel,
    handleManualSave,
    trackModelDropdownOpen,
  } = $props();

  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: projectsState.availableModels,
      connections: projectsState.availableConnections,
      selectedModelValue: projectsState.editForm.default_model,
      emptyLabel: defaultModelInheritLabel(),
    }),
  );

  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: projectsState.showAllModels,
      selectedModelValue: projectsState.editForm.default_model,
    }),
  );

  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: projectsState.showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
    }),
  );

  let modelSelectValue = $derived(
    selectModelValue(projectsState.editForm.default_model, modelOptions),
  );

  let sourceFormatOptions = $derived(
    PROJECT_SOURCE_FORMATS.map((formatKey) => ({
      value: formatKey,
      label: formatLabel(formatKey),
    })),
  );

  let agentOptions = $derived(
    buildDefaultAgentOptions({
      team: projectsState.activeTeam,
      currentValue: projectsState.editForm.default_agent,
      emptyLabel: t('projects.manage.defaultAgentEmpty'),
      unavailableLabel: (agentId) =>
        t('projects.manage.defaultAgentUnavailable', { agentId }),
    }),
  );

  let thinkingEffortOptions = $derived([
    {
      value: PROJECT_THINKING_EFFORT_NO_DEFAULT,
      label: t('projects.manage.noThinkingEffort'),
    },
    {
      value: '',
      label: defaultThinkingEffortInheritLabel(),
    },
    ...PROJECT_THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: t(`agents.form.thinkingEffortOption.${option}`),
    })),
  ]);

  let temperatureIsInherit = $derived(
    projectsState.editForm.default_temperature === '',
  );

  function defaultModelInheritLabel() {
    const value = globalDefaultText('model');
    if (value) {
      return t('inherit.option', {
        value,
      });
    }
    return t('inherit.optionNotConfigured');
  }

  function defaultThinkingEffortInheritLabel() {
    const value = globalDefaultText('thinking_effort');
    if (value) {
      return t('inherit.option', {
        value,
      });
    }
    return t('inherit.optionProviderDefault');
  }

  function globalDefaultText(fieldName) {
    const defaults = projectsState.globalAgentDefaults;
    const raw =
      defaults && typeof defaults === 'object' ? defaults[fieldName] : null;
    return raw === null || raw === undefined ? '' : String(raw).trim();
  }

  function updateEditField(field, value) {
    projectsController.updateEditField(field, value);
  }

  function navigateToAgentDefaults() {
    onNavigateToSettingsPanel('defaults');
  }

  function updateModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    projectsController.updateEditField(
      'default_model',
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  function clearDefaultTemperature() {
    projectsController.updateEditField('default_temperature', '');
  }
</script>

<div class="management-topic" id="project-detail-panel-overview">
  <form
    class="projects-settings-form"
    id="project-settings-form"
    onsubmit={handleManualSave}
  >
    <section class="s-section" aria-labelledby="project-section-repository">
      <header class="s-section__head">
        <h3 class="s-section__title" id="project-section-repository">
          {t('projects.repositorySection')}
        </h3>
      </header>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-name">
                {t('projects.manage.displayName')}
              </label>
            </div>
            <div class="s-row-control">
              <TextField
                id="project-edit-name"
                value={projectsState.editForm.display_name}
                onInput={(next) => updateEditField('display_name', next)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-source-format">
                {t('projects.manage.sourceFormat')}
              </label>
              <div class="s-row-desc">
                {t('projects.manage.sourceFormatHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-source-format"
                value={projectsState.editForm.source_format}
                options={sourceFormatOptions}
                ariaLabel={t('projects.manage.sourceFormat')}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('source_format', value)}
              />
            </div>
          </div>
          <details class="s-disclosure projects-repository-actions">
            <summary>
              {t('projects.repositoryActions')}
            </summary>
            <div class="s-disclosure__body">
              {@render repositoryActions?.()}
            </div>
          </details>
        </div>
      </div>
    </section>

    <section class="s-section" aria-labelledby="project-section-defaults">
      <header class="s-section__head">
        <h3 class="s-section__title" id="project-section-defaults">
          {t('projects.defaultsSection')}
        </h3>
      </header>
      <p class="s-section__desc">
        {t('projects.defaultsSummary')}
      </p>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-agent">
                {t('projects.manage.defaultAgent')}
              </label>
              <div class="s-row-desc">
                {t('projects.manage.defaultAgentHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-agent"
                value={projectsState.editForm.default_agent}
                options={agentOptions}
                placeholder={t('projects.manage.defaultAgentEmpty')}
                ariaLabel={t('projects.manage.defaultAgent')}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('default_agent', value)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-model">
                {t('projects.manage.defaultModel')}
              </label>
              <div class="s-row-desc">
                {t('projects.manage.defaultModelHelp')}
              </div>
              <Button
                variant="tertiary"
                class="projects-inherit-link"
                onClick={navigateToAgentDefaults}
              >
                {t('inherit.editGlobalDefaults')}
              </Button>
            </div>
            <div class="s-row-control">
              <SearchableDropdown
                id="project-edit-model"
                value={modelSelectValue}
                options={modelOptions}
                placeholder={defaultModelInheritLabel()}
                searchPlaceholder={t('projects.manage.modelSearchPlaceholder')}
                emptyLabel={t('projects.manage.modelSearchEmpty')}
                ariaLabel={t('projects.manage.defaultModel')}
                triggerClass="projects-dropdown"
                panelClass="projects-view__search-panel"
                footerActionLabel={modelFilterFooter}
                onFooterAction={() =>
                  (projectsState.showAllModels = !projectsState.showAllModels)}
                onOpenChange={trackModelDropdownOpen}
                onValueChange={updateModelSelection}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-thinking-effort">
                {t('projects.manage.defaultThinkingEffort')}
              </label>
              <div class="s-row-desc">
                {t('projects.manage.defaultThinkingEffortHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-thinking-effort"
                value={projectsState.editForm.default_thinking_effort}
                options={thinkingEffortOptions}
                ariaLabel={t('projects.manage.defaultThinkingEffort')}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('default_thinking_effort', value)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-temperature">
                {t('projects.manage.defaultTemperature')}
              </label>
              <div class="s-row-desc">
                {t('projects.manage.defaultTemperatureHelp')}
              </div>
              {#if temperatureIsInherit}
                <div class="s-row-desc projects-inherit-hint">
                  {#if globalDefaultText('temperature')}
                    {t('inherit.hint', {
                      value: globalDefaultText('temperature'),
                    })}
                  {:else}
                    {t('inherit.hintProviderDefault')}
                  {/if}
                </div>
              {/if}
            </div>
            <div class="s-row-control projects-number-control">
              {#if !temperatureIsInherit}
                <Button
                  variant="tertiary"
                  tooltip={globalDefaultText('temperature')
                    ? t('inherit.resetToValue', {
                        value: globalDefaultText('temperature'),
                      })
                    : t('inherit.resetToProviderDefault')}
                  ariaLabel={t('inherit.resetToInherit')}
                  onClick={clearDefaultTemperature}
                >
                  —
                </Button>
              {/if}
              <TextField
                id="project-edit-temperature"
                class="projects-number-input"
                inputmode="decimal"
                value={projectsState.editForm.default_temperature}
                ariaLabel={t('projects.manage.defaultTemperature')}
                onInput={(next) => updateEditField('default_temperature', next)}
              />
            </div>
          </div>
        </div>
      </div>
    </section>
  </form>
</div>
