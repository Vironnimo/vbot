<script module>
  const loadEditor = () => import('./SetupEditor.svelte');
</script>

<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import LazyContent from '../../../../webui/src/components/ui/LazyContent.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  let { model, onNavigate } = $props();
  const project = $derived(
    model.catalog.projects?.find((entry) => entry.id === model.setupId),
  );
</script>

<div class="factory-setups">
  <nav class="factory-project-list" aria-label={t('factory.setups')}>
    <h2>{t('factory.setups')}</h2>
    {#each model.catalog.projects ?? [] as entry (entry.id)}
      {@const setup = model.setups.find((item) => item.project_id === entry.id)}
      <button
        class="factory-project-choice"
        class:factory-project-choice--selected={model.setupId === entry.id}
        aria-current={model.setupId === entry.id ? 'page' : undefined}
        onclick={() => onNavigate(`/setups/${encodeURIComponent(entry.id)}`)}
      >
        <span>{entry.name}</span><span class="factory-help"
          >{!setup
            ? t('factory.notConfigured')
            : setup.enabled
              ? t('factory.enabled')
              : t('factory.disabled')}</span
        >
      </button>
    {:else}<p class="factory-help">{t('factory.noProjects')}</p>{/each}
  </nav>
  <div class="factory-setup-content">
    {#if project && !model.detailLoading}
      {#key `${project.id}:${model.setupGeneration}`}<LazyContent
          load={loadEditor}
          >{#snippet children(Component)}<Component
              {project}
              {model}
              setup={model.selectedSetup}
            />{/snippet}</LazyContent
        >{/key}
    {:else if model.detailLoading}<p role="status">{t('common.loading')}</p>
    {:else}<EmptyState
        title={t('factory.setups')}
        description={t('factory.noSetups')}
      />{/if}
  </div>
</div>
