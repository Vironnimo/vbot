<script>
  import { t } from '$lib/i18n.js';
  import ToggleChipList from '../ui/ToggleChipList.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    buildToolToggleList,
    buildSkillToggleSections,
  } from '$lib/projectsView.js';
  let {
    projectsState,
    projectsController,
    activeDetail,
    navigateToExtensions,
  } = $props();

  function projectToolGroupLabel(family, items = []) {
    const labels = {
      files: t('toolAccess.family.files', 'Files'),
      execution: t('toolAccess.family.execution', 'Execution'),
      web: t('toolAccess.family.web', 'Web'),
      sessions: t('toolAccess.family.sessions', 'Sessions'),
      skills: t('toolAccess.family.skills', 'Skills'),
      media: t('toolAccess.family.media', 'Media'),
    };
    return (
      labels[family] ??
      items.find((item) => item?.family_label)?.family_label ??
      (family
        ? String(family)
            .replaceAll('_', ' ')
            .replace(/\b\w/g, (letter) => letter.toUpperCase())
        : t('toolAccess.family.individual', 'Individual Tools'))
    );
  }

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

<div
  class="management-topic"
  role="tabpanel"
  id="project-detail-panel-access"
  aria-labelledby="project-detail-tab-access"
  hidden={activeDetail !== 'access'}
  tabindex="0"
>
  <!-- Section 4: Tools -->
  <div class="detail-section">
    <div class="detail-section-title">
      {t('projects.detail.sectionTools', 'Tools')}
    </div>
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
        <ToggleChipList
          items={toolChipItems}
          grouped
          groupLabel={projectToolGroupLabel}
          emptyLabel={t('projects.manage.toolsEmpty', 'No tools available')}
          ariaToggleLabel={(name) =>
            t('projects.manage.toggleTool', 'Toggle tool {name}', {
              name,
            })}
          onToggle={(name, next) => toggleTool(name, next)}
          onSetAll={setAllTools}
          onOpenExtensions={navigateToExtensions}
        >
          {#snippet headerActions()}
            <Button
              variant="tertiary"
              data-testid="project-tools-reset"
              onClick={resetToolsToDefaults}
            >
              {t('projects.manage.resetDefaults', 'Reset to defaults')}
            </Button>
          {/snippet}
        </ToggleChipList>
      </div>
    </div>
  </div>

  <!-- Section 5: Skills -->
  <div class="detail-section">
    <div class="detail-section-title">
      <span>{t('projects.detail.sectionSkills', 'Skills')}</span>
    </div>
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
