<script module>
  const loadEditor = () => import('./TemplateEditor.svelte');
</script>

<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Checkbox from '../../../../webui/src/components/ui/Checkbox.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import LazyContent from '../../../../webui/src/components/ui/LazyContent.svelte';
  import { templateKind } from './library.js';

  let { model, onNavigate } = $props();
  let archived = $state(false);
  const entries = $derived(
    model.library.filter((item) => archived || !item.archived),
  );
</script>

<div class="factory-setups">
  <nav class="factory-project-list" aria-label={t('factory.library')}>
    <h2>{t('factory.library')}</h2>
    <p class="factory-help">{t('factory.library.help')}</p>
    <Button variant="secondary" onClick={() => onNavigate('/library/new')}
      >{t('factory.library.new')}</Button
    >
    <Checkbox
      id="factory-library-archived"
      checked={archived}
      onChange={(value) => (archived = value)}
      >{t('factory.library.showArchived')}</Checkbox
    >
    {#each entries as item (item.id)}
      <button
        class="factory-project-choice"
        class:factory-project-choice--selected={model.templateId === item.id}
        aria-current={model.templateId === item.id ? 'page' : undefined}
        onclick={() => onNavigate(`/library/${encodeURIComponent(item.id)}`)}
      >
        <span>{item.name}</span>
        <span class="factory-help"
          >{templateKind(item.kind)} · {t('factory.library.version', {
            version: item.latest_version ?? item.version,
          })}{item.archived ? ` · ${t('factory.library.archived')}` : ''}</span
        >
      </button>
    {:else}<p class="factory-help">{t('factory.library.emptyList')}</p>{/each}
  </nav>
  <div class="factory-setup-content">
    {#if model.detailLoading}<p role="status">{t('common.loading')}</p>
    {:else if model.templateId === 'new' || model.selectedTemplate?.id === model.templateId}
      {#key model.templateId}<LazyContent load={loadEditor}
          >{#snippet children(Component)}<Component
              {model}
              record={model.selectedTemplate}
              {onNavigate}
            />{/snippet}</LazyContent
        >{/key}
    {:else}<EmptyState
        title={t('factory.library.choose')}
        description={t('factory.library.chooseHelp')}
      />{/if}
  </div>
</div>

<style>
  nav :global(.checkbox) {
    margin: 16px 0;
  }
</style>
