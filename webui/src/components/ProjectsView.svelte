<script>
  import { onDestroy, onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import ToggleChipList from './ui/ToggleChipList.svelte';
  import {
    PROJECT_SOURCE_FORMATS,
    PROJECT_THINKING_EFFORT_NO_DEFAULT,
    PROJECT_THINKING_EFFORT_OPTIONS,
    buildDefaultAgentOptions,
    buildSkillToggleSections,
    buildToolToggleList,
    createProjectsController,
    createProjectsState,
    hasManageChanges,
    memberFieldIsOverridden,
    needsRePoint,
    presentFormats,
    projectAgentTargetSummary,
    shouldSuggestClaudeMd,
  } from '$lib/projectsView.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import {
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import InfoHint from './ui/InfoHint.svelte';

  // Human-facing labels for the source-format vocabulary.
  const FORMAT_LABELS = Object.freeze({
    opencode: () => t('projects.format.opencode', 'OpenCode'),
    claude: () => t('projects.format.claude', 'Claude Code'),
  });

  function formatLabel(formatKey) {
    return FORMAT_LABELS[formatKey] ? FORMAT_LABELS[formatKey]() : formatKey;
  }

  function agentTargetPolicyText(member) {
    const summary = projectAgentTargetSummary(member, projectsState.activeTeam);
    if (summary.mode === 'unavailable') {
      return t(
        'projects.team.agentTargetsUnavailable',
        'Sub-Agent tools are not available to this Agent.',
      );
    }
    if (summary.mode === 'self') {
      return t(
        'projects.team.agentTargetsSelf',
        'Can call only itself in a separate Session.',
      );
    }
    if (summary.mode === 'all') {
      return t(
        'projects.team.agentTargetsAll',
        'Can call itself and every other Agent on this Project Team.',
      );
    }
    return t(
      'projects.team.agentTargetsLimited',
      'Can call itself plus: {agents}',
      {
        agents: summary.agents.join(', '),
      },
    );
  }

  // Maps each effective/override field to its section-header label key and the empty
  // wording (a null model reads "not configured", a null temperature/thinking
  // reads "provider default").
  const EFFECTIVE_FIELD_META = Object.freeze({
    model: {
      labelKey: 'projects.team.effectiveModel',
      labelFallback: 'Model',
      emptyKey: 'projects.team.valueNotConfigured',
      emptyFallback: 'not configured',
    },
    temperature: {
      labelKey: 'projects.team.effectiveTemperature',
      labelFallback: 'Temperature',
      emptyKey: 'projects.team.valueProviderDefault',
      emptyFallback: 'provider default',
    },
    thinking_effort: {
      labelKey: 'projects.team.effectiveThinkingEffort',
      labelFallback: 'Thinking effort',
      emptyKey: 'projects.team.valueProviderDefault',
      emptyFallback: 'provider default',
    },
  });

  const noop = () => {};

  let {
    selectedProjectId: preferredProjectId = '',
    onProjectSelected = noop,
    onToast = noop,
    onNavigateToSettingsPanel = noop,
    modelsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();

  const projectsState = $state(createProjectsState());
  const projectsController = createProjectsController({
    state: projectsState,
    translate: t,
    onProjectSelected: (projectId) => onProjectSelected(projectId),
    onToast: (toast) => onToast(toast),
  });

  const addFormatsPresent = $derived(
    projectsState.addDetect ? presentFormats(projectsState.addDetect) : [],
  );
  // Both formats found → the informed radio choice; exactly one → a quiet
  // "Detected" line (the server auto-detects the same); none → silent default.
  const addShowsFormatChoice = $derived(addFormatsPresent.length > 1);
  const addDetectedFormat = $derived(
    addFormatsPresent.length === 1 ? addFormatsPresent[0] : '',
  );
  const addSuggestsClaudeMd = $derived(
    projectsState.addDetect !== null &&
      shouldSuggestClaudeMd(projectsState.addDetect),
  );

  let hasProjects = $derived(projectsState.projects.length > 0);
  let canSubmitAdd = $derived(
    projectsState.addForm.cwd.trim().length > 0 && !projectsState.addingProject,
  );

  let selectedProject = $derived(projectsController.selectedProject());

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
  // The per-project source-format selector (exactly one format per project).
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

  // The project default thinking-effort options: the "no default" sentinel and
  // the inherit ('') choice wrap the shared effort ladder, reusing the agent
  // effort-level labels so there is no duplicate label catalog.
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

  // The sparse project.set changes the open form represents versus the saved
  // project — empty when the form matches what the server already holds.
  let pendingChanges = $derived(projectsController.pendingChanges());
  let saveDisabled = $derived(
    projectsState.editSaving || !hasManageChanges(pendingChanges),
  );

  let temperatureIsInherit = $derived(
    projectsState.editForm.default_temperature === '',
  );

  let toolToggleRows = $derived(
    buildToolToggleList({
      catalog: projectsState.toolCatalog,
      allowedTools: projectsState.editForm.allowed_tools,
    }),
  );
  let skillToggleSections = $derived(
    buildSkillToggleSections({
      projectSkills: projectsState.activeScanSkills.project,
      bundledSkills: projectsState.activeScanSkills.bundled,
      globalSkills: projectsState.activeScanSkills.global,
      skillsBundledEnabled: projectsState.editForm.skills_bundled_enabled,
      skillsGlobalEnabled: projectsState.editForm.skills_global_enabled,
      skillsProjectDisabled: projectsState.editForm.skills_project_disabled,
    }),
  );
  // The shared chip list keys off `allowed`; the toggle builders track it as
  // `enabled`, so map it across for each list.
  let toolChipItems = $derived(
    toolToggleRows.map((tool) => ({
      ...tool,
      allowed: tool.enabled,
      readiness_hint:
        tool.registered === false
          ? t(
              'projects.manage.unavailableToolHint',
              'This stored Tool Whitelist entry is not currently registered for Projects. Turn it off to remove the permission, or leave it on so the permission returns with the Tool.',
            )
          : tool.readiness_hint,
    })),
  );
  let projectSkillChips = $derived(
    skillToggleSections.project.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );
  let bundledSkillChips = $derived(
    skillToggleSections.bundled.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );
  let globalSkillChips = $derived(
    skillToggleSections.global.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );

  onMount(() => {
    void projectsController.initialize(preferredProjectId);
  });

  onDestroy(() => {
    projectsController.destroy();
  });

  // Auto-save the settings form once it has been idle for the debounce window.
  $effect(() => {
    if (saveDisabled) {
      return;
    }

    projectsController.scheduleAutoSave(() =>
      projectsController.saveSelectedProject(),
    );

    return () => {
      projectsController.clearAutoSave();
    };
  });

  // Reload the model catalog when the generic invalidation channel signals a
  // model/provider change (first run is a no-op: mount already loaded).
  $effect(() => {
    projectsController.updateModelsRefreshToken(modelsRefreshToken);
  });

  // Reload Project list/detail/scan state after another surface mutates the
  // server-owned Project catalog. The controller defers visible replacement
  // while this view owns an active form, picker, modal, or save.
  $effect(() => {
    projectsController.updateProjectsRefreshToken(projectsRefreshToken);
  });

  // Presentation-only labels and projections stay in the component. Project
  // lifecycle, mutations, reconciliation, and async state belong to the controller.
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

  function trackModelDropdownOpen(open) {
    projectsController.trackModelDropdownOpen(open);
  }

  function openAdd() {
    projectsController.openAdd();
  }

  function closeAdd() {
    projectsController.closeAdd();
  }

  function updateAddField(field, value) {
    projectsController.updateAddField(field, value);
  }

  function submitAdd(event) {
    event.preventDefault();
    void projectsController.submitAdd();
  }

  function selectProject(projectId) {
    projectsController.selectProject(projectId);
  }

  function refreshScan() {
    void projectsController.refreshScan();
  }

  function updateEditField(field, value) {
    projectsController.updateEditField(field, value);
  }

  function toggleTool(name, enabled) {
    projectsController.updateListField('allowed_tools', name, enabled);
  }

  function navigateToExtensions(_extensionName) {
    onNavigateToSettingsPanel('extensions');
  }

  function navigateToAgentDefaults() {
    onNavigateToSettingsPanel('defaults');
  }

  function toggleProjectSkill(name, active) {
    projectsController.updateListField(
      'skills_project_disabled',
      name,
      !active,
    );
  }

  function toggleBundledSkill(name, enabled) {
    projectsController.updateListField('skills_bundled_enabled', name, enabled);
  }

  function toggleGlobalSkill(name, enabled) {
    projectsController.updateListField('skills_global_enabled', name, enabled);
  }

  function resetToolsToDefaults() {
    projectsController.replaceListField(
      'allowed_tools',
      projectsState.defaultProjectTools,
    );
  }

  function setAllTools(enabled) {
    projectsController.replaceListField(
      'allowed_tools',
      enabled ? toolToggleRows.map((tool) => tool.name) : [],
    );
  }

  function setAllProjectSkills(enabled) {
    projectsController.replaceListField(
      'skills_project_disabled',
      enabled ? [] : skillToggleSections.project.map((skill) => skill.name),
    );
  }

  function setAllBundledSkills(enabled) {
    projectsController.replaceListField(
      'skills_bundled_enabled',
      enabled ? skillToggleSections.bundled.map((skill) => skill.name) : [],
    );
  }

  function setAllGlobalSkills(enabled) {
    projectsController.replaceListField(
      'skills_global_enabled',
      enabled ? skillToggleSections.global.map((skill) => skill.name) : [],
    );
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

  function handleManualSave(event) {
    event.preventDefault();
    void projectsController.saveSelectedProject({ manual: true });
  }

  function toggleMember(agentId) {
    projectsState.expandedMembers = {
      ...projectsState.expandedMembers,
      [agentId]: !projectsState.expandedMembers[agentId],
    };
  }

  function overrideDraft(agentId) {
    return projectsController.overrideDraft(agentId);
  }

  function updateOverrideDraft(agentId, field, value) {
    projectsController.updateOverrideDraft(agentId, field, value);
  }

  function updateOverrideModelSelection(agentId, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    projectsController.updateOverrideDraft(
      agentId,
      'model',
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  function effectiveDisplay(member, field) {
    const meta = EFFECTIVE_FIELD_META[field];
    const entry = member?.effective?.[field] ?? { value: null, source: null };
    const isEmpty = entry.value === null || entry.value === undefined;
    return {
      label: t(meta.labelKey, meta.labelFallback),
      value: isEmpty
        ? t(meta.emptyKey, meta.emptyFallback)
        : String(entry.value),
      isEmpty,
      sourceLabel: sourceLabel(entry.source),
    };
  }

  function sourceLabel(source) {
    switch (source) {
      case 'override':
        return t('projects.team.sourceOverride', 'override');
      case 'agent':
        return t('projects.team.sourceAgentFile', 'agent file (repo)');
      case 'project_default':
        return t('projects.team.sourceProjectDefault', 'project default');
      case 'global_default':
        return t('projects.team.sourceGlobalDefault', 'global default');
      default:
        return '';
    }
  }

  function overrideEffortOptions(member) {
    const reasoning = reasoningForModelValue(
      overrideDraft(member.agent_id).model ||
        member?.effective?.model?.value ||
        '',
      projectsState.availableModels,
    );
    return effortOptionsForReasoning(reasoning).map((option) => ({
      value: option,
      label:
        option === ''
          ? t(
              'projects.manage.providerThinkingEffortDefault',
              '— (provider default)',
            )
          : t(`agents.form.thinkingEffortOption.${option}`, option),
    }));
  }

  function overrideModelOptions(member) {
    const selectedModelValue = overrideDraft(member.agent_id).model;
    return filterModelSelectOptions(allOverrideModelOptions(member), {
      showAll: Boolean(projectsState.showAllOverrideModels[member.agent_id]),
      selectedModelValue,
    });
  }

  function allOverrideModelOptions(member) {
    return buildModelSelectOptions({
      models: projectsState.availableModels,
      connections: projectsState.availableConnections,
      selectedModelValue: overrideDraft(member.agent_id).model,
      emptyLabel: t('projects.team.overrideModelPlaceholder', 'No override'),
      translate: t,
    });
  }

  function overrideModelFilterFooter(member) {
    const showAll = Boolean(
      projectsState.showAllOverrideModels[member.agent_id],
    );
    return modelFilterFooterLabel({
      showAll,
      hiddenCount:
        allOverrideModelOptions(member).length -
        overrideModelOptions(member).length,
      translate: t,
    });
  }

  function toggleShowAllOverrideModels(agentId) {
    projectsState.showAllOverrideModels = {
      ...projectsState.showAllOverrideModels,
      [agentId]: !projectsState.showAllOverrideModels[agentId],
    };
  }

  function isOverrideBusy(agentId, field) {
    return projectsController.isOverrideBusy(agentId, field);
  }

  function canSetOverride(agentId, field) {
    return projectsController.canSetOverride(agentId, field);
  }

  function applySetOverride(agentId, field) {
    void projectsController.setMemberOverride(agentId, field);
  }

  function applyClearOverride(agentId, field) {
    void projectsController.clearMemberOverride(agentId, field);
  }

  function removeOne(project) {
    projectsController.openRemove(project);
  }

  function cancelRemove() {
    projectsController.cancelRemove();
  }

  function confirmRemove() {
    void projectsController.confirmRemove();
  }

  function openRePoint(project) {
    projectsController.openRePoint(project);
  }

  function closeRePoint() {
    projectsController.closeRePoint();
  }

  function submitRePoint(event) {
    event.preventDefault();
    void projectsController.submitRePoint();
  }

  function groupLabel(type) {
    return t(`projects.report.group.${type}`, type);
  }

  function addAutoLoadEntry() {
    const entry = projectsState.autoLoadDraft.trim();
    if (entry === '') {
      return;
    }
    if (!projectsState.editForm.auto_load.includes(entry)) {
      projectsController.updateEditField('auto_load', [
        ...projectsState.editForm.auto_load,
        entry,
      ]);
    }
    projectsState.autoLoadDraft = '';
  }

  function removeAutoLoadEntry(index) {
    projectsController.updateEditField(
      'auto_load',
      projectsState.editForm.auto_load.filter(
        (_, position) => position !== index,
      ),
    );
  }

  function handleAutoLoadKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      addAutoLoadEntry();
    }
  }
</script>

<section
  class="projects-view view active"
  aria-labelledby="projects-list-title"
>
  <div class="projects-layout">
    <aside
      class="project-list-pane secondary-pane"
      aria-labelledby="projects-list-title"
    >
      <div class="pane-header secondary-pane__header">
        <span id="projects-list-title" class="secondary-pane__title">
          {t('projects.title', 'Projects')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="primary"
            data-testid="project-add-open"
            onClick={openAdd}
          >
            <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
              <path d="M7 1v12M1 7h12" />
            </svg>
            {t('common.add', 'Add')}
          </Button>
        </div>
      </div>

      <div class="project-list-scroll secondary-pane__scroll secondary-list">
        {#if projectsState.listError}
          <Banner variant="error" role="alert">
            {projectsState.listError}
          </Banner>
        {/if}
        {#if projectsState.statusMessage}
          <p class="project-list-state" role="status">
            {projectsState.statusMessage}
          </p>
        {/if}

        {#if projectsState.loadingProjects}
          <p class="project-list-state" role="status">
            {t('projects.loading', 'Loading projectsState.projects…')}
          </p>
        {:else if !hasProjects}
          <EmptyState
            title={t('projects.emptyTitle', 'No projectsState.projects yet')}
            description={t(
              'projects.emptySubtitle',
              'Add a repository path below to create your first project.',
            )}
          />
        {:else}
          {#each projectsState.projects as project (project.project_id)}
            <button
              type="button"
              class="project-item secondary-list__item"
              class:active={project.project_id ===
                projectsState.selectedProjectId}
              data-testid={`project-toggle-${project.project_id}`}
              onclick={() => selectProject(project.project_id)}
            >
              <span class="project-item-inner">
                <span class="project-item-head">
                  <span class="project-item-name">
                    {project.display_name || project.project_id}
                  </span>
                  {#if needsRePoint(project)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title', 'Repository not found')}
                    </StatusChip>
                  {/if}
                </span>
                <span class="project-item-cwd" use:tooltip={project.cwd}>
                  {project.cwd}
                </span>
              </span>
            </button>
          {/each}
        {/if}
      </div>
    </aside>

    {#if !selectedProject}
      <div class="project-detail-pane">
        <EmptyState
          fill
          class="master-detail-empty"
          title={t(
            'projects.detail.empty',
            'Select a project to view and edit it.',
          )}
        />
      </div>
    {:else}
      {#key selectedProject.project_id}
        <div
          class="project-detail-pane"
          data-testid={`project-panel-${selectedProject.project_id}`}
        >
          <div class="project-detail-scroll">
            <div class="detail-top">
              <div>
                <div class="detail-heading-row">
                  <span class="detail-heading">
                    {selectedProject.display_name || selectedProject.project_id}
                  </span>
                  {#if needsRePoint(selectedProject)}
                    <StatusChip variant="error">
                      {t('projects.rePoint.title', 'Repository not found')}
                    </StatusChip>
                  {/if}
                </div>
                <div class="detail-sub">{selectedProject.cwd}</div>
              </div>
              <div class="detail-btns">
                <Button
                  variant="secondary"
                  data-testid="project-repository-rescan"
                  loading={projectsState.scanRefreshRequested}
                  disabled={projectsState.scanLoading}
                  onClick={refreshScan}
                >
                  {projectsState.scanRefreshRequested
                    ? t('projects.repository.rescanning', 'Scanning…')
                    : t('projects.repository.rescan', 'Rescan repository')}
                </Button>
                {#if needsRePoint(selectedProject)}
                  <Button
                    variant="secondary"
                    data-testid={`project-repoint-${selectedProject.project_id}`}
                    disabled={projectsState.editSaving}
                    onClick={() => openRePoint(selectedProject)}
                  >
                    {t('projects.rePoint.submit', 'Re-point')}
                  </Button>
                {/if}
                <Button
                  variant="danger"
                  data-testid={`project-remove-${selectedProject.project_id}`}
                  disabled={projectsState.removingProjectId ===
                    selectedProject.project_id || projectsState.editSaving}
                  onClick={() => removeOne(selectedProject)}
                >
                  {t('projects.remove', 'Remove')}
                </Button>
              </div>
            </div>

            {#if projectsState.editError}
              <Banner variant="error" role="alert">
                {projectsState.editError}
              </Banner>
            {/if}

            <!-- Section 1: Project settings -->
            <form
              class="detail-section detail-section--overflow"
              onsubmit={handleManualSave}
            >
              <div class="detail-section-title">
                {t('projects.detail.sectionSettings', 'Project settings')}
              </div>
              <div class="detail-section-body">
                <div class="projects-field-grid">
                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.displayName', 'Display name')}
                    </span>
                    <TextField
                      id="project-edit-name"
                      value={projectsState.editForm.display_name}
                      disabled={projectsState.editSaving}
                      onInput={(next) => updateEditField('display_name', next)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.sourceFormat', 'Source format')}
                      <InfoHint
                        text={t(
                          'projects.manage.sourceFormatHelp',
                          'Where this project’s agents and skills come from. Switching re-derives the team and skills from the other ecosystem’s directories; sessions are kept.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-source-format"
                      value={projectsState.editForm.source_format}
                      options={sourceFormatOptions}
                      ariaLabel={t(
                        'projects.manage.sourceFormat',
                        'Source format',
                      )}
                      disabled={projectsState.editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('source_format', value)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.defaultAgent', 'Default agent')}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultAgentHelp',
                          'The team agent preselected when you open this project in Chat.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-agent"
                      value={projectsState.editForm.default_agent}
                      options={agentOptions}
                      placeholder={t(
                        'projects.manage.defaultAgentEmpty',
                        'No project default',
                      )}
                      ariaLabel={t(
                        'projects.manage.defaultAgent',
                        'Default agent',
                      )}
                      disabled={projectsState.editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('default_agent', value)}
                    />
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t('projects.manage.defaultModel', 'Default model')}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultModelHelp',
                          'Used by team agents that do not declare their own model. Resolution order: per-agent override → the agent’s own value → this project default → the global default.',
                        )}
                      />
                    </span>
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
                      ariaLabel={t(
                        'projects.manage.defaultModel',
                        'Default model',
                      )}
                      disabled={projectsState.editSaving}
                      triggerClass="projects-dropdown"
                      panelClass="projects-view__search-panel"
                      footerActionLabel={modelFilterFooter}
                      onFooterAction={() =>
                        (projectsState.showAllModels =
                          !projectsState.showAllModels)}
                      onOpenChange={trackModelDropdownOpen}
                      onValueChange={updateModelSelection}
                    />
                    <Button
                      variant="tertiary"
                      class="projects-inherit-link"
                      onClick={navigateToAgentDefaults}
                    >
                      {t('inherit.editGlobalDefaults', 'Edit global defaults')}
                    </Button>
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t(
                        'projects.manage.defaultTemperature',
                        'Default temperature',
                      )}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultTemperatureHelp',
                          'Used by team agents that do not set their own temperature. Same resolution order as the default model.',
                        )}
                      />
                    </span>
                    <div class="projects-override-controls">
                      <TextField
                        id="project-edit-temperature"
                        class="projects-override-input"
                        inputmode="decimal"
                        value={projectsState.editForm.default_temperature}
                        disabled={projectsState.editSaving}
                        ariaLabel={t(
                          'projects.manage.defaultTemperature',
                          'Default temperature',
                        )}
                        onInput={(next) =>
                          updateEditField('default_temperature', next)}
                      />
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
                    </div>
                    {#if temperatureIsInherit}
                      {#if globalDefaultText('temperature')}
                        <small class="projects-inherit-hint">
                          {t(
                            'inherit.hint',
                            'Inherited: {value} (global default)',
                            { value: globalDefaultText('temperature') },
                          )}
                        </small>
                      {:else}
                        <small class="projects-inherit-hint">
                          {t(
                            'inherit.hintProviderDefault',
                            'Provider default — nothing is set here or in the global defaults.',
                          )}
                        </small>
                      {/if}
                    {/if}
                  </label>

                  <label class="projects-field">
                    <span class="projects-label">
                      {t(
                        'projects.manage.defaultThinkingEffort',
                        'Default thinking effort',
                      )}
                      <InfoHint
                        text={t(
                          'projects.manage.defaultThinkingEffortHelp',
                          'Used by team agents that do not set their own thinking effort. Same resolution order as the default model.',
                        )}
                      />
                    </span>
                    <Dropdown
                      id="project-edit-thinking-effort"
                      value={projectsState.editForm.default_thinking_effort}
                      options={thinkingEffortOptions}
                      ariaLabel={t(
                        'projects.manage.defaultThinkingEffort',
                        'Default thinking effort',
                      )}
                      disabled={projectsState.editSaving}
                      triggerClass="projects-dropdown"
                      onValueChange={(value) =>
                        updateEditField('default_thinking_effort', value)}
                    />
                  </label>
                </div>

                <div class="detail-btns projectsState.projects-save-row">
                  <Button
                    variant="primary"
                    type="submit"
                    data-testid={`project-save-${selectedProject.project_id}`}
                    disabled={projectsState.editSaving}
                  >
                    {projectsState.editSaving
                      ? t('projects.manage.saving', 'Saving…')
                      : t('projects.manage.save', 'Save changes')}
                  </Button>
                </div>
              </div>
            </form>

            <!-- Section 2: Auto-load files -->
            <div class="detail-section">
              <div class="detail-section-title">
                {t('projects.detail.sectionAutoLoad', 'Auto-load files')}
                <InfoHint
                  text={t(
                    'projects.detail.autoLoadInfo',
                    'These files are embedded into the system prompt of every session in this project — the agent always sees their full content, with higher weight than normal chat history, and they are never dropped or summarized by context compaction.\n\nPaths are relative to the project folder (absolute paths also work), files load in list order, and missing files are skipped. When an outside Identity Agent explicitly loads the project with the project Tool, the same files are returned as Project Context.',
                  )}
                />
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  {#if projectsState.editForm.auto_load.length > 0}
                    <ul class="projects-file-list">
                      {#each projectsState.editForm.auto_load as filePath, index (index)}
                        <li class="projects-file-row">
                          <span class="projects-file-name">{filePath}</span>
                          <button
                            type="button"
                            class="projects-file-remove"
                            data-testid={`project-auto-load-remove-${index}`}
                            disabled={projectsState.editSaving}
                            aria-label={t(
                              'projects.manage.autoLoadRemove',
                              'Remove {file}',
                              { file: filePath },
                            )}
                            onclick={() => removeAutoLoadEntry(index)}
                          >
                            ×
                          </button>
                        </li>
                      {/each}
                    </ul>
                  {:else}
                    <EmptyState
                      density="compact"
                      description={t(
                        'projects.manage.autoLoadEmpty',
                        'No auto-load files',
                      )}
                    />
                  {/if}
                  <div class="projects-file-add">
                    <TextField
                      id="project-edit-auto-load"
                      class="projects-file-input"
                      value={projectsState.autoLoadDraft}
                      placeholder={t(
                        'projects.manage.autoLoadPlaceholder',
                        'Add a file path…',
                      )}
                      disabled={projectsState.editSaving}
                      ariaLabel={t(
                        'projects.manage.autoLoad',
                        'Auto-load files',
                      )}
                      onInput={(next) => {
                        projectsState.autoLoadDraft = next;
                      }}
                      onkeydown={handleAutoLoadKeydown}
                    />
                    <Button
                      variant="secondary"
                      data-testid="project-auto-load-add"
                      disabled={projectsState.editSaving ||
                        projectsState.autoLoadDraft.trim().length === 0}
                      onClick={addAutoLoadEntry}
                    >
                      {t('projects.manage.autoLoadAdd', 'Add')}
                    </Button>
                  </div>
                </div>
              </div>
            </div>

            <!-- Section 3: Team -->
            <div class="detail-section">
              <div class="detail-section-title">
                <span class="projects-section-title-copy">
                  {t('projects.detail.sectionTeam', 'Team')}
                  <InfoHint
                    text={t(
                      'projects.detail.teamInfo',
                      'Agents discovered live in the project repository — where they are read from depends on the source format. The list is re-derived on open and re-scan; the repository is the source of truth, so vBot never copies or edits these agents.',
                    )}
                  />
                </span>
              </div>
              <div class="detail-section-body">
                {#if projectsState.activeReport && !projectsState.activeReport.clean}
                  <div class="projects-field">
                    <Banner variant="warn" role="status">
                      {t(
                        'projects.report.findingCount',
                        '{count} issues found',
                        {
                          count: projectsState.activeReport.findingCount,
                        },
                      )}
                    </Banner>
                    {#each projectsState.activeReport.groups as group (group.type)}
                      <div class="projects-finding-group">
                        <h4 class="projects-finding-title">
                          {groupLabel(group.type)}
                        </h4>
                        <ul class="projects-findings">
                          {#each group.findings as finding, index (`${group.type}-${index}`)}
                            <li class="projects-finding">
                              <span class="projects-finding-detail">
                                {finding.detail}
                              </span>
                              {#if finding.agent_id}
                                <span class="projects-finding-meta">
                                  {t(
                                    'projects.report.finding.agent',
                                    'Agent {agentId}',
                                    { agentId: finding.agent_id },
                                  )}
                                </span>
                              {/if}
                              {#if finding.source_path}
                                <span class="projects-finding-meta">
                                  {t(
                                    'projects.report.finding.source',
                                    'Source: {source}',
                                    { source: finding.source_path },
                                  )}
                                </span>
                              {/if}
                            </li>
                          {/each}
                        </ul>
                      </div>
                    {/each}
                  </div>
                {/if}

                {#if projectsState.scanLoading}
                  <p class="projects-scan-loading" role="status">
                    {t('projects.loading', 'Loading projectsState.projects…')}
                  </p>
                {:else if projectsState.activeTeam.length === 0}
                  <EmptyState
                    density="compact"
                    description={t(
                      'projects.team.empty',
                      'No agents discovered in this repository yet. An empty project is valid — add agent files to the repo to build a team.',
                    )}
                  />
                {:else}
                  <ul class="projects-team">
                    {#each projectsState.activeTeam as member (member.agent_id)}
                      {@const expanded =
                        projectsState.expandedMembers[member.agent_id] === true}
                      {@const summary = effectiveDisplay(member, 'model')}
                      <li
                        class="projects-team-member"
                        class:projectsState.projects-team-member--expanded={expanded}
                        data-testid={`project-team-member-${member.agent_id}`}
                      >
                        <button
                          type="button"
                          class="projects-team-header"
                          data-testid={`project-team-toggle-${member.agent_id}`}
                          aria-expanded={expanded}
                          onclick={() => toggleMember(member.agent_id)}
                        >
                          <svg
                            class="projects-team-chevron"
                            class:projectsState.projects-team-chevron--open={expanded}
                            viewBox="0 0 12 12"
                            width="11"
                            height="11"
                            aria-hidden="true"
                          >
                            <path d="M4 2l4 4-4 4" />
                          </svg>
                          <span class="projects-team-headline">
                            <span class="projects-team-name">
                              {member.display_name}
                            </span>
                            {#if member.description}
                              <span class="projects-team-description">
                                {member.description}
                              </span>
                            {/if}
                          </span>
                          <span
                            class="projects-team-summary"
                            use:tooltip={summary.value}
                          >
                            {summary.value}
                          </span>
                        </button>

                        {#if expanded}
                          <div class="projects-team-detail">
                            <ul class="projects-effective-list">
                              {#each ['model', 'temperature', 'thinking_effort'] as field (field)}
                                {@const display = effectiveDisplay(
                                  member,
                                  field,
                                )}
                                <li class="projects-effective-row">
                                  <span class="projects-effective-label">
                                    {display.label}
                                  </span>
                                  <span
                                    class="projects-effective-value"
                                    class:projectsState.projects-effective-value--muted={display.isEmpty}
                                  >
                                    {display.value}
                                  </span>
                                  {#if display.sourceLabel}
                                    <span class="projects-effective-source">
                                      {t(
                                        'projects.team.fromSource',
                                        'from {source}',
                                        { source: display.sourceLabel },
                                      )}
                                    </span>
                                  {/if}
                                </li>
                              {/each}
                            </ul>

                            <div class="projects-overrides">
                              <!-- Model override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t('projects.team.effectiveModel', 'Model')}
                                </span>
                                <div class="projects-override-controls">
                                  <div class="projects-override-input">
                                    <SearchableDropdown
                                      id={`project-override-model-${member.agent_id}`}
                                      value={selectModelValue(
                                        overrideDraft(member.agent_id).model,
                                        overrideModelOptions(member),
                                      )}
                                      options={overrideModelOptions(member)}
                                      placeholder={t(
                                        'projects.team.overrideModelPlaceholder',
                                        'No override',
                                      )}
                                      searchPlaceholder={t(
                                        'projects.manage.modelSearchPlaceholder',
                                        'Filter models…',
                                      )}
                                      emptyLabel={t(
                                        'projects.manage.modelSearchEmpty',
                                        'No models match',
                                      )}
                                      ariaLabel={t(
                                        'projects.team.effectiveModel',
                                        'Model',
                                      )}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'model',
                                      )}
                                      triggerClass="projects-dropdown"
                                      panelClass="projects-view__search-panel"
                                      footerActionLabel={overrideModelFilterFooter(
                                        member,
                                      )}
                                      onFooterAction={() =>
                                        toggleShowAllOverrideModels(
                                          member.agent_id,
                                        )}
                                      onOpenChange={trackModelDropdownOpen}
                                      onValueChange={(value) =>
                                        updateOverrideModelSelection(
                                          member.agent_id,
                                          value,
                                        )}
                                    />
                                  </div>
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-model-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'model',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'model',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'model')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-model-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'model',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'model',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <!-- Temperature override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t(
                                    'projects.team.effectiveTemperature',
                                    'Temperature',
                                  )}
                                </span>
                                <div class="projects-override-controls">
                                  <TextField
                                    id={`project-override-temperature-${member.agent_id}`}
                                    class="projects-override-input"
                                    inputmode="decimal"
                                    value={overrideDraft(member.agent_id)
                                      .temperature}
                                    placeholder={t(
                                      'projects.team.overrideTemperaturePlaceholder',
                                      'e.g. 0.7',
                                    )}
                                    disabled={isOverrideBusy(
                                      member.agent_id,
                                      'temperature',
                                    )}
                                    ariaLabel={t(
                                      'projects.team.effectiveTemperature',
                                      'Temperature',
                                    )}
                                    onInput={(next) =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'temperature',
                                        next,
                                      )}
                                  />
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-temperature-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'temperature',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'temperature',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'temperature')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-temperature-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'temperature',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'temperature',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <!-- Thinking-effort override -->
                              <div class="projects-override-row">
                                <span class="projects-label">
                                  {t('projects.team.overrideLabel', 'Override')} ·
                                  {t(
                                    'projects.team.effectiveThinkingEffort',
                                    'Thinking effort',
                                  )}
                                </span>
                                <div class="projects-override-controls">
                                  <div class="projects-override-input">
                                    <Dropdown
                                      id={`project-override-thinking-${member.agent_id}`}
                                      value={overrideDraft(member.agent_id)
                                        .thinking_effort}
                                      options={overrideEffortOptions(member)}
                                      ariaLabel={t(
                                        'projects.team.effectiveThinkingEffort',
                                        'Thinking effort',
                                      )}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                      triggerClass="projects-dropdown"
                                      onValueChange={(value) =>
                                        updateOverrideDraft(
                                          member.agent_id,
                                          'thinking_effort',
                                          value,
                                        )}
                                    />
                                  </div>
                                  <Button
                                    variant="secondary"
                                    data-testid={`project-override-set-thinking-${member.agent_id}`}
                                    disabled={!canSetOverride(
                                      member.agent_id,
                                      'thinking_effort',
                                    )}
                                    onClick={() =>
                                      applySetOverride(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                  >
                                    {t(
                                      'projects.team.setOverride',
                                      'Set override',
                                    )}
                                  </Button>
                                  {#if memberFieldIsOverridden(member, 'thinking_effort')}
                                    <Button
                                      variant="tertiary"
                                      data-testid={`project-override-clear-thinking-${member.agent_id}`}
                                      disabled={isOverrideBusy(
                                        member.agent_id,
                                        'thinking_effort',
                                      )}
                                      onClick={() =>
                                        applyClearOverride(
                                          member.agent_id,
                                          'thinking_effort',
                                        )}
                                    >
                                      {t(
                                        'projects.team.clearOverride',
                                        'Clear override',
                                      )}
                                    </Button>
                                  {/if}
                                </div>
                              </div>

                              <div
                                class="projects-override-row projectsState.projects-override-row--policy"
                              >
                                <span class="projects-label">
                                  {t(
                                    'projects.team.compactionPolicy',
                                    'Compaction Policy',
                                  )}
                                </span>
                                {#if overrideDraft(member.agent_id).compaction_policy}
                                  <CompactionPolicyEditor
                                    value={overrideDraft(member.agent_id)
                                      .compaction_policy}
                                    onChange={(value) =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'compaction_policy',
                                        value,
                                      )}
                                    idPrefix={`project-compaction-${member.agent_id}`}
                                  />
                                  <div class="projects-override-controls">
                                    <Button
                                      variant="secondary"
                                      disabled={!canSetOverride(
                                        member.agent_id,
                                        'compaction_policy',
                                      )}
                                      onClick={() =>
                                        applySetOverride(
                                          member.agent_id,
                                          'compaction_policy',
                                        )}
                                    >
                                      {t(
                                        'projects.team.setOverride',
                                        'Set override',
                                      )}
                                    </Button>
                                    <Button
                                      variant="tertiary"
                                      onClick={() =>
                                        memberFieldIsOverridden(
                                          member,
                                          'compaction_policy',
                                        )
                                          ? applyClearOverride(
                                              member.agent_id,
                                              'compaction_policy',
                                            )
                                          : updateOverrideDraft(
                                              member.agent_id,
                                              'compaction_policy',
                                              null,
                                            )}
                                    >
                                      {memberFieldIsOverridden(
                                        member,
                                        'compaction_policy',
                                      )
                                        ? t(
                                            'projects.team.clearOverride',
                                            'Clear override',
                                          )
                                        : t('common.cancel', 'Cancel')}
                                    </Button>
                                  </div>
                                {:else}
                                  <Button
                                    variant="secondary"
                                    onClick={() =>
                                      updateOverrideDraft(
                                        member.agent_id,
                                        'compaction_policy',
                                        structuredClone(
                                          projectsState.globalCompactionPolicy ??
                                            {},
                                        ),
                                      )}
                                  >
                                    {t(
                                      'projects.team.customizeCompaction',
                                      'Customize for this agent',
                                    )}
                                  </Button>
                                {/if}
                              </div>

                              <p class="projects-override-help">
                                {t(
                                  'projects.team.overrideHelp',
                                  'An override replaces the agent file and all defaults for this agent in this project. The model override can also be set with /model in chat.',
                                )}
                              </p>
                            </div>

                            <div>
                              <p class="projects-tools-line">
                                {agentTargetPolicyText(member)}
                              </p>
                              <p class="projects-tools-follow">
                                {t(
                                  'projects.team.agentTargetsRepoOwned',
                                  'Defined by the repository Agent config and read-only in vBot. Even full access stays inside this Project Team.',
                                )}
                              </p>
                            </div>

                            <div>
                              {#if member.denied_tools.length > 0}
                                <p class="projects-tools-line">
                                  {t(
                                    'projects.team.deniedTools',
                                    'Denied by the agent file: {tools}',
                                    { tools: member.denied_tools.join(', ') },
                                  )}
                                </p>
                                <p class="projects-tools-follow">
                                  {t(
                                    'projects.team.toolsFollowWhitelist',
                                    'All other tools follow the project tool whitelist.',
                                  )}
                                </p>
                              {:else}
                                <p class="projects-tools-line">
                                  {t(
                                    'projects.team.deniedToolsNone',
                                    'No tool denials — follows the project tool whitelist.',
                                  )}
                                </p>
                              {/if}
                            </div>

                            {#if member.source_path}
                              <p class="projects-source-line">
                                {t(
                                  'projects.team.sourceFile',
                                  'Source: {path} ({format})',
                                  {
                                    path: member.source_path,
                                    format: member.source_format,
                                  },
                                )}
                              </p>
                            {/if}
                          </div>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            </div>

            <!-- Section 4: Tools -->
            <div class="detail-section">
              <div class="detail-section-title">
                {t('projects.detail.sectionTools', 'Tools')}
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  <span class="projects-label">
                    {t('projects.manage.allowedTools', 'Tool whitelist')}
                  </span>
                  <p class="projects-help">
                    {t(
                      'projects.manage.allowedToolsHelp',
                      'The maximum tools this project’s agents may use. An individual agent may use fewer through its own permissions.',
                    )}
                  </p>
                  <ToggleChipList
                    items={toolChipItems}
                    disabled={projectsState.editSaving}
                    emptyLabel={t(
                      'projects.manage.toolsEmpty',
                      'No tools available',
                    )}
                    ariaToggleLabel={(name) =>
                      t('projects.manage.toggleTool', 'Toggle tool {name}', {
                        name,
                      })}
                    onToggle={(name, next) => toggleTool(name, next)}
                    onSetAll={setAllTools}
                    onOpenExtensions={navigateToExtensions}
                  >
                    {#snippet headerActions()}
                      <Button
                        variant="tertiary"
                        data-testid="project-tools-reset"
                        disabled={projectsState.editSaving}
                        onClick={resetToolsToDefaults}
                      >
                        {t(
                          'projects.manage.resetDefaults',
                          'Reset to defaults',
                        )}
                      </Button>
                    {/snippet}
                  </ToggleChipList>
                </div>
              </div>
            </div>

            <!-- Section 5: Skills -->
            <div class="detail-section">
              <div class="detail-section-title">
                <span>{t('projects.detail.sectionSkills', 'Skills')}</span>
              </div>
              <div class="detail-section-body">
                <div class="projects-field">
                  <span class="projects-label">
                    {t('projects.manage.allowedSkills', 'Skill whitelist')}
                  </span>
                  <p class="projects-help">
                    {t(
                      'projects.manage.allowedSkillsHelp',
                      'Project skills are active by default; bundled and global skills are opt-in.',
                    )}
                  </p>
                  {#if skillToggleSections.project.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.projectSkills', 'Project skills')}
                    </span>
                    <ToggleChipList
                      items={projectSkillChips}
                      disabled={projectsState.editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleProjectSkill(name, next)}
                      onSetAll={setAllProjectSkills}
                    />
                  {/if}
                  {#if skillToggleSections.bundled.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.bundledSkills', 'Bundled skills')}
                    </span>
                    <ToggleChipList
                      items={bundledSkillChips}
                      disabled={projectsState.editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleBundledSkill(name, next)}
                      onSetAll={setAllBundledSkills}
                    />
                  {/if}
                  {#if skillToggleSections.global.length > 0}
                    <span class="projects-sublabel">
                      {t('projects.manage.globalSkills', 'Global skills')}
                    </span>
                    <ToggleChipList
                      items={globalSkillChips}
                      disabled={projectsState.editSaving}
                      ariaToggleLabel={(name) =>
                        t(
                          'projects.manage.toggleSkill',
                          'Toggle skill {name}',
                          {
                            name,
                          },
                        )}
                      onToggle={(name, next) => toggleGlobalSkill(name, next)}
                      onSetAll={setAllGlobalSkills}
                    />
                  {/if}
                  {#if skillToggleSections.project.length === 0 && skillToggleSections.bundled.length === 0 && skillToggleSections.global.length === 0}
                    <EmptyState
                      density="compact"
                      description={t(
                        'projects.manage.skillsEmpty',
                        'No skills available',
                      )}
                    />
                  {/if}
                </div>
              </div>
            </div>
          </div>
        </div>
      {/key}
    {/if}
  </div>

  {#if projectsState.isAddOpen}
    <Modal
      title={t('projects.add.title', 'Add project')}
      labelledById="projects-add-title"
      class="projects-view__modal"
      closeDisabled={projectsState.addingProject}
      onClose={closeAdd}
    >
      {#snippet body()}
        <form onsubmit={submitAdd}>
          <div class="modal-body">
            <p class="projects-help">
              {t(
                'projects.add.subtitle',
                'Enter the path to a repository on this machine. The folder must already exist; vBot reads it but never writes to it.',
              )}
            </p>

            <FormField
              controlId="projects-add-cwd"
              label={t('projects.add.cwd', 'Repository path')}
              help={t(
                'projects.add.cwdHelp',
                'The folder must exist. The project is created immediately and then scanned — you can remove it again afterwards.',
              )}
            >
              <TextField
                id="projects-add-cwd"
                variant="modal"
                value={projectsState.addForm.cwd}
                placeholder={t(
                  'projects.add.cwdPlaceholder',
                  'C:/path/to/repository',
                )}
                disabled={projectsState.addingProject}
                onInput={(next) => updateAddField('cwd', next)}
              />
            </FormField>

            <FormField
              controlId="projects-add-display-name"
              label={t('projects.add.displayName', 'Display name')}
            >
              <TextField
                id="projects-add-display-name"
                variant="modal"
                value={projectsState.addForm.display_name}
                placeholder={t(
                  'projects.add.displayNamePlaceholder',
                  'Optional — defaults to the folder name',
                )}
                disabled={projectsState.addingProject}
                onInput={(next) => updateAddField('display_name', next)}
              />
            </FormField>

            {#if addShowsFormatChoice}
              <FormField
                label={t('projects.add.format', 'Source format')}
                help={t(
                  'projects.add.formatHelp',
                  'This repository carries both ecosystems. Pick which one this project uses — its agents and skills come only from that one. You can switch later in the project settings.',
                )}
                role="radiogroup"
                aria-label={t('projects.add.format', 'Source format')}
              >
                <div class="projects-format-choice">
                  {#each PROJECT_SOURCE_FORMATS as formatKey (formatKey)}
                    <button
                      type="button"
                      class="projects-format-option"
                      class:projectsState.projects-format-option--selected={projectsState
                        .addForm.source_format === formatKey}
                      role="radio"
                      aria-checked={projectsState.addForm.source_format ===
                        formatKey}
                      disabled={projectsState.addingProject}
                      onclick={() => updateAddField('source_format', formatKey)}
                    >
                      <span class="projects-format-option__name">
                        {formatLabel(formatKey)}
                      </span>
                      <span class="projects-format-option__detail">
                        {t(
                          'projects.add.formatCounts',
                          '{agents} agents · {skills} skills',
                          {
                            agents:
                              projectsState.addDetect?.formats?.[formatKey]
                                ?.agents ?? 0,
                            skills:
                              projectsState.addDetect?.formats?.[formatKey]
                                ?.skills ?? 0,
                          },
                        )}
                      </span>
                    </button>
                  {/each}
                </div>
              </FormField>
            {:else if addDetectedFormat}
              <p class="projects-help">
                {t('projects.add.formatDetected', 'Detected: {format}', {
                  format: formatLabel(addDetectedFormat),
                })}
              </p>
            {/if}

            {#if addSuggestsClaudeMd}
              <div class="projects-claude-md-suggestion">
                <Toggle
                  size="sm"
                  checked={projectsState.addForm.include_claude_md}
                  disabled={projectsState.addingProject}
                  ariaLabel={t(
                    'projects.add.claudeMdSuggestionLabel',
                    'Load CLAUDE.md as a project file',
                  )}
                  onChange={(next) => updateAddField('include_claude_md', next)}
                />
                <span>
                  {t(
                    'projects.add.claudeMdSuggestion',
                    'Load {path} as a project file. The repository has no AGENTS.md; the file is loaded as-is into project agent prompts.',
                    { path: projectsState.addDetect?.claude_md ?? 'CLAUDE.md' },
                  )}
                </span>
              </div>
            {/if}

            {#if projectsState.addError}
              <Banner variant="error" role="alert">
                {projectsState.addError}
              </Banner>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={projectsState.addingProject}
              onClick={closeAdd}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={!canSubmitAdd}>
              {projectsState.addingProject
                ? t('projects.add.submitting', 'Adding project…')
                : t('projects.add.submit', 'Add project')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}

  {#if projectsState.rePointProject}
    <Modal
      title={t('projects.rePoint.title', 'Repository not found')}
      labelledById="projects-repoint-title"
      class="projects-view__modal"
      onClose={closeRePoint}
    >
      {#snippet body()}
        <form onsubmit={submitRePoint}>
          <div class="modal-body">
            <p class="projects-help">
              {t(
                'projects.rePoint.description',
                'The repository folder for this project no longer exists. Point it at the new location to restore the project.',
              )}
            </p>
            <FormField
              controlId="projects-repoint-cwd"
              label={t('projects.rePoint.cwd', 'New repository path')}
            >
              <TextField
                id="projects-repoint-cwd"
                variant="modal"
                value={projectsState.rePointCwd}
                placeholder={t(
                  'projects.rePoint.cwdPlaceholder',
                  'C:/path/to/repository',
                )}
                disabled={projectsState.rePointing}
                onInput={(next) => {
                  projectsState.rePointCwd = next;
                  projectsState.rePointError = '';
                }}
              />
            </FormField>

            {#if projectsState.rePointError}
              <Banner variant="error" role="alert">
                {projectsState.rePointError}
              </Banner>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={projectsState.rePointing}
              onClick={closeRePoint}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              variant="primary"
              type="submit"
              disabled={projectsState.rePointing}
            >
              {projectsState.rePointing
                ? t('projects.rePoint.submitting', 'Re-pointing…')
                : t('projects.rePoint.submit', 'Re-point')}
            </Button>
          </div>
        </form>
      {/snippet}
    </Modal>
  {/if}

  {#if projectsState.removeConfirmProject}
    <Modal
      title={t('projects.remove.confirmTitle', 'Remove project')}
      onClose={cancelRemove}
      closeDisabled={Boolean(projectsState.removingProjectId)}
    >
      {#snippet body()}
        <p>
          {t(
            'projects.remove.rootedAgentsBody',
            'Removing {name} clears it from every affected Rooted Agent and resets those Agents to their Default Workspace. Their Sessions and history stay unchanged. The repository and old Workspace files are never touched.',
            {
              name:
                projectsState.removeConfirmProject.display_name ||
                projectsState.removeConfirmProject.project_id,
            },
          )}
        </p>
        <div class="projects-toggle-row">
          <span>
            {t(
              'projects.remove.copyIdentityFiles',
              'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
            )}
          </span>
          <Toggle
            size="sm"
            checked={projectsState.copyRootedAgentIdentityFiles}
            disabled={Boolean(projectsState.removingProjectId)}
            ariaLabel={t(
              'projects.remove.copyIdentityFiles',
              'Copy SOUL.md, USER.md, and MEMORY.md to affected Default Workspaces',
            )}
            onChange={(next) =>
              (projectsState.copyRootedAgentIdentityFiles = next)}
          />
        </div>
        <p class="modal-hint">
          {t(
            'projects.remove.copyIdentityFilesHelp',
            'When enabled, existing destination versions are backed up before replacement. One choice applies to every affected Agent.',
          )}
        </p>
      {/snippet}
      {#snippet footer()}
        <Button
          variant="secondary"
          disabled={Boolean(projectsState.removingProjectId)}
          onClick={cancelRemove}
        >
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button
          variant="danger"
          disabled={Boolean(projectsState.removingProjectId)}
          onClick={confirmRemove}
        >
          {t('common.remove', 'Remove')}
        </Button>
      {/snippet}
    </Modal>
  {/if}
</section>
