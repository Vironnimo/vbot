<script>
  import { t } from '$lib/i18n.js';
  import Modal from '../ui/Modal.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';

  let { actions } = $props();
  const id = $props.id();
</script>

{#if actions.policySession}
  <Modal
    title={t('sessions.compactionPolicy')}
    labelledById={`${id}-title`}
    size="lg"
    closeDisabled={actions.policySaving}
    onClose={actions.closePolicyEdit}
  >
    {#snippet body()}
      <div class="modal-body">
        <!-- Without the override the rows show the inherited Policy, read-only. -->
        <div class="s-group">
          <div class="s-row s-row--compact">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('sessions.compactionOverride')}
              </div>
              <div class="s-row-desc">
                {t('sessions.compactionOverrideDescription')}
              </div>
            </div>
            <div class="s-row-control">
              <Toggle
                checked={actions.policyUsesOverride}
                disabled={actions.policySaving}
                ariaLabel={t('sessions.compactionOverride')}
                onChange={(enabled) => (actions.policyUsesOverride = enabled)}
              />
            </div>
          </div>
          <CompactionPolicyEditor
            layout="rows"
            value={actions.policyDraft}
            disabled={!actions.policyUsesOverride || actions.policySaving}
            idPrefix={id}
            onChange={(next) => (actions.policyDraft = next)}
          />
        </div>
        {#if actions.policyError}
          <Banner variant="error" role="alert">{actions.policyError}</Banner>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.policySaving}
        onClick={actions.closePolicyEdit}
      >
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.policySaving}
        onClick={actions.savePolicy}
      >
        {actions.policySaving ? t('common.saving') : t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}
