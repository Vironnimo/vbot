<script>
  // The Terminals view's group dialogs: New group and Rename group (one
  // small name dialog) and the Delete group confirmation. The view opens them
  // through the exported functions; the controller does the work.
  import { tick } from 'svelte';

  import { t } from '$lib/i18n.js';
  import { groupCanEdit, terminalError, viewLabel } from './terminalLabels.js';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';

  let { viewState, controller, serverUnavailable, onToast } = $props();

  const NAME_FIELD_ID = 'terminal-group-name';

  let groupDialogOpen = $state(false);
  let groupDialogMode = $state('create');
  let groupDialogName = $state('');
  let groupDialogTargetId = $state('');
  let nameError = $state('');
  let deleteGroupTargetId = $state('');

  const deleteGroup = $derived(
    deleteGroupTargetId
      ? (viewState.groups.find(
          (group) => group.group_id === deleteGroupTargetId,
        ) ?? null)
      : null,
  );
  const actionError = $derived(
    viewState.actionError && !serverUnavailable
      ? terminalError(viewState.actionError)
      : '',
  );

  async function openGroupDialog(mode, group = null) {
    groupDialogMode = mode;
    groupDialogName = group?.name ?? '';
    groupDialogTargetId = group?.group_id ?? '';
    nameError = '';
    viewState.actionError = '';
    groupDialogOpen = true;
    await tick();
    document.getElementById(NAME_FIELD_ID)?.focus();
  }

  export function openCreateGroupDialog() {
    void openGroupDialog('create');
  }

  export function openRenameGroupDialog(group) {
    if (groupCanEdit(group)) {
      void openGroupDialog('rename', group);
    }
  }

  function closeGroupDialog() {
    if (viewState.groupActionPending) {
      return;
    }
    groupDialogOpen = false;
    viewState.actionError = '';
  }

  async function submitGroupDialog(event) {
    event.preventDefault();
    const name = groupDialogName.trim();
    if (!name) {
      nameError = t('terminals.groupNameRequired');
      document.getElementById(NAME_FIELD_ID)?.focus();
      return;
    }
    if (groupDialogMode === 'create') {
      const group = await controller.createGroup(name);
      if (!group) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupCreatedTitle'),
        message: t('terminals.groupCreatedMessage'),
        variant: 'success',
      });
    } else {
      const renamed = await controller.renameGroup(groupDialogTargetId, name);
      if (!renamed) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupRenamedTitle'),
        message: t('terminals.groupRenamedMessage'),
        variant: 'success',
      });
    }
  }

  export function openDeleteGroupDialog(group) {
    if (!groupCanEdit(group)) {
      return;
    }
    deleteGroupTargetId = group.group_id;
    viewState.actionError = '';
  }

  function closeDeleteGroupDialog() {
    if (viewState.groupActionPending) {
      return;
    }
    deleteGroupTargetId = '';
    viewState.actionError = '';
  }

  async function confirmDeleteGroup() {
    if (!deleteGroup) {
      deleteGroupTargetId = '';
      return;
    }
    const result = await controller.deleteGroup(deleteGroupTargetId);
    if (!result) {
      return;
    }
    deleteGroupTargetId = '';
    onToast({
      title: t('terminals.groupDeletedTitle'),
      message: t('terminals.deleteGroupMessage'),
      variant: 'success',
    });
  }
</script>

{#if groupDialogOpen}
  <Modal
    title={groupDialogMode === 'create'
      ? t('terminals.createGroupTitle')
      : t('terminals.renameGroupTitle')}
    description={groupDialogMode === 'create'
      ? t('terminals.createGroupDescription')
      : ''}
    size="sm"
    closeDisabled={viewState.groupActionPending}
    onClose={closeGroupDialog}
  >
    {#snippet body()}
      <form id="terminal-group-form" onsubmit={submitGroupDialog}>
        <div class="modal-body">
          <FormField
            controlId={NAME_FIELD_ID}
            label={t('terminals.groupNameLabel')}
            error={nameError || actionError}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={groupDialogName}
                invalid={field.invalid}
                disabled={viewState.groupActionPending}
                placeholder={t('terminals.groupNamePlaceholder')}
                spellcheck="false"
                onInput={(next) => {
                  groupDialogName = next;
                  nameError = '';
                  viewState.actionError = '';
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.groupActionPending}
        onClick={closeGroupDialog}
      >
        {t('common.cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-group-form"
        variant="primary"
        loading={viewState.groupActionPending}
      >
        {groupDialogMode === 'create'
          ? t('terminals.createGroup')
          : t('terminals.renameGroup')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if deleteGroup}
  <ConfirmDialog
    title={t('terminals.deleteGroupQuestion', { name: viewLabel(deleteGroup) })}
    body={t('terminals.deleteGroupWarning')}
    confirmLabel={t('terminals.deleteGroupConfirm')}
    busy={viewState.groupActionPending}
    error={actionError}
    onConfirm={() => void confirmDeleteGroup()}
    onCancel={closeDeleteGroupDialog}
  />
{/if}
