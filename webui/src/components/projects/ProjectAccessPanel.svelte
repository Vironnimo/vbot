<script>
  import { t } from '$lib/i18n.js';
  import ToolCatalogEditor from '../tools/ToolCatalogEditor.svelte';
  import ToggleChipList from '../ui/ToggleChipList.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
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
          ? t(
              'projects.manage.unavailableToolHint',
              'This stored Tool Whitelist entry is not currently registered for Projects. Turn it off to remove the permission, or leave it on so the permission returns with the Tool.',
            )
          : tool.readiness_hint,
    })),
  );

  let projectSkillChips = $derived(
    skillToggleSections.project.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );

  let bundledSkillChips = $derived(
    skillToggleSections.bundled.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );

  let globalSkillChips = $derived(
    skillToggleSections.global.map((skill) => ({
      ...skill,
      allowed: skill.enabled,
    })),
  );

  function toggleTool(name, enabled) {
    projectsController.updateListField('allowed_tools', name, enabled);
  }

  function toggleProjectSkill(name, active) {
    projectsController.updateListField(
      'skills_project_disabled',
      name,
      !active,
    );
  }

  function toggleBundledSkill(name, enabled) {
    projectsController.updateListField('skills_bundled_enabled', name, enabled);
  }

  function toggleGlobalSkill(name, enabled) {
    projectsController.updateListField('skills_global_enabled', name, enabled);
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

  function setAllProjectSkills(enabled) {
    projectsController.replaceListField(
      'skills_project_disabled',
      enabled ? [] : skillToggleSections.project.map((skill) => skill.name),
    );
  }

  function setAllBundledSkills(enabled) {
    projectsController.replaceListField(
      'skills_bundled_enabled',
      enabled ? skillToggleSections.bundled.map((skill) => skill.name) : [],
    );
  }

  function setAllGlobalSkills(enabled) {
    projectsController.replaceListField(
      'skills_global_enabled',
      enabled ? skillToggleSections.global.map((skill) => skill.name) : [],
    );
  }
</script>

<div class="management-topic" id="project-detail-panel-access">
  <!-- Section 4: Tools -->
  <div class="detail-section">
    <h3 class="detail-section-title">
      {t('projects.detail.sectionTools', 'Tools')}
    </h3>
    <div class="detail-section-body">
      <div class="projects-field">
        <span class="projects-label">
          {t('projects.manage.allowedTools', 'Tool whitelist')}
        </span>
        <p class="projects-help">
          {t(
            'projects.manage.allowedToolsHelp',
            'The maximum tools this project’s agents may use. An individual agent may use fewer through its own permissions.',
          )}
        </p>
        <ToolCatalogEditor
          items={toolChipItems}
          toggleLabel={(tool) =>
            t('projects.manage.toggleTool', 'Toggle tool {name}', {
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
              >{t('toolAccess.selectAll', 'Select all')}</Button
            >
            <Button variant="tertiary" onClick={() => setAllTools(false)}
              >{t('toolAccess.deselectAll', 'Deselect all')}</Button
            >
            <Button
              variant="tertiary"
              data-testid="project-tools-reset"
              onClick={resetToolsToDefaults}
            >
              {t('projects.manage.resetDefaults', 'Reset to defaults')}
            </Button>
          {/snippet}
        </ToolCatalogEditor>
      </div>
    </div>
  </div>

  <!-- Section 5: Skills -->
  <div class="detail-section">
    <h3 class="detail-section-title">
      <span>{t('projects.detail.sectionSkills', 'Skills')}</span>
    </h3>
    <div class="detail-section-body">
      <div class="projects-field">
        <span class="projects-label">
          {t('projects.manage.allowedSkills', 'Skill whitelist')}
        </span>
        <p class="projects-help">
          {t(
            'projects.manage.allowedSkillsHelp',
            'Project skills are active by default; bundled and global skills are opt-in.',
          )}
        </p>
        {#if skillToggleSections.project.length > 0}
          <span class="projects-sublabel">
            {t('projects.manage.projectSkills', 'Project skills')}
          </span>
          <ToggleChipList
            items={projectSkillChips}
            ariaToggleLabel={(name) =>
              t('projects.manage.toggleSkill', 'Toggle skill {name}', {
                name,
              })}
            onToggle={(name, next) => toggleProjectSkill(name, next)}
            onSetAll={setAllProjectSkills}
          />
        {/if}
        {#if skillToggleSections.bundled.length > 0}
          <span class="projects-sublabel">
            {t('projects.manage.bundledSkills', 'Bundled skills')}
          </span>
          <ToggleChipList
            items={bundledSkillChips}
            ariaToggleLabel={(name) =>
              t('projects.manage.toggleSkill', 'Toggle skill {name}', {
                name,
              })}
            onToggle={(name, next) => toggleBundledSkill(name, next)}
            onSetAll={setAllBundledSkills}
          />
        {/if}
        {#if skillToggleSections.global.length > 0}
          <span class="projects-sublabel">
            {t('projects.manage.globalSkills', 'Global skills')}
          </span>
          <ToggleChipList
            items={globalSkillChips}
            ariaToggleLabel={(name) =>
              t('projects.manage.toggleSkill', 'Toggle skill {name}', {
                name,
              })}
            onToggle={(name, next) => toggleGlobalSkill(name, next)}
            onSetAll={setAllGlobalSkills}
          />
        {/if}
        {#if skillToggleSections.project.length === 0 && skillToggleSections.bundled.length === 0 && skillToggleSections.global.length === 0}
          <EmptyState
            density="compact"
            description={t(
              'projects.manage.skillsEmpty',
              'No skills available',
            )}
          />
        {/if}
      </div>
    </div>
  </div>
</div>
