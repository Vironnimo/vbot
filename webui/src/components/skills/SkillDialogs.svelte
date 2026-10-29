<script>
  // The Skill dialogs of the Skills manager and the Agent editor's Skills
  // section: create a Skill, edit a package's SKILL.md,
  // confirm a delete (into the archive), a revert of history revisions and
  // the permanent delete of an archived package. State and requests live in
  // actions.svelte.js.
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import './skills.css';

  let { actions } = $props();
</script>

{#if actions.showCreateModal}
  <Modal
    title={t('settings.skills.create')}
    size="lg"
    labelledById="skill-create-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeCreateModal}
  >
    {#snippet body()}
      <div class="modal-body">
        <FormField
          controlId="create-scope"
          label={t('skills.createScopeLabel')}
          help={actions.createScope === 'global'
            ? t('skills.createGlobalHelp')
            : t('skills.createPrivateHelp')}
        >
          {#snippet children(field)}
            <Dropdown
              id={field.controlId}
              disabled={actions.busy}
              value={actions.createScope}
              options={actions.scopeOptions}
              ariaLabel={t('skills.createScopeLabel')}
              ariaDescribedby={field.describedBy}
              onValueChange={(value) => (actions.createScope = value)}
            />
          {/snippet}
        </FormField>
        <FormField
          controlId="new-skill-name"
          label={t('settings.skills.nameLabel')}
        >
          <TextField
            id="new-skill-name"
            disabled={actions.busy}
            value={actions.newName}
            onInput={(next) => (actions.newName = next)}
            placeholder={t('settings.skills.namePlaceholder')}
          />
        </FormField>
        <FormField controlId="new-skill-description">
          {#snippet labelContent()}
            {t('skills.descriptionLabel')}
            <InfoHint text={t('skills.descriptionHelp')} />
          {/snippet}
          <TextField
            id="new-skill-description"
            disabled={actions.busy}
            value={actions.newDescription}
            onInput={(value) => (actions.newDescription = value)}
            placeholder={t('skills.descriptionPlaceholder')}
          />
        </FormField>
        <FormField
          controlId="new-skill-content"
          label={t('skills.instructions')}
        >
          <TextArea
            id="new-skill-content"
            disabled={actions.busy}
            rows="10"
            autoGrow
            value={actions.newContent}
            onInput={(value) => (actions.newContent = value)}
            placeholder={t('skills.instructionsPlaceholder')}
          />
        </FormField>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.busy}
        onClick={actions.closeCreateModal}
      >
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.createDisabled}
        onClick={actions.createSkill}
      >
        {t('settings.skills.create')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if actions.editing}
  <Modal
    title={t('skills.editTitle', { name: actions.editing.name })}
    size="lg"
    labelledById="skill-edit-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeEditModal}
  >
    {#snippet body()}
      <div class="modal-body">
        {#if actions.editing.shared}<Banner variant="info"
            >{t('skills.editSharedHelp')}</Banner
          >{/if}
        <FormField
          controlId={`skill-content-${actions.editing.name}`}
          label={t('settings.skills.contentLabel')}
        >
          <TextArea
            id={`skill-content-${actions.editing.name}`}
            code
            rows="16"
            autoGrow
            value={actions.editContent}
            onInput={(value) => (actions.editContent = value)}
          />
        </FormField>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.busy}
        onClick={actions.closeEditModal}
      >
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        disabled={actions.busy}
        onClick={actions.saveEdit}
      >
        {actions.busy ? t('common.saving') : t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if actions.deleteTarget}
  <ConfirmDialog
    title={t('skills.deleteConfirmTitle', { name: actions.deleteTarget.name })}
    body={actions.deleteTarget.message}
    confirmLabel={t('common.delete')}
    onConfirm={actions.confirmDelete}
    onCancel={actions.cancelDelete}
  />
{/if}

{#if actions.revertTarget}
  <ConfirmDialog
    title={actions.revertTarget.later !== null
      ? t('skills.revert.togetherTitle', {
          revisions: actions.revertTarget.revisions.join(', '),
        })
      : actions.revertTarget.pass
        ? t('skills.revert.passTitle')
        : t('skills.revert.title', {
            revision: actions.revertTarget.revisions[0],
            name: actions.revertTarget.name,
          })}
    body={actions.revertTarget.later !== null
      ? t('skills.revert.together', {
          later: actions.revertTarget.later,
          name: actions.revertTarget.name,
        })
      : actions.revertTarget.pass
        ? t('skills.revert.pass')
        : t('skills.revert.confirm')}
    confirmLabel={actions.revertTarget.later === null &&
    !actions.revertTarget.pass
      ? t('skills.revert.action')
      : t('skills.revert.togetherAction')}
    danger={false}
    onConfirm={actions.confirmRevert}
    onCancel={actions.cancelRevert}
  />
{/if}

{#if actions.purgeTarget}
  <ConfirmDialog
    title={t('skills.archived.purgeTitle', { name: actions.purgeTarget.name })}
    body={t('skills.archived.purgeConfirm')}
    confirmLabel={t('skills.archived.purgeAction')}
    onConfirm={actions.confirmPurge}
    onCancel={actions.cancelPurge}
  />
{/if}
