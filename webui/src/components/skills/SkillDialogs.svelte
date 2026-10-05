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
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import './skills.css';

  let { actions } = $props();
</script>

{#if actions.showCreateModal}
  <Modal
    title={t('settings.skills.newSkill')}
    class="skills-editor-modal"
    labelledById="skill-create-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeCreateModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        <div class="skills-field">
          <label class="skills-field-label" for="create-scope">
            {t('skills.createScopeLabel')}
          </label>
          <Dropdown
            id="create-scope"
            value={actions.createScope}
            options={actions.scopeOptions}
            ariaLabel={t('skills.createScopeLabel')}
            onValueChange={(value) => (actions.createScope = value)}
          />
          <p class="skills-secondary">
            {actions.createScope === 'global'
              ? t('skills.createGlobalHelp')
              : t('skills.createPrivateHelp')}
          </p>
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-name">
            {t('settings.skills.nameLabel')}
          </label>
          <TextField
            id="new-skill-name"
            value={actions.newName}
            onInput={(next) => (actions.newName = next)}
            placeholder={t('settings.skills.namePlaceholder')}
          />
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-description">
            {t('skills.descriptionLabel')}
            <InfoHint text={t('skills.descriptionHelp')} />
          </label>
          <TextField
            id="new-skill-description"
            value={actions.newDescription}
            onInput={(value) => (actions.newDescription = value)}
            placeholder={t('skills.descriptionPlaceholder')}
          />
        </div>
        <div class="skills-field">
          <label class="skills-field-label" for="new-skill-content">
            {t('skills.instructions')}
          </label>
          <TextArea
            id="new-skill-content"
            rows="12"
            value={actions.newContent}
            onInput={(value) => (actions.newContent = value)}
            placeholder={t('skills.instructionsPlaceholder')}
          />
        </div>
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
    class="skills-editor-modal"
    labelledById="skill-edit-modal-title"
    closeDisabled={actions.busy}
    onClose={actions.closeEditModal}
  >
    {#snippet body()}
      <div class="skills-modal-body">
        {#if actions.editing.shared}<Banner variant="info"
            >{t('skills.editSharedHelp')}</Banner
          >{/if}
        <div class="skills-field">
          <label
            class="skills-field-label"
            for={`skill-content-${actions.editing.name}`}
          >
            {t('settings.skills.contentLabel')}
          </label>
          <TextArea
            id={`skill-content-${actions.editing.name}`}
            code
            rows="16"
            value={actions.editContent}
            onInput={(value) => (actions.editContent = value)}
          />
        </div>
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
    title={t('settings.skills.deleteConfirmTitle')}
    body={actions.deleteTarget.message}
    confirmLabel={t('common.delete')}
    onConfirm={actions.confirmDelete}
    onCancel={actions.cancelDelete}
  />
{/if}

{#if actions.revertTarget}
  <ConfirmDialog
    title={t('skills.revert.title')}
    body={actions.revertTarget.later !== null
      ? t('skills.revert.together', {
          later: actions.revertTarget.later,
          name: actions.revertTarget.name,
          revisions: actions.revertTarget.revisions.join(', '),
        })
      : actions.revertTarget.pass
        ? t('skills.revert.pass', {
            revisions: actions.revertTarget.revisions.join(', '),
          })
        : t('skills.revert.confirm', {
            revision: actions.revertTarget.revisions[0],
            name: actions.revertTarget.name,
          })}
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
    title={t('skills.archived.purgeTitle')}
    body={t('skills.archived.purgeConfirm', { name: actions.purgeTarget.name })}
    confirmLabel={t('skills.archived.purgeAction')}
    onConfirm={actions.confirmPurge}
    onCancel={actions.cancelPurge}
  />
{/if}
