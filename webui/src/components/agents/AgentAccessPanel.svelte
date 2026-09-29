<script>
  import { t } from '$lib/i18n.js';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import AgentSkillsPanel from '../skills/AgentSkillsPanel.svelte';
  import { skillAccessOf } from '../skills/skillAccess.js';
  import AgentSelectionGroup from './AgentSelectionGroup.svelte';
  import {
    withSubagentAllowedAgents,
    subagentAllowedAgents,
  } from '$lib/agentForm.js';
  import { toolAccessIncludes } from '$lib/toolAccess.js';
  let {
    availableTools,
    skillCatalog = { skills: [], agents: [], projects: [] },
    availableAgentTargets,
    agentTargetCatalogError,
    formValues = $bindable(),
    navigateToExtensions,
  } = $props();

  const WILDCARD_ACCESS = '*';

  let projectAgentsOpen = $state(false);

  // Filter text shared by the groups of the Skills and Sub-Agent sections.
  let skillQuery = $state('');

  let agentQuery = $state('');

  let visibleAgentTargetItems = $derived(agentTargetAccessItems());

  // The saved Skill projection of this Agent (null before it exists); the
  // checked state of allowlist-governed Skills comes from the draft.
  let skillAgent = $derived(
    skillCatalog.agents.find((agent) => agent.id === formValues.id) ?? null,
  );

  // Packages this Agent could see that fail to load.
  let invalidSkills = $derived(
    skillCatalog.skills.filter(
      (entry) =>
        entry.status === 'invalid' &&
        (entry.origin === 'global' ||
          entry.origin === 'bundled' ||
          entry.owner_id === formValues.id ||
          (entry.project_id &&
            entry.project_id === skillAgent?.root_project_id)),
    ),
  );

  let agentTargetItems = $derived(
    visibleAgentTargetItems.map((target) => ({
      name: target.name,
      kind: target.kind,
      allowed: target.isAllowed,
      detail: target.description,
      unavailable: Boolean(target.unavailable),
    })),
  );

  let identityAgentItems = $derived(
    agentTargetItems.filter((target) => target.kind !== 'project'),
  );

  let projectAgentItems = $derived(
    agentTargetItems.filter((target) => target.kind === 'project'),
  );

  let configuredAgentTargets = $derived(
    subagentAllowedAgents(formValues.tools),
  );

  let agentsAreWildcard = $derived(isWildcardAccess(configuredAgentTargets));

  let subagentToolEnabled = $derived(
    toolAccessIncludes(formValues.tool_access, 'subagent'),
  );

  function setAgentGroupAccess(items, isAllowed) {
    if (items.every((item) => item.allowed === isAllowed)) return;
    const groupNames = new Set(items.map((item) => item.name));
    const selectedNames = agentsAreWildcard
      ? agentTargetItems.map((item) => item.name)
      : configuredAgentTargets;
    const nextNames = selectedNames.filter((name) => !groupNames.has(name));
    if (isAllowed) nextNames.push(...groupNames);
    formValues.tools = withSubagentAllowedAgents(formValues.tools, nextNames);
  }

  function isWildcardAccess(items) {
    return Array.isArray(items) && items.includes(WILDCARD_ACCESS);
  }

  function agentTargetAccessItems() {
    const currentItems = configuredAgentTargets;
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const catalog = Array.isArray(availableAgentTargets)
      ? availableAgentTargets.filter(
          (target) =>
            target.kind !== 'identity' || target.name !== formValues.id,
        )
      : [];
    const knownNames = new Set(catalog.map((target) => target.name));
    const missingTargets = hasWildcard
      ? []
      : currentItems
          .filter((name) => !knownNames.has(name))
          .map((name) => ({
            name,
            kind: name.includes('@') ? 'project' : 'identity',
            unavailable: true,
          }));

    return [...catalog, ...missingTargets].map((target) => ({
      ...target,
      description: agentTargetDescription(target),
      isAllowed: hasWildcard || currentItems.includes(target.name),
    }));
  }

  // The row's secondary line; the group already names the target kind.
  function agentTargetDescription(target) {
    if (target.unavailable) {
      return t('agents.access.unavailableAgentTarget');
    }
    if (target.kind === 'project') {
      return t('agents.access.projectAgentDetail', {
        agent: target.displayName || target.name,
        project: target.projectName || target.projectId,
      });
    }
    return target.displayName || '';
  }

  function agentToggleLabel(name) {
    return t('agents.access.toggleAgent', { name });
  }

  function updateAgentTargetAccessItem(itemName, isAllowed) {
    const allTargetNames = agentTargetAccessItems().map(
      (target) => target.name,
    );
    if (allTargetNames.length === 0) {
      formValues.tools = withSubagentAllowedAgents(formValues.tools, []);
      return;
    }

    const currentItems = [...configuredAgentTargets];
    if (currentItems.includes(WILDCARD_ACCESS)) {
      formValues.tools = withSubagentAllowedAgents(
        formValues.tools,
        isAllowed
          ? [WILDCARD_ACCESS]
          : allTargetNames.filter((name) => name !== itemName),
      );
      return;
    }

    const nextItems = currentItems.filter((item) =>
      allTargetNames.includes(item),
    );
    const existingIndex = nextItems.indexOf(itemName);

    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    } else if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }
    formValues.tools = withSubagentAllowedAgents(formValues.tools, nextItems);
  }
</script>

<div class="agents-view__part" id="agent-detail-panel-access">
  <section class="s-section" aria-labelledby="agent-section-tools">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-tools">
        {t('agents.form.toolAccess')}
      </h3>
    </header>
    <p class="s-section__desc">
      {t('agents.form.toolAccessHelp')}
    </p>
    <div class="s-section__body">
      <ToolAccessEditor
        value={formValues.tool_access}
        tools={availableTools}
        memoryPromptMode={formValues.memory_prompt_mode}
        onChange={(next) => (formValues.tool_access = next)}
        onOpenExtensions={navigateToExtensions}
      />
    </div>
  </section>

  <section class="s-section" aria-labelledby="agent-section-skills">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-skills">
        {t('agents.form.skills')}
      </h3>
    </header>
    <p class="s-section__desc">
      {t('agents.form.skillsDescription')}
    </p>
    <div class="s-section__body">
      <AgentSkillsPanel
        agent={skillAgent}
        agentId={formValues.id}
        access={skillAccessOf(formValues)}
        inventory={skillCatalog.skills}
        agents={skillCatalog.agents}
        projects={skillCatalog.projects}
        query={skillQuery}
        showFilter
        onQuery={(next) => (skillQuery = next)}
        onChange={(next) => {
          formValues.allowed_skills = next.allowed;
          formValues.excluded_skills = next.excluded;
        }}
        columns
      />
      {#if invalidSkills.length > 0}
        <div class="s-subhead">
          <h4 class="s-subhead__title">
            {t('agents.access.invalidSkillsTitle')}
          </h4>
        </div>
        <div class="s-group agents-view__invalid-skills">
          {#each invalidSkills as item (item.id)}
            <div class="s-row s-row--compact">
              <div class="s-row-info">
                <div class="agents-view__invalid-skill-name">
                  {item.name || t('agents.access.unknownSkillName')}
                </div>
                {#if Array.isArray(item.warnings) && item.warnings.length > 0}
                  <ul class="agents-view__skill-warnings">
                    {#each item.warnings as warning, index (`${item.id}-warning-${index}`)}
                      <li>{warning}</li>
                    {/each}
                  </ul>
                {/if}
              </div>
              <div class="s-row-control">
                <StatusChip variant="warn">
                  {t('agents.access.notLoadable')}
                </StatusChip>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </section>

  {#if subagentToolEnabled}
    <section class="s-section" aria-labelledby="agent-section-subagents">
      <header class="s-section__head">
        <h3 class="s-section__title" id="agent-section-subagents">
          {t('agents.form.subagentTargets')}
        </h3>
      </header>
      <p class="s-section__desc">
        {agentsAreWildcard && visibleAgentTargetItems.length > 0
          ? t('agents.form.agentWildcardNote')
          : t('agents.form.agentAddressNote')}
      </p>
      <div class="s-section__body">
        {#if agentTargetItems.length > 1}
          {@render filterToolbar(
            agentQuery,
            (next) => (agentQuery = next),
            t('agents.access.filterAgents'),
            t('agents.access.filterAgentsPlaceholder'),
          )}
        {/if}
        <AgentSelectionGroup
          title={t('agents.access.identityAgents')}
          titleId="agent-identity-targets-label"
          items={identityAgentItems}
          query={agentQuery}
          allLabel={t('agents.access.allIdentityAgents')}
          toggleLabel={agentToggleLabel}
          emptyLabel={t('agents.access.noIdentityAgentTargets')}
          onToggle={updateAgentTargetAccessItem}
          onSetAll={(next) => setAgentGroupAccess(identityAgentItems, next)}
        />
        {#if projectAgentItems.length > 0}
          <AgentSelectionGroup
            class="agents-view__project-targets"
            title={t('agents.access.projectAgents')}
            titleId="agent-project-targets-label"
            items={projectAgentItems}
            query={agentQuery}
            allLabel={t('agents.access.allProjectAgents')}
            toggleLabel={agentToggleLabel}
            collapsible
            open={projectAgentsOpen}
            toggleId="agent-project-targets-toggle"
            contentId="agent-project-targets"
            onOpenChange={(next) => (projectAgentsOpen = next)}
            onToggle={updateAgentTargetAccessItem}
            onSetAll={(next) => setAgentGroupAccess(projectAgentItems, next)}
          />
        {/if}
        {#if agentTargetCatalogError}
          <p class="agents-view__catalog-error" role="status">
            {agentTargetCatalogError}
          </p>
        {/if}
      </div>
    </section>
  {/if}
</div>

{#snippet filterToolbar(value, onInput, label, placeholder)}
  <div class="s-group-toolbar agents-view__filter-toolbar">
    <TextField
      type="search"
      class="agents-view__filter"
      {value}
      {placeholder}
      ariaLabel={label}
      {onInput}
    />
  </div>
{/snippet}
