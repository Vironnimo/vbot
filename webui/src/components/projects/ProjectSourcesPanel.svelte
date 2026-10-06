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
      items={projectsState.editForm.sources}
      getLabel={sourceLabel}
      onReorder={reorder}
      itemFocusable
    >
      {#snippet item(source)}
        {@const info = sourceInfo(source.id)}
        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label">{sourceLabel(source)}</span>
            <div class="s-row-desc">
              {info?.detected
                ? t('projects.sources.counts', {
                    agents: info.agents,
                    skills: info.skills,
                  })
                : t('projects.sources.absent')}
            </div>
            {#if info?.problem}<p>{info.problem}</p>{/if}
            {#if source.agent_paths}
              <div class="s-row-desc">
                {t('projects.sources.scope', {
                  paths: source.agent_paths.join(', '),
                })}
              </div>
              <Button
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
            {/if}
          </div>
          <Toggle
            checked={source.enabled}
            ariaLabel={t('projects.sources.toggle', {
              source: sourceLabel(source),
            })}
            onChange={(enabled) => toggle(source.id, enabled)}
          />
        </div>
      {/snippet}
    </SortableList>
    <details class="s-disclosure">
      <summary>{t('projects.sources.modelMappings')}</summary>
      <div class="s-disclosure__body">
        <p>{t('projects.sources.modelHelp')}</p>
        {#each Object.entries(projectsState.editForm.model_mappings) as [name, value] (name)}
          <div class="s-row">
            <code>{name}</code>
            <SearchableDropdown
              {value}
              options={modelOptions}
              ariaLabel={name}
              onValueChange={(next) => mapModel(name, next)}
            />
            <Button onClick={() => mapModel(name, '')}
              >{t('common.remove')}</Button
            >
          </div>
        {/each}
        <div class="s-row">
          <TextField
            value={wish}
            aria-label={t('projects.sources.modelWish')}
            placeholder={t('projects.sources.modelWish')}
            onInput={(value) => (wish = value)}
          />
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
    </details>
  </div>
</section>
