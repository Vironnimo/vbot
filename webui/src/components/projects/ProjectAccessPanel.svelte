<script>
  import { t } from '$lib/i18n.js';
  import SkillSelectionPanel from '../skills/SkillSelectionPanel.svelte';
  import {
    projectSkillPatch,
    projectSkillView,
  } from '../skills/skillAccess.js';
  import ToolCatalogEditor from '../tools/ToolCatalogEditor.svelte';
  import Button from '../ui/Button.svelte';
  import {
    buildToolToggleList,
    buildSkillToggleSections,
  } from '$lib/projectsView.js';
  let { projectsState, projectsController, navigateToExtensions } = $props();

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
      project: skillToggleSections.project.map(activeRow),
      bundled: skillToggleSections.bundled.map(activeRow),
      global: skillToggleSections.global.map(activeRow),
    }),
  );

  let skillQuery = $state('');

  function activeRow(skill) {
    return { ...skill, active: skill.enabled };
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
        columns
        emptyTitle={t('skills.empty.project')}
        emptyHelp={t('skills.empty.projectHelp')}
      />
    </div>
  </section>
</div>
