<script>
  import { onDestroy } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { skillInventory } from '$lib/api.js';
  import SkillDialogs from '../skills/SkillDialogs.svelte';
  import SkillSelectionPanel from '../skills/SkillSelectionPanel.svelte';
  import { createSkillActions } from '../skills/actions.svelte.js';
  import {
    projectSkillPatch,
    projectSkillView,
  } from '../skills/skillAccess.js';
  import { projectRowMenu } from '../skills/skillMenus.js';
  import ToolCatalogEditor from '../tools/ToolCatalogEditor.svelte';
  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor } from '../ui/contextMenu.js';
  import {
    buildToolToggleList,
    buildSkillToggleSections,
  } from '$lib/projectsView.js';

  const noop = () => {};

  let {
    projectsState,
    projectsController,
    navigateToExtensions,
    onToast = noop,
    // Opens a Skill's page in the Skills manager: (projectId, skillId).
    onOpenSkill = noop,
    skillsRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();

  // The Skill inventory behind the rows' menus: each row's package, so a
  // Skill can be opened, edited, turned off or deleted where it is listed.
  let skillCatalog = $state({ skills: [], agents: [], projects: [] });
  let skillCatalogRequest = 0;
  let destroyed = false;
  onDestroy(() => (destroyed = true));

  async function loadSkillCatalog() {
    const request = ++skillCatalogRequest;
    try {
      const result = await skillInventory();
      if (destroyed || request !== skillCatalogRequest) return;
      skillCatalog = {
        skills: Array.isArray(result?.skills) ? result.skills : [],
        agents: Array.isArray(result?.agents) ? result.agents : [],
        projects: Array.isArray(result?.projects) ? result.projects : [],
      };
    } catch {
      // Without the inventory the rows keep their toggles; menus offer only
      // the Project switch and Copy name.
    }
  }

  // Loads once and again on every Skill, Agent or Project change.
  $effect(() => {
    void [skillsRefreshToken, agentsRefreshToken, projectsRefreshToken];
    void loadSkillCatalog();
  });

  const skillActions = createSkillActions({
    get agents() {
      return skillCatalog.agents;
    },
    inspected: null,
    get onToast() {
      return onToast;
    },
    loadInventory: loadSkillCatalog,
  });

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

  let toolChipItems = $derived(
    toolToggleRows.map((tool) => ({
      ...tool,
      allowed: tool.enabled,
      readiness_hint:
        tool.registered === false
          ? t('projects.manage.unavailableToolHint')
          : tool.readiness_hint,
    })),
  );

  // Each Skill pool is one selection group, shared with the Skills manager's
  // Project view; toggles edit the draft's Project Skill lists.
  let skillView = $derived(
    projectSkillView({
      project: skillToggleSections.project.map((row) =>
        activeRow(row, 'project'),
      ),
      bundled: skillToggleSections.bundled.map((row) =>
        activeRow(row, 'bundled'),
      ),
      global: skillToggleSections.global.map((row) => activeRow(row, 'global')),
    }),
  );

  let skillQuery = $state('');

  // The package each row's name resolves to in this Project (the inventory's
  // Project pool), so the row menu acts on the copy the Project uses.
  let packageIds = $derived(
    new Map(
      (
        skillCatalog.projects.find(
          (project) => project.project_id === projectsState.selectedProjectId,
        )?.skills ?? []
      ).map((row) => [`${row.source}:${row.name}`, row.package_id]),
    ),
  );

  function activeRow(skill, source) {
    return {
      ...skill,
      active: skill.enabled,
      packageId: packageIds.get(`${source}:${skill.name}`) ?? null,
    };
  }

  let skillMenu = $state(null);

  function openSkillMenu(source, item, event) {
    const projectId = projectsState.selectedProjectId;
    skillMenu = {
      ...contextMenuAnchor(event),
      ...projectRowMenu(
        item,
        {
          projectName:
            projectsState.editForm.display_name ||
            projectsController.selectedProject()?.display_name ||
            projectId,
          entry:
            skillCatalog.skills.find((entry) => entry.id === item.packageId) ??
            null,
          toggle: (on) => setProjectSkills(source, [item.name], on),
        },
        {
          open: (entry) => onOpenSkill(projectId, entry.id),
          edit: (entry) => void skillActions.startEdit(entry),
          copyName: (name) => void skillActions.copyName(name),
          setDisabled: (entry, disabled) =>
            skillActions.setDisabled(entry, disabled),
          remove: (entry) => skillActions.requestDelete(entry),
        },
      ),
    };
  }

  function setProjectSkills(source, names, active) {
    const patch = projectSkillPatch(
      projectsState.editForm,
      source,
      names,
      active,
    );
    for (const [field, values] of Object.entries(patch))
      projectsController.replaceListField(field, values);
  }

  function setSkillGroup(source, active) {
    const group = skillView.groups.find((item) => item.id === source);
    if (group)
      setProjectSkills(
        source,
        group.items.map((item) => item.name),
        active,
      );
  }

  function toggleTool(name, enabled) {
    projectsController.updateListField('allowed_tools', name, enabled);
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
</script>

<div class="management-topic" id="project-detail-panel-access">
  <section class="s-section" aria-labelledby="project-section-tools">
    <header class="s-section__head">
      <h3 class="s-section__title" id="project-section-tools">
        {t('projects.detail.sectionTools')}
      </h3>
    </header>
    <p class="s-section__desc">
      {t('projects.manage.allowedToolsHelp')}
    </p>
    <div class="s-section__body">
      <ToolCatalogEditor
        items={toolChipItems}
        toggleLabel={(tool) =>
          t('projects.manage.toggleTool', {
            name: tool.name,
          })}
        onToggle={(tool, next) => toggleTool(tool.name, next)}
        onToggleGroup={(members, enabled) => {
          const names = new Set(members.map((tool) => tool.name));
          projectsController.replaceListField(
            'allowed_tools',
            enabled
              ? [
                  ...new Set([
                    ...projectsState.editForm.allowed_tools,
                    ...names,
                  ]),
                ]
              : projectsState.editForm.allowed_tools.filter(
                  (name) => !names.has(name),
                ),
          );
        }}
        onOpenExtensions={navigateToExtensions}
      >
        {#snippet toolbar()}
          <Button variant="tertiary" onClick={() => setAllTools(true)}
            >{t('toolAccess.selectAll')}</Button
          >
          <Button variant="tertiary" onClick={() => setAllTools(false)}
            >{t('toolAccess.deselectAll')}</Button
          >
          <Button
            variant="tertiary"
            data-testid="project-tools-reset"
            onClick={resetToolsToDefaults}
          >
            {t('projects.manage.resetDefaults')}
          </Button>
        {/snippet}
      </ToolCatalogEditor>
    </div>
  </section>

  <section class="s-section" aria-labelledby="project-section-skills">
    <header class="s-section__head">
      <h3 class="s-section__title" id="project-section-skills">
        {t('projects.detail.sectionSkills')}
      </h3>
    </header>
    <p class="s-section__desc">
      {t('projects.manage.allowedSkillsHelp')}
    </p>
    <div class="s-section__body">
      <SkillSelectionPanel
        groups={skillView.groups}
        active={skillView.active}
        total={skillView.total}
        query={skillQuery}
        showFilter
        onQuery={(next) => (skillQuery = next)}
        onToggle={(source, name, active) =>
          setProjectSkills(source, [name], active)}
        onSetAll={setSkillGroup}
        onContextMenu={openSkillMenu}
        columns
        emptyTitle={t('skills.empty.project')}
        emptyHelp={t('skills.empty.projectHelp')}
      />
    </div>
  </section>
</div>

<SkillDialogs actions={skillActions} />
<ContextMenu menu={skillMenu} onClose={() => (skillMenu = null)} />
