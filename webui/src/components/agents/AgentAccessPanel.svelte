<script>
  import { t } from '$lib/i18n.js';
  import InfoHint from '../ui/InfoHint.svelte';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import ToggleChipList from '../ui/ToggleChipList.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import Button from '../ui/Button.svelte';
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
    activeDetail,
    navigateToExtensions,
  } = $props();

  const WILDCARD_ACCESS = '*';

  let projectAgentsOpen = $state(false);

  let visibleSkillItems = $derived(skillAccessItems());

  let visibleAgentTargetItems = $derived(agentTargetAccessItems());

  let skillChipItems = $derived(
    visibleSkillItems.map((skill) => ({ ...skill, allowed: skill.isAllowed })),
  );

  let agentTargetChipItems = $derived(
    visibleAgentTargetItems.map((target) => ({
      ...target,
      allowed: target.isAllowed,
    })),
  );

  let identityAgentChipItems = $derived(
    agentTargetChipItems.filter((target) => target.kind !== 'project'),
  );

  let projectAgentChipItems = $derived(
    agentTargetChipItems.filter((target) => target.kind === 'project'),
  );

  let selectedProjectAgentCount = $derived(
    projectAgentChipItems.filter((target) => target.allowed).length,
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
      ? agentTargetChipItems.map((item) => item.name)
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

  function agentTargetDescription(target) {
    if (target.unavailable) {
      return t(
        'agents.access.unavailableAgentTarget',
        'This configured target is not present in the current Identity Agent or Project Team catalogs.',
      );
    }
    if (target.kind === 'project') {
      return t(
        'agents.access.projectAgentTarget',
        'Project Agent · {agent} · {project}',
        {
          agent: target.displayName || target.name,
          project: target.projectName || target.projectId,
        },
      );
    }
    return t('agents.access.identityAgentTarget', 'Identity Agent · {agent}', {
      agent: target.displayName || target.name,
    });
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

<div
  class="management-topic"
  role="tabpanel"
  id="agent-detail-panel-access"
  aria-labelledby="agent-detail-tab-access"
  hidden={activeDetail !== 'access'}
  tabindex="0"
>
  <div class="detail-group">
    <div class="detail-group-title">
      {t('agents.detail.access', 'Access')}
    </div>

    <div class="tl-section">
      <div class="tl-section-header">
        <span class="tl-section-label">
          {t('agents.form.toolAccess', 'Tool access')}
        </span>
        <InfoHint
          text={t(
            'agents.form.toolAccessHelp',
            'Choose all Tools, your own selection, or none. Click a Tool name or a family switch to change access. Dashed Tools activate automatically when their condition is met.',
          )}
        />
      </div>
      <div class="agents-view__tool-access-content">
        <ToolAccessEditor
          value={formValues.tool_access}
          tools={availableTools}
          memoryPromptMode={formValues.memory_prompt_mode}
          onChange={(next) => (formValues.tool_access = next)}
          onOpenExtensions={navigateToExtensions}
        />
      </div>
    </div>

    <div class="tl-section">
      <div class="tl-section-header">
        <span class="tl-section-label">
          {t('agents.form.allowedSkills', 'Allowed skills')}
        </span>
      </div>
      <ToggleChipList
        items={skillChipItems}
        emptyLabel={t(
          'agents.access.noSkills',
          'No loadable skills are available.',
        )}
        note={skillsAreWildcard && visibleSkillItems.length > 0
          ? t(
              'agents.form.wildcardNote',
              'Currently all are allowed, including ones added in the future. Turning any single item off switches to a fixed list.',
            )
          : ''}
        ariaToggleLabel={(name) =>
          t('agents.access.toggleSkill', 'Toggle skill {name}', { name })}
        onToggle={(name, next) =>
          updateAccessItem('allowed_skills', name, next)}
        onSetAll={(next) =>
          (formValues.allowed_skills = next ? [WILDCARD_ACCESS] : [])}
      />
      {#if invalidSkills.length > 0}
        <div class="agents-view__invalid-skills">
          <div class="agents-view__invalid-skills-title">
            {t('agents.access.invalidSkillsTitle', 'Unavailable skills')}
          </div>
          <div class="agents-view__invalid-skills-list">
            {#each invalidSkills as item (item.path || item.name)}
              <div class="agents-view__invalid-skill">
                <div class="agents-view__access-copy">
                  <span class="tl-item-name">
                    {item.name ||
                      t('agents.access.unknownSkillName', 'Unknown skill')}
                  </span>
                  {#if item.path}
                    <span class="agents-view__invalid-skill-path">
                      {item.path}
                    </span>
                  {/if}
                  {#if Array.isArray(item.warnings) && item.warnings.length > 0}
                    <div class="agents-view__skill-warnings">
                      <span class="agents-view__warning-label">
                        {t('agents.access.skillWarnings', 'Warnings')}
                      </span>
                      <ul>
                        {#each item.warnings as warning, index (`${item.path || item.name}-warning-${index}`)}
                          <li>{warning}</li>
                        {/each}
                      </ul>
                    </div>
                  {/if}
                </div>
                <StatusChip variant="warn">
                  {t('agents.access.notLoadable', 'not loadable')}
                </StatusChip>
              </div>
            {/each}
          </div>
        </div>
      {/if}
    </div>

    {#if subagentToolEnabled}
      <div class="tl-section">
        <div class="tl-section-header">
          <span class="tl-section-label">
            {t('agents.form.subagentSettings', 'Sub-Agent settings')}
            <InfoHint
              text={t(
                'agents.form.allowedAgentsHelp',
                'Additional targets for subagent. The calling Agent is always available by omitting agent_id and is not listed here. Project Agents use agent@project ids. Rooting does not narrow this permission.',
              )}
            />
          </span>
        </div>
        <section aria-labelledby="agent-identity-targets-label">
          <h4
            id="agent-identity-targets-label"
            class="agents-view__access-group-label"
          >
            {t('agents.access.identityAgents', 'Identity Agents')}
          </h4>
          <ToggleChipList
            items={identityAgentChipItems}
            emptyLabel={t(
              'agents.access.noIdentityAgentTargets',
              'No additional Identity Agents are available.',
            )}
            note={agentsAreWildcard && visibleAgentTargetItems.length > 0
              ? t(
                  'agents.form.agentWildcardNote',
                  'Additional Agents: all other Identity Agents and all Agents on every registered Project, including ones added later. The calling Agent remains implicit. Rooting does not narrow this.',
                )
              : t(
                  'agents.form.agentAddressNote',
                  'Additional Agents use bare Identity ids or agent@project ids. The calling Agent remains implicit. Rooting does not change this list.',
                )}
            ariaToggleLabel={(name) =>
              t('agents.access.toggleAgent', 'Toggle agent {name}', {
                name,
              })}
            onToggle={(name, next) =>
              updateAccessItem('allowed_agents', name, next)}
            onSetAll={(next) =>
              setAgentGroupAccess(identityAgentChipItems, next)}
          />
        </section>
        {#if projectAgentChipItems.length > 0}
          <section
            class="agents-view__project-targets"
            aria-labelledby="agent-project-targets-toggle"
          >
            <Button
              id="agent-project-targets-toggle"
              variant="tertiary"
              class="agents-view__access-group-toggle"
              aria-expanded={projectAgentsOpen}
              aria-controls="agent-project-targets"
              onClick={() => (projectAgentsOpen = !projectAgentsOpen)}
            >
              <span
                class="agents-view__access-chevron"
                class:is-open={projectAgentsOpen}
                aria-hidden="true">▸</span
              >
              <span>{t('agents.access.projectAgents', 'Project Agents')}</span>
              <span class="agents-view__access-group-count"
                >({selectedProjectAgentCount}/{projectAgentChipItems.length})</span
              >
            </Button>
            <div id="agent-project-targets" hidden={!projectAgentsOpen}>
              <ToggleChipList
                items={projectAgentChipItems}
                ariaToggleLabel={(name) =>
                  t('agents.access.toggleAgent', 'Toggle agent {name}', {
                    name,
                  })}
                onToggle={(name, next) =>
                  updateAccessItem('allowed_agents', name, next)}
                onSetAll={(next) =>
                  setAgentGroupAccess(projectAgentChipItems, next)}
              />
            </div>
          </section>
        {/if}
        {#if agentTargetCatalogError}
          <p class="agents-view__placeholder-row" role="status">
            {agentTargetCatalogError}
          </p>
        {/if}
      </div>
    {/if}
  </div>
</div>
