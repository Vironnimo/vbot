<script>
  // The Sources of one kind (Agents or Skills), shown in the
  // section that uses them. The Project stores one ordered list; dragging
  // reorders a kind among its own rows only, because names compete only
  // within a kind. Skill folders the user added carry a Remove action.
  import { t } from '$lib/i18n.js';
  import {
    folderSourcePath,
    reorderSourcesOfKind,
    sourceKind,
  } from '$lib/projectsView.js';
  import { formatLabel } from './projectLabels.js';
  import SortableList from '../ui/SortableList.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Button from '../ui/Button.svelte';

  let { projectsState, projectsController, kind } = $props();

  function kindOf(source) {
    return sourceKind(source, projectsState.activeSources);
  }
  let sources = $derived(
    projectsState.editForm.sources.filter((source) => kindOf(source) === kind),
  );

  function sourceInfo(id) {
    return projectsState.activeSources.find((source) => source.id === id);
  }
  function sourceLabel(source) {
    return (
      folderSourcePath(source.id) ??
      formatLabel(sourceInfo(source.id)?.ecosystem ?? source.id)
    );
  }
  function setSources(next) {
    projectsController.updateEditField('sources', next);
  }
  function toggle(id, enabled) {
    setSources(
      projectsState.editForm.sources.map((source) =>
        source.id === id ? { ...source, enabled } : source,
      ),
    );
  }
  function remove(id) {
    setSources(
      projectsState.editForm.sources.filter((source) => source.id !== id),
    );
  }
  function reorder(from, to) {
    setSources(
      reorderSourcesOfKind(
        projectsState.editForm.sources,
        kindOf,
        kind,
        from,
        to,
      ),
    );
  }
  function expand(id) {
    setSources(
      projectsState.editForm.sources.map((item) => {
        if (item.id !== id) return item;
        const expanded = { ...item };
        delete expanded.agent_paths;
        return expanded;
      }),
    );
  }
  // A row counts what its kind supplies; a folder row names only its count,
  // because its label already is the path.
  function sourceSummary(source, info) {
    const folder = folderSourcePath(source.id) !== null;
    if (!info?.detected)
      return folder
        ? t('projects.sources.folderMissing')
        : t('projects.sources.absent');
    const paths = folder ? '' : (info.paths ?? []).join(', ');
    let count = '';
    if (kind === 'agents')
      count =
        info.agents === 1
          ? t('projects.sources.agentCount.one')
          : t('projects.sources.agentCount.many', { count: info.agents });
    if (kind === 'skills')
      count =
        info.skills === 1
          ? t('projects.sources.skillCount.one')
          : t('projects.sources.skillCount.many', { count: info.skills });
    return [paths, count].filter(Boolean).join(' · ');
  }
</script>

{#if sources.length > 0}
  <SortableList
    class="s-group projects-source-list"
    itemClass="s-row s-row--compact projects-source-row"
    items={sources}
    getKey={(source) => source.id}
    getLabel={sourceLabel}
    onReorder={reorder}
    itemFocusable
    aria-label={t(`projects.sources.${kind}`)}
  >
    {#snippet item(source)}
      {@const info = sourceInfo(source.id)}
      {@const folder = folderSourcePath(source.id) !== null}
      <div class="s-row-info">
        <span class="s-row-label" class:projects-source-folder={folder}
          >{sourceLabel(source)}</span
        >
        <div class="s-row-desc">
          {sourceSummary(source, info)}
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
              onClick={() => expand(source.id)}
              >{t('projects.sources.expand')}</Button
            >
          </div>
        {/if}
      </div>
      <div class="s-row-control projects-source-actions">
        {#if folder}
          <Button
            variant="tertiary"
            ariaLabel={t('projects.sources.removeFolder', {
              folder: sourceLabel(source),
            })}
            onClick={() => remove(source.id)}>{t('common.remove')}</Button
          >
        {/if}
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
{:else}
  <div class="s-group">
    <div class="s-group__block s-group__note">
      {kind === 'agents'
        ? t('projects.sources.agentsEmpty')
        : t('projects.sources.skillsEmpty')}
    </div>
  </div>
{/if}
