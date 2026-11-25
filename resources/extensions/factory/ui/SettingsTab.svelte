<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import RecordEditor from './RecordEditor.svelte';

  let { model } = $props();
  let editor = $state(null);

  async function archiveFactory() {
    if (editor && !(await editor.flush())) return;
    await model.mutate('factory.archive', {
      id: model.factory.id,
      expected_revision: model.factory.revision,
    });
  }
</script>

<section class="factory-settings" aria-label={t('factory.tabs.settings')}>
  {#key model.factory.id}
    <RecordEditor
      bind:this={editor}
      kind="factory"
      record={model.factory}
      {model}
    />
  {/key}

  {#if ['active', 'paused'].includes(model.factory.state)}
    <div class="factory-settings-actions">
      <Button
        variant="danger"
        disabled={Boolean(model.pending)}
        onClick={archiveFactory}>{t('factory.settings.archive')}</Button
      >
    </div>
  {/if}
</section>

<style>
  .factory-settings {
    max-width: 720px;
    display: grid;
    gap: 20px;
  }
  .factory-settings-actions {
    display: flex;
    gap: 12px;
  }
</style>
