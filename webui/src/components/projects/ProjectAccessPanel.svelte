<script>
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import ToolCatalogEditor from '../tools/ToolCatalogEditor.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
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
          ? t('projects.manage.unavailableToolHint')
          : tool.readiness_hint,
    })),
  );

  // Each Skill source is one selection list: a group checkbox selects or
  // clears the whole source, a row checkbox one Skill.
  let skillGroups = $derived(
    [
      {
        id: 'project',
        title: t('projects.manage.projectSkills'),
        allLabel: t('projects.manage.allProjectSkills'),
        items: skillToggleSections.project,
        toggle: toggleProjectSkill,
        setAll: setAllProjectSkills,
      },
      {
        id: 'bundled',
        title: t('projects.manage.bundledSkills'),
        allLabel: t('projects.manage.allBundledSkills'),
        items: skillToggleSections.bundled,
        toggle: toggleBundledSkill,
        setAll: setAllBundledSkills,
      },
      {
        id: 'global',
        title: t('projects.manage.globalSkills'),
        allLabel: t('projects.manage.allGlobalSkills'),
        items: skillToggleSections.global,
        toggle: toggleGlobalSkill,
        setAll: setAllGlobalSkills,
      },
    ].filter((group) => group.items.length > 0),
  );

  let skillQuery = $state('');

  let visibleSkillGroups = $derived(
    skillGroups
      .map((group) => ({
        ...group,
        visible: group.items.filter((skill) =>
          [skill.name, skill.description ?? '']
            .join(' ')
            .toLocaleLowerCase()
            .includes(skillQuery.trim().toLocaleLowerCase()),
        ),
      }))
      .filter((group) => group.visible.length > 0),
  );

  let skillTotal = $derived(
    skillGroups.reduce((sum, group) => sum + group.items.length, 0),
  );

  let skillEnabledTotal = $derived(
    skillGroups.reduce(
      (sum, group) => sum + group.items.filter((skill) => skill.enabled).length,
      0,
    ),
  );

  function groupState(group) {
    const count = group.items.filter((skill) => skill.enabled).length;
    return count === group.items.length ? 'on' : count ? 'mixed' : 'off';
  }

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
      {#if skillGroups.length === 0}
        <EmptyState
          density="compact"
          description={t('projects.manage.skillsEmpty')}
        />
      {:else}
        <div class="s-group-toolbar projects-skill-toolbar">
          <span class="s-group-toolbar__meta projects-skill-summary">
            {t('projects.manage.skillSelectionCount', {
              enabled: skillEnabledTotal,
              total: skillTotal,
            })}
          </span>
          <label class="projects-skill-search">
            <input
              type="search"
              bind:value={skillQuery}
              placeholder={t('projects.manage.skillSearchPlaceholder')}
              aria-label={t('projects.manage.skillSearchLabel')}
            />
          </label>
        </div>
        {#if visibleSkillGroups.length === 0}
          <EmptyState
            density="compact"
            title={t('projects.manage.skillsNoMatch')}
          />
        {/if}
        <div class="s-check-groups">
          {#each visibleSkillGroups as group (group.id)}
            {@const state = groupState(group)}
            <section class="s-group s-check-group">
              <header class="s-check-group__head">
                <Checkbox
                  checked={state === 'on'}
                  indeterminate={state === 'mixed'}
                  ariaLabel={group.allLabel}
                  onChange={(next) => group.setAll(next)}
                />
                <h4 class="s-check-group__title">{group.title}</h4>
                <span class="s-check-group__count">
                  {group.items.filter((skill) => skill.enabled).length}/{group
                    .items.length}
                </span>
              </header>
              <div class="s-check-group__rows">
                {#each group.visible as skill (skill.name)}
                  <div class="projects-skill-row">
                    <Checkbox
                      class="s-check-row"
                      checked={skill.enabled}
                      ariaLabel={t('projects.manage.toggleSkill', {
                        name: skill.name,
                      })}
                      onChange={(next) => group.toggle(skill.name, next)}
                    >
                      <span class="s-check-row__name">{skill.name}</span>
                    </Checkbox>
                    {#if skill.description}
                      <div
                        class="floating-card projects-skill-tip"
                        use:floatingHoverCard
                      >
                        <strong>{skill.name}</strong>
                        <p>{skill.description}</p>
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
            </section>
          {/each}
        </div>
      {/if}
    </div>
  </section>
</div>
