<script>
  import { t } from '$lib/i18n.js';
  import InfoHint from '../ui/InfoHint.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import {
    selectModelValue,
    filterModelSelectOptions,
    buildModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
  } from '$lib/modelSelection.js';
  import {
    memberFieldIsOverridden,
    projectAgentTargetSummary,
  } from '$lib/projectsView.js';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import {
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  let {
    projectsState,
    projectsController,
    activeDetail,
    findingsExpanded = $bindable(false),
    trackModelDropdownOpen,
    updateToolAccessOverride,
    navigateToExtensions,
  } = $props();

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

  function toggleMember(agentId) {
    projectsState.expandedMembers = {
      ...projectsState.expandedMembers,
      [agentId]: !projectsState.expandedMembers[agentId],
    };
  }

  function toggleFindings() {
    findingsExpanded = !findingsExpanded;
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

  function applyClearOverride(agentId, field) {
    void projectsController.clearMemberOverride(agentId, field);
  }

  function groupLabel(type) {
    return t(`projects.report.group.${type}`, type);
  }
</script>

<div
  class="management-topic"
  role="tabpanel"
  id="project-detail-panel-team"
  aria-labelledby="project-detail-tab-team"
  hidden={activeDetail !== 'team'}
  tabindex="0"
>
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
            <span>
              {t('projects.report.findingCount', '{count} issues found', {
                count: projectsState.activeReport.findingCount,
              })}
            </span>
            <Button
              variant="tertiary"
              aria-expanded={findingsExpanded}
              onClick={toggleFindings}
            >
              {findingsExpanded
                ? t('projects.report.hideDetails', 'Hide details')
                : t('projects.report.showDetails', 'Show details')}
            </Button>
          </Banner>
          {#if findingsExpanded}
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
          {/if}
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
                <span class="projects-team-summary" use:tooltip={summary.value}>
                  {summary.value}
                </span>
              </button>

              {#if expanded}
                <div class="projects-team-detail">
                  <ul class="projects-effective-list">
                    {#each ['model', 'temperature', 'thinking_effort'] as field (field)}
                      {@const display = effectiveDisplay(member, field)}
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
                            {t('projects.team.fromSource', 'from {source}', {
                              source: display.sourceLabel,
                            })}
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
                            triggerClass="projects-dropdown"
                            panelClass="projects-view__search-panel"
                            footerActionLabel={overrideModelFilterFooter(
                              member,
                            )}
                            onFooterAction={() =>
                              toggleShowAllOverrideModels(member.agent_id)}
                            onOpenChange={trackModelDropdownOpen}
                            onValueChange={(value) =>
                              updateOverrideModelSelection(
                                member.agent_id,
                                value,
                              )}
                          />
                        </div>

                        {#if memberFieldIsOverridden(member, 'model')}
                          <Button
                            variant="tertiary"
                            data-testid={`project-override-clear-model-${member.agent_id}`}
                            onClick={() =>
                              applyClearOverride(member.agent_id, 'model')}
                          >
                            {t('projects.team.clearOverride', 'Clear override')}
                          </Button>
                        {/if}
                      </div>
                    </div>

                    <!-- Temperature override -->
                    <div class="projects-override-row">
                      <span class="projects-label">
                        {t('projects.team.overrideLabel', 'Override')} ·
                        {t('projects.team.effectiveTemperature', 'Temperature')}
                      </span>
                      <div class="projects-override-controls">
                        <TextField
                          id={`project-override-temperature-${member.agent_id}`}
                          class="projects-override-input"
                          inputmode="decimal"
                          value={overrideDraft(member.agent_id).temperature}
                          placeholder={t(
                            'projects.team.overrideTemperaturePlaceholder',
                            'e.g. 0.7',
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

                        {#if memberFieldIsOverridden(member, 'temperature')}
                          <Button
                            variant="tertiary"
                            data-testid={`project-override-clear-temperature-${member.agent_id}`}
                            onClick={() =>
                              applyClearOverride(
                                member.agent_id,
                                'temperature',
                              )}
                          >
                            {t('projects.team.clearOverride', 'Clear override')}
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
                            triggerClass="projects-dropdown"
                            onValueChange={(value) =>
                              updateOverrideDraft(
                                member.agent_id,
                                'thinking_effort',
                                value,
                              )}
                          />
                        </div>

                        {#if memberFieldIsOverridden(member, 'thinking_effort')}
                          <Button
                            variant="tertiary"
                            data-testid={`project-override-clear-thinking-${member.agent_id}`}
                            onClick={() =>
                              applyClearOverride(
                                member.agent_id,
                                'thinking_effort',
                              )}
                          >
                            {t('projects.team.clearOverride', 'Clear override')}
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
                                projectsState.globalCompactionPolicy ?? {},
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

                    <div class="projects-tool-access-override">
                      <div class="projects-tool-access-heading">
                        <div>
                          <span class="projects-label">
                            {t(
                              'projects.team.toolAccessOverride',
                              'Tool access override',
                            )}
                          </span>
                          <p class="projects-tools-follow">
                            {t(
                              'projects.team.toolAccessOverrideHelp',
                              'This replaces the repository Agent policy completely. It may allow a Tool blocked by the Agent file, but it can never exceed the Project Tool Whitelist.',
                            )}
                          </p>
                        </div>
                        <StatusChip
                          variant={memberFieldIsOverridden(
                            member,
                            'tool_access',
                          )
                            ? 'info'
                            : 'neutral'}
                        >
                          {memberFieldIsOverridden(member, 'tool_access')
                            ? t(
                                'projects.team.toolOverrideActive',
                                'Override active',
                              )
                            : t(
                                'projects.team.repositoryPolicyActive',
                                'Repository policy',
                              )}
                        </StatusChip>
                      </div>
                      <ToolAccessEditor
                        value={overrideDraft(member.agent_id).tool_access}
                        tools={projectsState.toolCatalog}
                        ceiling={projectsState.editForm.allowed_tools}
                        disabled={isOverrideBusy(
                          member.agent_id,
                          'tool_access',
                        )}
                        showReset={memberFieldIsOverridden(
                          member,
                          'tool_access',
                        )}
                        onChange={(value) =>
                          updateToolAccessOverride(member.agent_id, value)}
                        onReset={() =>
                          applyClearOverride(member.agent_id, 'tool_access')}
                        onOpenExtensions={navigateToExtensions}
                      />
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

                  {#if member.denied_tools.length > 0}
                    <div>
                      <p class="projects-tools-line">
                        {t(
                          'projects.team.deniedToolsBaseline',
                          'Repository baseline blocks: {tools}',
                          { tools: member.denied_tools.join(', ') },
                        )}
                      </p>
                      <p class="projects-tools-follow">
                        {t(
                          'projects.team.deniedToolsBaselineHelp',
                          'These blocks apply only while the repository policy is active. A vBot Tool override replaces them.',
                        )}
                      </p>
                    </div>
                  {/if}

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
</div>
