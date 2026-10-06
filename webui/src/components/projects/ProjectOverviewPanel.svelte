<script>
  import { t } from '$lib/i18n.js';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import SamplingSettings from '../sampling/SamplingSettings.svelte';
  import {
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
  import ProjectSourcesPanel from './ProjectSourcesPanel.svelte';
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

  // Project defaults serve every Team member's Model, so no single Model's
  // recommendation is offered here.
  let samplingFields = $derived({
    temperature: samplingField('temperature'),
    top_p: samplingField('top_p'),
  });

  function samplingField(fieldName) {
    const inherited = globalDefaultText(fieldName);
    return {
      value: projectsState.editForm[`default_${fieldName}`],
      hint: inherited
        ? t('inherit.hint', { value: inherited })
        : t('inherit.hintProviderDefault'),
      clearLabel: inherited
        ? t('inherit.resetToValue', { value: inherited })
        : t('inherit.resetToProviderDefault'),
    };
  }

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

    <ProjectSourcesPanel
      {projectsState}
      {projectsController}
      modelOptions={allModelOptions}
    />

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
          <SamplingSettings
            idPrefix="project-edit"
            fields={samplingFields}
            onChange={(fieldName, value) =>
              updateEditField(`default_${fieldName}`, value)}
            onClear={(fieldName) => updateEditField(`default_${fieldName}`, '')}
          />
        </div>
      </div>
    </section>
  </form>
</div>
