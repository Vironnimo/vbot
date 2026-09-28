<script>
  import { t, tOr } from '$lib/i18n.js';
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
    projectsState = $bindable(),
    projectsController,
    scanAction,
    findingsExpanded = $bindable(false),
    trackModelDropdownOpen,
    updateToolAccessOverride,
    navigateToExtensions,
  } = $props();

  function agentTargetPolicyText(member) {
    const summary = projectAgentTargetSummary(member, projectsState.activeTeam);
    if (summary.mode === 'unavailable') {
      return t('projects.team.agentTargetsUnavailable');
    }
    if (summary.mode === 'self') {
      return t('projects.team.agentTargetsSelf');
    }
    if (summary.mode === 'all') {
      return t('projects.team.agentTargetsAll');
    }
    return t('projects.team.agentTargetsLimited', {
      agents: summary.agents.join(', '),
    });
  }

  const EFFECTIVE_FIELD_META = Object.freeze({
    model: {
      label: () => t('projects.team.effectiveModel'),
      empty: () => t('projects.team.valueNotConfigured'),
    },
    temperature: {
      label: () => t('projects.team.effectiveTemperature'),
      empty: () => t('projects.team.valueProviderDefault'),
    },
    thinking_effort: {
      label: () => t('projects.team.effectiveThinkingEffort'),
      empty: () => t('projects.team.valueProviderDefault'),
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
      label: meta.label(),
      value: isEmpty ? meta.empty() : String(entry.value),
      isEmpty,
      sourceLabel: sourceLabel(entry.source),
    };
  }

  function sourceLabel(source) {
    switch (source) {
      case 'override':
        return t('projects.team.sourceOverride');
      case 'agent':
        return t('projects.team.sourceAgentFile');
      case 'project_default':
        return t('projects.team.sourceProjectDefault');
      case 'global_default':
        return t('projects.team.sourceGlobalDefault');
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
          ? t('projects.manage.providerThinkingEffortDefault')
          : t(`agents.form.thinkingEffortOption.${option}`),
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
      emptyLabel: t('projects.team.overrideModelPlaceholder'),
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
    return tOr(`projects.report.group.${type}`, type);
  }
</script>

<div class="management-topic" id="project-detail-panel-team">
  <section class="s-section" aria-labelledby="project-section-team">
    <header class="s-section__head">
      <h3 class="s-section__title" id="project-section-team">
        {t('projects.detail.sectionTeam')}
      </h3>
      <InfoHint text={t('projects.detail.teamInfo')} />
      <div class="s-section__aside">{@render scanAction?.()}</div>
    </header>
    <div class="s-section__body">
      {#if projectsState.activeReport && !projectsState.activeReport.clean}
        <Banner variant="warn" role="status">
          <span>
            {t('projects.report.findingCount', {
              count: projectsState.activeReport.findingCount,
            })}
          </span>
          <Button
            variant="tertiary"
            aria-expanded={findingsExpanded}
            onClick={toggleFindings}
          >
            {findingsExpanded
              ? t('projects.report.hideDetails')
              : t('projects.report.showDetails')}
          </Button>
        </Banner>
        {#if findingsExpanded}
          {#each projectsState.activeReport.groups as group (group.type)}
            <div class="s-subhead projects-finding-head">
              <h4 class="s-subhead__title">{groupLabel(group.type)}</h4>
            </div>
            <ul class="s-group projects-findings">
              {#each group.findings as finding, index (`${group.type}-${index}`)}
                <li class="projects-finding">
                  <span class="projects-finding-detail">
                    {finding.detail}
                  </span>
                  {#if finding.agent_id}
                    <span class="projects-finding-meta">
                      {t('projects.report.finding.agent', {
                        agentId: finding.agent_id,
                      })}
                    </span>
                  {/if}
                  {#if finding.source_path}
                    <span class="projects-finding-meta">
                      {t('projects.report.finding.source', {
                        source: finding.source_path,
                      })}
                    </span>
                  {/if}
                </li>
              {/each}
            </ul>
          {/each}
        {/if}
      {/if}

      {#if projectsState.scanLoading}
        <p class="projects-scan-loading" role="status">
          {t('projects.loading')}
        </p>
      {:else if projectsState.activeTeam.length === 0}
        <EmptyState density="compact" description={t('projects.team.empty')} />
      {:else}
        <ul class="s-group projects-team">
          {#each projectsState.activeTeam as member (member.agent_id)}
            {@const expanded =
              projectsState.expandedMembers[member.agent_id] === true}
            {@const summary = effectiveDisplay(member, 'model')}
            <li
              class="projects-team-member"
              class:projects-team-member--expanded={expanded}
              data-testid={`project-team-member-${member.agent_id}`}
            >
              <button
                type="button"
                class="projects-team-header"
                data-testid={`project-team-toggle-${member.agent_id}`}
                aria-expanded={expanded}
                use:tooltip={{
                  text: member.description,
                  rows: [
                    {
                      label: t('projects.team.effectiveModel'),
                      value: summary.value,
                      mono: true,
                    },
                  ],
                }}
                onclick={() => toggleMember(member.agent_id)}
              >
                <span
                  class="disclosure-chevron projects-team-chevron"
                  class:disclosure-chevron--open={expanded}
                  class:projects-team-chevron--open={expanded}
                  aria-hidden="true"
                ></span>
                <span class="projects-team-name">
                  {member.display_name}
                </span>
                <span class="projects-team-summary">
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
                          class:projects-effective-value--muted={display.isEmpty}
                        >
                          {display.value}
                        </span>
                        {#if display.sourceLabel}
                          <span class="projects-effective-source">
                            {t('projects.team.fromSource', {
                              source: display.sourceLabel,
                            })}
                          </span>
                        {/if}
                      </li>
                    {/each}
                  </ul>

                  <div class="projects-member-block">
                    <div class="s-subhead">
                      <h4 class="s-subhead__title">
                        {t('projects.team.overridesTitle')}
                      </h4>
                      <p class="s-subhead__desc">
                        {t('projects.team.overrideHelp')}
                      </p>
                    </div>

                    <div class="projects-member-fields">
                      <div class="projects-member-field">
                        <div class="projects-member-field__info">
                          <label
                            class="s-row-label"
                            for={`project-override-model-${member.agent_id}`}
                          >
                            {t('projects.team.effectiveModel')}
                          </label>
                          {#if memberFieldIsOverridden(member, 'model')}
                            <Button
                              variant="tertiary"
                              class="projects-clear-override"
                              data-testid={`project-override-clear-model-${member.agent_id}`}
                              onClick={() =>
                                applyClearOverride(member.agent_id, 'model')}
                            >
                              {t('projects.team.clearOverride')}
                            </Button>
                          {/if}
                        </div>
                        <div class="projects-member-field__control">
                          <SearchableDropdown
                            id={`project-override-model-${member.agent_id}`}
                            value={selectModelValue(
                              overrideDraft(member.agent_id).model,
                              overrideModelOptions(member),
                            )}
                            options={overrideModelOptions(member)}
                            placeholder={t(
                              'projects.team.overrideModelPlaceholder',
                            )}
                            searchPlaceholder={t(
                              'projects.manage.modelSearchPlaceholder',
                            )}
                            emptyLabel={t('projects.manage.modelSearchEmpty')}
                            ariaLabel={t('projects.team.effectiveModel')}
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
                      </div>

                      <div class="projects-member-field">
                        <div class="projects-member-field__info">
                          <label
                            class="s-row-label"
                            for={`project-override-temperature-${member.agent_id}`}
                          >
                            {t('projects.team.effectiveTemperature')}
                          </label>
                          {#if memberFieldIsOverridden(member, 'temperature')}
                            <Button
                              variant="tertiary"
                              class="projects-clear-override"
                              data-testid={`project-override-clear-temperature-${member.agent_id}`}
                              onClick={() =>
                                applyClearOverride(
                                  member.agent_id,
                                  'temperature',
                                )}
                            >
                              {t('projects.team.clearOverride')}
                            </Button>
                          {/if}
                        </div>
                        <div
                          class="projects-member-field__control projects-number-control"
                        >
                          <TextField
                            id={`project-override-temperature-${member.agent_id}`}
                            class="projects-number-input"
                            inputmode="decimal"
                            value={overrideDraft(member.agent_id).temperature}
                            placeholder={t(
                              'projects.team.overrideTemperaturePlaceholder',
                            )}
                            ariaLabel={t('projects.team.effectiveTemperature')}
                            onInput={(next) =>
                              updateOverrideDraft(
                                member.agent_id,
                                'temperature',
                                next,
                              )}
                          />
                        </div>
                      </div>

                      <div class="projects-member-field">
                        <div class="projects-member-field__info">
                          <label
                            class="s-row-label"
                            for={`project-override-thinking-${member.agent_id}`}
                          >
                            {t('projects.team.effectiveThinkingEffort')}
                          </label>
                          {#if memberFieldIsOverridden(member, 'thinking_effort')}
                            <Button
                              variant="tertiary"
                              class="projects-clear-override"
                              data-testid={`project-override-clear-thinking-${member.agent_id}`}
                              onClick={() =>
                                applyClearOverride(
                                  member.agent_id,
                                  'thinking_effort',
                                )}
                            >
                              {t('projects.team.clearOverride')}
                            </Button>
                          {/if}
                        </div>
                        <div class="projects-member-field__control">
                          <Dropdown
                            id={`project-override-thinking-${member.agent_id}`}
                            value={overrideDraft(member.agent_id)
                              .thinking_effort}
                            options={overrideEffortOptions(member)}
                            ariaLabel={t(
                              'projects.team.effectiveThinkingEffort',
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
                      </div>

                      <div
                        class="projects-member-field projects-override-row--policy"
                      >
                        <div class="projects-member-field__info">
                          <span class="s-row-label">
                            {t('projects.team.compactionPolicy')}
                          </span>
                          {#if overrideDraft(member.agent_id).compaction_policy}
                            <Button
                              variant="tertiary"
                              class="projects-clear-override"
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
                                ? t('projects.team.clearOverride')
                                : t('common.cancel')}
                            </Button>
                          {/if}
                        </div>
                        {#if overrideDraft(member.agent_id).compaction_policy}
                          <div class="projects-member-field__wide">
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
                          </div>
                        {:else}
                          <div class="projects-member-field__control">
                            <Button
                              variant="secondary"
                              onClick={() =>
                                updateOverrideDraft(
                                  member.agent_id,
                                  'compaction_policy',
                                  $state.snapshot(
                                    projectsState.globalCompactionPolicy ?? {},
                                  ),
                                )}
                            >
                              {t('projects.team.customizeCompaction')}
                            </Button>
                          </div>
                        {/if}
                      </div>
                    </div>
                  </div>

                  <div class="projects-member-block">
                    <div class="s-subhead projects-member-subhead">
                      <div class="projects-member-subhead__copy">
                        <h4 class="s-subhead__title">
                          {t('projects.team.toolAccessOverride')}
                        </h4>
                        <p class="s-subhead__desc">
                          {t('projects.team.toolAccessOverrideHelp')}
                        </p>
                      </div>
                      <StatusChip
                        variant={memberFieldIsOverridden(member, 'tool_access')
                          ? 'info'
                          : 'neutral'}
                      >
                        {memberFieldIsOverridden(member, 'tool_access')
                          ? t('projects.team.toolOverrideActive')
                          : t('projects.team.repositoryPolicyActive')}
                      </StatusChip>
                    </div>
                    <ToolAccessEditor
                      value={overrideDraft(member.agent_id).tool_access}
                      tools={projectsState.toolCatalog}
                      ceiling={projectsState.editForm.allowed_tools}
                      disabled={isOverrideBusy(member.agent_id, 'tool_access')}
                      showReset={memberFieldIsOverridden(member, 'tool_access')}
                      onChange={(value) =>
                        updateToolAccessOverride(member.agent_id, value)}
                      onReset={() =>
                        applyClearOverride(member.agent_id, 'tool_access')}
                      onOpenExtensions={navigateToExtensions}
                    />
                  </div>

                  <div class="projects-member-block projects-member-facts">
                    <p class="projects-tools-line">
                      {agentTargetPolicyText(member)}
                    </p>
                    <p class="projects-tools-follow">
                      {t('projects.team.agentTargetsRepoOwned')}
                    </p>
                    {#if member.denied_tools.length > 0}
                      <p class="projects-tools-line">
                        {t('projects.team.deniedToolsBaseline', {
                          tools: member.denied_tools.join(', '),
                        })}
                      </p>
                      <p class="projects-tools-follow">
                        {t('projects.team.deniedToolsBaselineHelp')}
                      </p>
                    {/if}
                    {#if member.source_path}
                      <p class="projects-source-line">
                        {t('projects.team.sourceFile', {
                          path: member.source_path,
                          format: member.source_format,
                        })}
                      </p>
                    {/if}
                  </div>
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </section>
</div>
