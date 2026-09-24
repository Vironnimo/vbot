<script>
  import { t } from '$lib/i18n.js';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import AgentSelectionGroup from './AgentSelectionGroup.svelte';
  import {
    withSubagentAllowedAgents,
    subagentAllowedAgents,
  } from '$lib/agentForm.js';
  import { toolAccessIncludes } from '$lib/toolAccess.js';
  let {
    availableTools,
    availableSkills,
    invalidSkills,
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

  let visibleSkillItems = $derived(skillAccessItems());

  let visibleAgentTargetItems = $derived(agentTargetAccessItems());

  let skillItems = $derived(
    visibleSkillItems.map((skill) => ({
      name: skill.name,
      allowed: skill.isAllowed,
      detail: skill.description,
      warnings: skill.warnings,
    })),
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

  let skillsAreWildcard = $derived(isWildcardAccess(formValues.allowed_skills));

  let configuredAgentTargets = $derived(
    subagentAllowedAgents(formValues.tools),
  );

  let agentsAreWildcard = $derived(isWildcardAccess(configuredAgentTargets));

  let subagentToolEnabled = $derived(
    toolAccessIncludes(formValues.tool_access, 'subagent'),
  );

  function updateAccessItem(fieldName, itemName, isAllowed) {
    if (fieldName === 'allowed_skills') {
      updateSkillAccessItem(itemName, isAllowed);
      return;
    }

    if (fieldName === 'allowed_agents') {
      updateAgentTargetAccessItem(itemName, isAllowed);
    }
  }

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

  function skillAccessItems() {
    const currentItems = Array.isArray(formValues.allowed_skills)
      ? formValues.allowed_skills
      : [];
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const allowedItems = hasWildcard ? [] : currentItems;

    return availableSkills.map((skill) => ({
      ...skill,
      warnings: Array.isArray(skill.warnings) ? skill.warnings : [],
      isAllowed: hasWildcard || allowedItems.includes(skill.name),
    }));
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
      return t(
        'agents.access.unavailableAgentTarget',
        'This configured target is not present in the current Identity Agent or Project Team catalogs.',
      );
    }
    if (target.kind === 'project') {
      return t('agents.access.projectAgentDetail', '{agent} · {project}', {
        agent: target.displayName || target.name,
        project: target.projectName || target.projectId,
      });
    }
    return target.displayName || '';
  }

  function agentToggleLabel(name) {
    return t('agents.access.toggleAgent', 'Toggle agent {name}', { name });
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

  function updateSkillAccessItem(itemName, isAllowed) {
    const allSkillNames = availableSkills.map((skill) => skill.name);

    if (allSkillNames.length === 0) {
      formValues.allowed_skills = [];
      return;
    }

    const currentItems = Array.isArray(formValues.allowed_skills)
      ? [...formValues.allowed_skills]
      : [];

    if (currentItems.includes(WILDCARD_ACCESS)) {
      if (isAllowed) {
        formValues.allowed_skills = [WILDCARD_ACCESS];
        return;
      }

      formValues.allowed_skills = allSkillNames.filter(
        (name) => name !== itemName,
      );
      return;
    }

    const nextItems = currentItems.filter((item) =>
      allSkillNames.includes(item),
    );
    const existingIndex = nextItems.indexOf(itemName);

    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    }

    if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }

    formValues.allowed_skills = allSkillNames.every((name) =>
      nextItems.includes(name),
    )
      ? [WILDCARD_ACCESS]
      : nextItems;
  }
</script>

<div class="agents-view__part" id="agent-detail-panel-access">
  <section class="s-section" aria-labelledby="agent-section-tools">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-tools">
        {t('agents.form.toolAccess', 'Tool access')}
      </h3>
    </header>
    <p class="s-section__desc">
      {t(
        'agents.form.toolAccessHelp',
        'Choose which Tools this Agent may use. Automatic Tools become available when their condition is met; permission does not guarantee current availability.',
      )}
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
        {t('agents.form.skills', 'Skills')}
      </h3>
    </header>
    <p class="s-section__desc">
      {skillsAreWildcard && skillItems.length > 0
        ? t(
            'agents.form.wildcardNote',
            'Currently all are allowed, including ones added in the future. Turning any single item off switches to a fixed list.',
          )
        : t(
            'agents.form.skillsDescription',
            'Skills this Agent may load. Selecting all also allows Skills added later.',
          )}
    </p>
    <div class="s-section__body">
      {#if skillItems.length > 1}
        {@render filterToolbar(
          skillQuery,
          (next) => (skillQuery = next),
          t('agents.access.filterSkills', 'Filter Skills'),
          t('agents.access.filterSkillsPlaceholder', 'Filter Skills…'),
        )}
      {/if}
      <AgentSelectionGroup
        title={t('agents.form.allowedSkills', 'Allowed skills')}
        titleId="agent-skills-label"
        items={skillItems}
        query={skillQuery}
        allLabel={t('agents.access.allSkills', 'All Skills')}
        toggleLabel={(name) =>
          t('agents.access.toggleSkill', 'Toggle skill {name}', { name })}
        emptyLabel={t(
          'agents.access.noSkills',
          'No loadable skills are available.',
        )}
        onToggle={(name, next) =>
          updateAccessItem('allowed_skills', name, next)}
        onSetAll={(next) =>
          (formValues.allowed_skills = next ? [WILDCARD_ACCESS] : [])}
      />
      {#if invalidSkills.length > 0}
        <div class="s-subhead">
          <h4 class="s-subhead__title">
            {t('agents.access.invalidSkillsTitle', 'Unavailable skills')}
          </h4>
        </div>
        <div class="s-group agents-view__invalid-skills">
          {#each invalidSkills as item (item.path || item.name)}
            <div class="s-row s-row--compact">
              <div class="s-row-info">
                <div class="agents-view__invalid-skill-name">
                  {item.name ||
                    t('agents.access.unknownSkillName', 'Unknown skill')}
                </div>
                {#if item.path}
                  <div class="agents-view__invalid-skill-path">
                    {item.path}
                  </div>
                {/if}
                {#if Array.isArray(item.warnings) && item.warnings.length > 0}
                  <ul class="agents-view__skill-warnings">
                    {#each item.warnings as warning, index (`${item.path || item.name}-warning-${index}`)}
                      <li>{warning}</li>
                    {/each}
                  </ul>
                {/if}
              </div>
              <div class="s-row-control">
                <StatusChip variant="warn">
                  {t('agents.access.notLoadable', 'not loadable')}
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
          {t('agents.form.subagentTargets', 'Sub-Agent targets')}
        </h3>
      </header>
      <p class="s-section__desc">
        {agentsAreWildcard && visibleAgentTargetItems.length > 0
          ? t(
              'agents.form.agentWildcardNote',
              'Additional Agents: all other Identity Agents and all Agents on every registered Project, including ones added later. The calling Agent remains implicit. Rooting does not narrow this.',
            )
          : t(
              'agents.form.agentAddressNote',
              'Additional Agents use bare Identity ids or agent@project ids. The calling Agent remains implicit. Rooting does not change this list.',
            )}
      </p>
      <div class="s-section__body">
        {#if agentTargetItems.length > 1}
          {@render filterToolbar(
            agentQuery,
            (next) => (agentQuery = next),
            t('agents.access.filterAgents', 'Filter Agents'),
            t('agents.access.filterAgentsPlaceholder', 'Filter Agents…'),
          )}
        {/if}
        <AgentSelectionGroup
          title={t('agents.access.identityAgents', 'Identity Agents')}
          titleId="agent-identity-targets-label"
          items={identityAgentItems}
          query={agentQuery}
          allLabel={t('agents.access.allIdentityAgents', 'All Identity Agents')}
          toggleLabel={agentToggleLabel}
          emptyLabel={t(
            'agents.access.noIdentityAgentTargets',
            'No additional Identity Agents are available.',
          )}
          onToggle={(name, next) =>
            updateAccessItem('allowed_agents', name, next)}
          onSetAll={(next) => setAgentGroupAccess(identityAgentItems, next)}
        />
        {#if projectAgentItems.length > 0}
          <AgentSelectionGroup
            class="agents-view__project-targets"
            title={t('agents.access.projectAgents', 'Project Agents')}
            titleId="agent-project-targets-label"
            items={projectAgentItems}
            query={agentQuery}
            allLabel={t('agents.access.allProjectAgents', 'All Project Agents')}
            toggleLabel={agentToggleLabel}
            collapsible
            open={projectAgentsOpen}
            toggleId="agent-project-targets-toggle"
            contentId="agent-project-targets"
            onOpenChange={(next) => (projectAgentsOpen = next)}
            onToggle={(name, next) =>
              updateAccessItem('allowed_agents', name, next)}
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
