<script>
  import { t } from '$lib/i18n.js';
  import Toggle from '../ui/Toggle.svelte';
  import MemoryEntriesEditor from '../memory/MemoryEntriesEditor.svelte';

  let {
    projectId,
    enabled,
    onEnabledChange,
    refreshToken = 0,
    onToast = () => {},
  } = $props();
  const panelId = $props.id();
</script>

<section class="s-section" aria-labelledby={`project-memory-${panelId}`}>
  <header class="s-section__head">
    <h3 class="s-section__title" id={`project-memory-${panelId}`}>
      {t('memory.scope.project')}
    </h3>
  </header>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">{t('memory.projectEnabled')}</div>
          <div class="s-row-desc">{t('memory.projectEnabledHelp')}</div>
        </div>
        <div class="s-row-control">
          <Toggle
            checked={enabled}
            ariaLabel={t('memory.projectEnabled')}
            onChange={onEnabledChange}
          />
        </div>
      </div>
      <MemoryEntriesEditor
        {projectId}
        scopes={['project']}
        activeScopes={enabled ? ['project'] : []}
        {refreshToken}
        onToast={(title) => onToast({ title, variant: 'success' })}
      />
    </div>
  </div>
</section>
