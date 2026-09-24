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
      translate: t,
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
      translate: t,
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
      emptyLabel: t('projects.manage.defaultAgentEmpty', 'No project default'),
      unavailableLabel: (agentId) =>
        t(
          'projects.manage.defaultAgentUnavailable',
          '{agentId} (not in team)',
          { agentId },
        ),
    }),
  );

  let thinkingEffortOptions = $derived([
    {
      value: PROJECT_THINKING_EFFORT_NO_DEFAULT,
      label: t('projects.manage.noThinkingEffort', 'No project default'),
    },
    {
      value: '',
      label: defaultThinkingEffortInheritLabel(),
    },
    ...PROJECT_THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: t(`agents.form.thinkingEffortOption.${option}`, option),
    })),
  ]);

  let temperatureIsInherit = $derived(
    projectsState.editForm.default_temperature === '',
  );

  function defaultModelInheritLabel() {
    const value = globalDefaultText('model');
    if (value) {
      return t('inherit.option', 'Inherited: {value} (global default)', {
        value,
      });
    }
    return t('inherit.optionNotConfigured', 'Inherit (not configured)');
  }

  function defaultThinkingEffortInheritLabel() {
    const value = globalDefaultText('thinking_effort');
    if (value) {
      return t('inherit.option', 'Inherited: {value} (global default)', {
        value,
      });
    }
    return t('inherit.optionProviderDefault', 'Inherit (provider default)');
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
          {t('projects.repositorySection', 'Repository')}
        </h3>
      </header>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-name">
                {t('projects.manage.displayName', 'Display name')}
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
                {t('projects.manage.sourceFormat', 'Source format')}
              </label>
              <div class="s-row-desc">
                {t(
                  'projects.manage.sourceFormatHelp',
                  'Where this project’s agents and skills come from. Switching re-derives the team and skills from the other ecosystem’s directories; sessions are kept.',
                )}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-source-format"
                value={projectsState.editForm.source_format}
                options={sourceFormatOptions}
                ariaLabel={t('projects.manage.sourceFormat', 'Source format')}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('source_format', value)}
              />
            </div>
          </div>
          <details class="s-disclosure projects-repository-actions">
            <summary>
              {t('projects.repositoryActions', 'Repository management')}
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
          {t('projects.defaultsSection', 'Agent defaults')}
        </h3>
      </header>
      <p class="s-section__desc">
        {t(
          'projects.defaultsSummary',
          'Shared starting values for the Team. Individual Agents can override them.',
        )}
      </p>
      <div class="s-section__body">
        <div class="s-group">
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-agent">
                {t('projects.manage.defaultAgent', 'Default agent')}
              </label>
              <div class="s-row-desc">
                {t(
                  'projects.manage.defaultAgentHelp',
                  'The team agent preselected when you open this project in Chat.',
                )}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-agent"
                value={projectsState.editForm.default_agent}
                options={agentOptions}
                placeholder={t(
                  'projects.manage.defaultAgentEmpty',
                  'No project default',
                )}
                ariaLabel={t('projects.manage.defaultAgent', 'Default agent')}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('default_agent', value)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-model">
                {t('projects.manage.defaultModel', 'Default model')}
              </label>
              <div class="s-row-desc">
                {t(
                  'projects.manage.defaultModelHelp',
                  'Used by team agents that do not declare their own model. Resolution order: per-agent override → the agent’s own value → this project default → the global default.',
                )}
              </div>
              <Button
                variant="tertiary"
                class="projects-inherit-link"
                onClick={navigateToAgentDefaults}
              >
                {t('inherit.editGlobalDefaults', 'Edit global defaults')}
              </Button>
            </div>
            <div class="s-row-control">
              <SearchableDropdown
                id="project-edit-model"
                value={modelSelectValue}
                options={modelOptions}
                placeholder={defaultModelInheritLabel()}
                searchPlaceholder={t(
                  'projects.manage.modelSearchPlaceholder',
                  'Filter models…',
                )}
                emptyLabel={t(
                  'projects.manage.modelSearchEmpty',
                  'No models match',
                )}
                ariaLabel={t('projects.manage.defaultModel', 'Default model')}
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
                {t(
                  'projects.manage.defaultThinkingEffort',
                  'Default thinking effort',
                )}
              </label>
              <div class="s-row-desc">
                {t(
                  'projects.manage.defaultThinkingEffortHelp',
                  'Used by team agents that do not set their own thinking effort. Same resolution order as the default model.',
                )}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="project-edit-thinking-effort"
                value={projectsState.editForm.default_thinking_effort}
                options={thinkingEffortOptions}
                ariaLabel={t(
                  'projects.manage.defaultThinkingEffort',
                  'Default thinking effort',
                )}
                triggerClass="projects-dropdown"
                onValueChange={(value) =>
                  updateEditField('default_thinking_effort', value)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="project-edit-temperature">
                {t('projects.manage.defaultTemperature', 'Default temperature')}
              </label>
              <div class="s-row-desc">
                {t(
                  'projects.manage.defaultTemperatureHelp',
                  'Used by team agents that do not set their own temperature. Same resolution order as the default model.',
                )}
              </div>
              {#if temperatureIsInherit}
                <div class="s-row-desc projects-inherit-hint">
                  {#if globalDefaultText('temperature')}
                    {t('inherit.hint', 'Inherited: {value} (global default)', {
                      value: globalDefaultText('temperature'),
                    })}
                  {:else}
                    {t(
                      'inherit.hintProviderDefault',
                      'Provider default — nothing is set here or in the global defaults.',
                    )}
                  {/if}
                </div>
              {/if}
            </div>
            <div class="s-row-control projects-number-control">
              {#if !temperatureIsInherit}
                <Button
                  variant="tertiary"
                  tooltip={t(
                    'inherit.resetToInherit',
                    'Reset to inherited value',
                  )}
                  ariaLabel={t(
                    'inherit.resetToInherit',
                    'Reset to inherited value',
                  )}
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
                ariaLabel={t(
                  'projects.manage.defaultTemperature',
                  'Default temperature',
                )}
                onInput={(next) => updateEditField('default_temperature', next)}
              />
            </div>
          </div>
        </div>
      </div>
    </section>
  </form>
</div>
