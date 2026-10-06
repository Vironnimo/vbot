<script>
  // Who gets one Skill package, as editable rows: each Identity Agent (its
  // Skill selection, or sharing for another Agent's private Skill) and each
  // Project that can activate it. Fixed grants are locked with their reason.
  import { t } from '$lib/i18n.js';
  import AgentSelectionGroup from '../agents/AgentSelectionGroup.svelte';
  import {
    skillAccessOf,
    skillAccessView,
    toggleSkill,
  } from './skillAccess.js';

  const noop = () => {};
  const uid = $props.id();

  let {
    entry,
    agents = [],
    projects = [],
    inventory = [],
    onAgentAccess = noop,
    onShare = noop,
    onProjectSkills = noop,
  } = $props();

  let view = $derived(skillAccessView(entry, { agents, projects, inventory }));
  let agentItems = $derived(view.agents);
  let projectItems = $derived(view.projects);

  function agentById(id) {
    return agents.find((agent) => agent.id === id);
  }

  function grant(agentId, on, own = false) {
    const agent = agentById(agentId);
    if (agent)
      onAgentAccess(
        agent,
        toggleSkill(skillAccessOf(agent), entry.name, on, own),
      );
  }

  function toggleAgent(_name, on, item) {
    if (item.kind === 'share') {
      const current = Array.isArray(entry.shared_with) ? entry.shared_with : [];
      onShare(
        entry,
        on
          ? [...new Set([...current, item.agentId])]
          : current.filter((id) => id !== item.agentId),
      );
    } else if (item.kind === 'grant' || item.kind === 'own') {
      grant(item.agentId, on, item.kind === 'own');
    }
  }

  function toggleProject(_name, on, item) {
    const project = projects.find(
      (candidate) => candidate.project_id === item.projectId,
    );
    if (project && item.source)
      onProjectSkills(project, item.source, [entry.name], on);
  }

  function agentToggleLabel(name, item) {
    return item.kind === 'share'
      ? t('skills.access.shareWith', { name })
      : t('skills.access.toggleAgent', { name });
  }
</script>

<section class="skills-access" aria-labelledby={`${uid}-title`}>
  <h4 id={`${uid}-title`}>{t('skills.access.title')}</h4>
  <p class="skills-secondary">
    {entry.owner_id
      ? t('skills.access.privateHelp')
      : t('skills.access.poolHelp')}
  </p>
  <div class="skills-access__groups">
    <AgentSelectionGroup
      title={t('skills.access.agents')}
      titleId={`${uid}-agents`}
      items={agentItems}
      groupToggle={false}
      toggleLabel={agentToggleLabel}
      emptyLabel={t('skills.access.noAgents')}
      onToggle={toggleAgent}
      onAction={(item) => grant(item.agentId, true)}
    />
    {#if projectItems.length}
      <AgentSelectionGroup
        title={t('skills.access.projects')}
        titleId={`${uid}-projects`}
        items={projectItems}
        groupToggle={false}
        toggleLabel={(name) => t('skills.access.toggleProject', { name })}
        onToggle={toggleProject}
      />
    {/if}
  </div>
</section>
