<script>
  import { t } from '$lib/i18n.js';
  import { formatLabel } from './projectLabels.js';
  import SortableList from '../ui/SortableList.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';

  let { projectsState, projectsController, modelOptions = [] } = $props();
  let wish = $state('');
  let target = $state('');

  function sourceInfo(id) {
    return projectsState.activeSources.find((source) => source.id === id);
  }
  function sourceLabel(source) {
    const info = sourceInfo(source.id);
    if (!info || info.kind === 'unknown') return source.id;
    return `${formatLabel(info.ecosystem)} · ${t(`projects.sources.${info.kind}`)}`;
  }
  function toggle(id, enabled) {
    projectsController.updateEditField(
      'sources',
      projectsState.editForm.sources.map((source) =>
        source.id === id ? { ...source, enabled } : source,
      ),
    );
  }
  function reorder(from, to) {
    const sources = [...projectsState.editForm.sources];
    sources.splice(to, 0, sources.splice(from, 1)[0]);
    projectsController.updateEditField('sources', sources);
  }
  // Each kind of source loads one kind of thing, so its row counts only that.
  function sourceSummary(info) {
    if (!info?.detected) return t('projects.sources.absent');
    if (info.kind === 'agents') {
      return info.agents === 1
        ? t('projects.sources.agentCount.one')
        : t('projects.sources.agentCount.many', { count: info.agents });
    }
    if (info.kind === 'skills') {
      return info.skills === 1
        ? t('projects.sources.skillCount.one')
        : t('projects.sources.skillCount.many', { count: info.skills });
    }
    return (info.paths ?? []).join(', ');
  }
  let mappings = $derived(
    Object.entries(projectsState.editForm.model_mappings),
  );
  function mapModel(name, value) {
    const mappings = { ...projectsState.editForm.model_mappings };
    if (value) mappings[name] = value;
    else delete mappings[name];
    projectsController.updateEditField('model_mappings', mappings);
  }
</script>

<section class="s-section" aria-labelledby="project-section-sources">
  <header class="s-section__head">
    <h3 class="s-section__title" id="project-section-sources">
      {t('projects.sources.title')}
    </h3>
  </header>
  <p class="s-section__desc">{t('projects.sources.help')}</p>
  <div class="s-section__body">
    <SortableList
      class="s-group projects-source-list"
      itemClass="s-row s-row--compact projects-source-row"
      items={projectsState.editForm.sources}
      getLabel={sourceLabel}
      onReorder={reorder}
      itemFocusable
      aria-label={t('projects.sources.title')}
    >
      {#snippet item(source)}
        {@const info = sourceInfo(source.id)}
        <div class="s-row-info">
          <span class="s-row-label">{sourceLabel(source)}</span>
          <div
            class="s-row-desc"
            class:projects-source-paths={info?.detected &&
              info.kind === 'instructions'}
          >
            {sourceSummary(info)}
          </div>
          {#if info?.problem}
            <div class="s-row-desc projects-source-problem">
              {info.problem}
            </div>
          {/if}
          {#if source.agent_paths}
            <div class="s-row-desc projects-source-scope">
              {t('projects.sources.scope', {
                paths: source.agent_paths.join(', '),
              })}
              <Button
                variant="tertiary"
                class="projects-source-expand"
                onClick={() =>
                  projectsController.updateEditField(
                    'sources',
                    projectsState.editForm.sources.map((item) => {
                      if (item.id !== source.id) return item;
                      const expanded = { ...item };
                      delete expanded.agent_paths;
                      return expanded;
                    }),
                  )}>{t('projects.sources.expand')}</Button
              >
            </div>
          {/if}
        </div>
        <Toggle
          checked={source.enabled}
          ariaLabel={t('projects.sources.toggle', {
            source: sourceLabel(source),
          })}
          onChange={(enabled) => toggle(source.id, enabled)}
        />
      {/snippet}
    </SortableList>
    <div class="s-group">
      <details class="s-disclosure projects-model-mappings">
        <summary>
          {t('projects.sources.modelMappings')}
          {#if mappings.length > 0}
            <span class="s-disclosure__meta">{mappings.length}</span>
          {/if}
        </summary>
        <div class="s-group__rows">
          <p class="s-group__note projects-model-mappings__note">
            {t('projects.sources.modelHelp')}
          </p>
          {#each mappings as [name, value] (name)}
            <div class="s-row">
              <div class="s-row-info">
                <span class="s-row-label">{name}</span>
              </div>
              <div class="s-row-control projects-model-mappings__control">
                <SearchableDropdown
                  {value}
                  options={modelOptions}
                  ariaLabel={name}
                  onValueChange={(next) => mapModel(name, next)}
                />
                <Button variant="tertiary" onClick={() => mapModel(name, '')}
                  >{t('common.remove')}</Button
                >
              </div>
            </div>
          {/each}
          <div class="s-row">
            <div class="s-row-info">
              <TextField
                value={wish}
                aria-label={t('projects.sources.modelWish')}
                placeholder={t('projects.sources.modelWish')}
                onInput={(value) => (wish = value)}
              />
            </div>
            <div class="s-row-control projects-model-mappings__control">
              <SearchableDropdown
                value={target}
                options={modelOptions.filter((option) => option.value)}
                ariaLabel={t('projects.sources.modelTarget')}
                onValueChange={(value) => (target = value)}
              />
              <Button
                disabled={!wish.trim() || !target}
                onClick={() => {
                  mapModel(wish.trim(), target);
                  wish = '';
                  target = '';
                }}>{t('projects.sources.addMapping')}</Button
              >
            </div>
          </div>
        </div>
      </details>
    </div>
  </div>
</section>
