<script>
  // The Skills one Agent can use, edited through its allowlist pair
  // (`allowed_skills` + `excluded_skills`). Project grants are shown locked;
  // own Skills and every other change go through the skillAccess.js rules and
  // are reported as the next pair, so the Skills manager can save it at once
  // and the Agent editor can keep it as a draft. `onContextMenu(item, event,
  // toggle)` offers a row's context menu; `toggle(on)` applies the same rule
  // as the row's checkbox.
  import { t } from '$lib/i18n.js';
  import SkillSelectionPanel from './SkillSelectionPanel.svelte';
  import {
    agentSkillView,
    setAutoAdd,
    toggleSkill,
    toggleSkills,
  } from './skillAccess.js';

  const noop = () => {};

  let {
    agent = null,
    agentId = '',
    access,
    inventory = [],
    agents = [],
    projects = [],
    query = '',
    showFilter = false,
    onQuery = noop,
    onChange = noop,
    onOpen = null,
    onContextMenu = null,
    columns = false,
  } = $props();

  let view = $derived(
    agentSkillView(agent, access, {
      inventory,
      projects,
      agents,
      agentId: agent?.id ?? agentId,
    }),
  );

  function setGroup(groupId, on) {
    const group = view.groups.find((item) => item.id === groupId);
    const names = (group?.items ?? [])
      .filter((item) => !item.locked)
      .map((item) => item.name);
    if (names.length)
      onChange(toggleSkills(access, names, on, groupId === 'own'));
  }
</script>

<SkillSelectionPanel
  groups={view.groups}
  active={view.active}
  total={view.total}
  {query}
  {showFilter}
  {onQuery}
  autoAdd={{
    checked: view.autoAdd,
    onChange: (on) => onChange(setAutoAdd(access, view.governed, on, view.own)),
  }}
  onToggle={(_group, name, on, item) =>
    onChange(toggleSkill(access, name, on, item.own))}
  onSetAll={setGroup}
  {onOpen}
  onContextMenu={onContextMenu &&
    ((_group, item, event) =>
      onContextMenu(item, event, (on) =>
        onChange(toggleSkill(access, item.name, on, item.own)),
      ))}
  {columns}
  emptyTitle={t('skills.empty.agent')}
  emptyHelp={t('skills.empty.agentHelp')}
/>
