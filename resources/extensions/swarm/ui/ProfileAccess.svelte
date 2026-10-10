<script>
  // The Tool access and Skills sections of the Swarm editor: what every
  // participant may use. It edits the editor's bound draft.
  // `allowed_skills` is `['*']` while new Skills are added automatically and
  // names the allowed Skills otherwise; saved names the catalog does not list
  // (a Project's own Skills) are kept.
  import { SvelteSet } from 'svelte/reactivity';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import SkillSelectionPanel from '../../../../webui/src/components/skills/SkillSelectionPanel.svelte';
  import ToolAccessEditor from '../../../../webui/src/components/tools/ToolAccessEditor.svelte';
  import { changeToolAccessMode } from '../../../../webui/src/lib/toolAccess.js';

  let { draft = $bindable(), catalog = {} } = $props();

  let skillQuery = $state('');

  const tools = $derived(
    (catalog.tools ?? []).filter(
      (tool) => !(tool.constraints ?? []).includes('identity_agent'),
    ),
  );
  const skillNames = $derived(
    (catalog.skills ?? []).map((skill) => skill.name),
  );
  const everySkill = $derived(draft.allowed_skills.includes('*'));
  const skillItems = $derived(
    (catalog.skills ?? []).map((skill) => ({
      key: skill.name,
      name: skill.name,
      allowed: everySkill || draft.allowed_skills.includes(skill.name),
      detail: skill.description || '',
    })),
  );
  const skillGroups = $derived(
    skillItems.length
      ? [
          {
            id: 'installed',
            title: t('swarm.profile.skillGroup'),
            allLabel: t('skills.panel.allInGroup', {
              group: t('swarm.profile.skillGroup'),
            }),
            items: skillItems,
          },
        ]
      : [],
  );

  function setToolAccess(next) {
    // Swarms pin the current selection. Materialize All/None through the
    // shared policy owner instead of relabeling an incompatible policy shape.
    draft.tool_access = changeToolAccessMode(next, 'selected', tools);
    if (next.mode === 'none' || next.mode === 'all') {
      const denied = new SvelteSet(draft.tool_access.denied ?? []);
      for (const tool of tools.filter((item) => item.family === 'swarm')) {
        if (next.mode === 'none') denied.add(tool.name);
        else denied.delete(tool.name);
      }
      if (denied.size) draft.tool_access.denied = [...denied];
      else delete draft.tool_access.denied;
    }
  }

  // Changing single Skills ends automatic adding: the list then names every
  // Skill that stays allowed.
  function setSkills(names, allowed) {
    if (everySkill && allowed) return;
    const next = new SvelteSet(
      everySkill
        ? skillNames
        : draft.allowed_skills.filter((name) => name !== '*'),
    );
    for (const name of names) {
      if (allowed) next.add(name);
      else next.delete(name);
    }
    draft.allowed_skills = [...next];
  }

  function setAutoAdd(on) {
    draft.allowed_skills = on ? ['*'] : [...skillNames];
  }
</script>

<section class="s-section" aria-labelledby="swarm-section-tools">
  <header class="s-section__head">
    <h3 class="s-section__title" id="swarm-section-tools">
      {t('swarm.profile.tools')}
    </h3>
  </header>
  <p class="s-section__desc">{t('swarm.profile.toolSelectionHelp')}</p>
  <div class="s-section__body">
    <ToolAccessEditor
      value={draft.tool_access}
      memoryPromptMode="off"
      {tools}
      onChange={setToolAccess}
    />
  </div>
</section>

<section class="s-section" aria-labelledby="swarm-section-skills">
  <header class="s-section__head">
    <h3 class="s-section__title" id="swarm-section-skills">
      {t('swarm.profile.skillsHeading')}
    </h3>
  </header>
  <p class="s-section__desc">{t('swarm.profile.skillsHelp')}</p>
  <div class="s-section__body">
    <SkillSelectionPanel
      groups={skillGroups}
      active={skillItems.filter((item) => item.allowed).length}
      total={skillItems.length}
      query={skillQuery}
      showFilter
      onQuery={(next) => (skillQuery = next)}
      autoAdd={{ checked: everySkill, onChange: setAutoAdd }}
      onToggle={(_group, name, allowed) => setSkills([name], allowed)}
      onSetAll={(_group, allowed) => setSkills(skillNames, allowed)}
      emptyTitle={t('swarm.profile.noSkills')}
    />
  </div>
</section>
