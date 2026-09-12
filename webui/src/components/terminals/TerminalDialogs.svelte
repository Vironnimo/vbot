<script>
  import { tick } from 'svelte';
  import { t } from '$lib/i18n.js';
  import {
    parseTerminalCommandLine,
    formatTerminalCommandLine,
  } from '$lib/terminalsView.js';
  import {
    groupKindLabel,
    groupCanEdit,
    terminalError,
    launchHistoryLabel,
    launchHistoryWorkdir,
  } from './terminalLabels.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  let { viewState, controller, serverUnavailable, onToast, onStarted } =
    $props();
  let selectedGroup = $derived(
    viewState.groups.find(
      (group) => group.group_id === viewState.selectedGroupId,
    ) ?? null,
  );

  let startDialogOpen = $state(false);

  let selectedLaunchHistoryId = $state('');

  let startCommand = $state('');

  let startCommandError = $state('');

  let startWorkdir = $state('');

  let startName = $state('');

  let startGroupId = $state('');

  let groupDialogOpen = $state(false);

  let groupDialogMode = $state('create');

  let groupDialogName = $state('');

  let groupDialogTargetId = $state('');

  let deleteGroupDialogOpen = $state(false);

  let deleteGroupTargetId = $state('');

  let launchHistoryOptions = $derived(
    viewState.launchHistory.map((entry) => ({
      value: entry.id,
      label: launchHistoryLabel(entry),
      secondaryLabel: launchHistoryWorkdir(entry),
    })),
  );

  let groupOptions = $derived(
    viewState.groups
      .filter((group) => group.kind === 'user' || group.kind === 'agent')
      .map((group) => ({
        value: group.group_id,
        label: group.name,
        secondaryLabel: groupKindLabel(group.kind),
      })),
  );

  const groupOptionAutomatic = {
    value: '',
    label: t('terminals.groupAutomatic', 'Automatic'),
    secondaryLabel: t('terminals.kind.manual', 'Manual'),
  };

  export function openStartDialog() {
    applyLaunchHistory(viewState.launchHistory[0] ?? null);
    startName = '';
    startGroupId = groupCanEdit(selectedGroup)
      ? (viewState.selectedGroupId ?? '')
      : '';
    viewState.startError = '';
    startDialogOpen = true;
  }

  function applyLaunchHistory(entry) {
    selectedLaunchHistoryId = entry?.id ?? '';
    startCommand = formatTerminalCommandLine(entry?.command, entry?.args);
    startCommandError = '';
    startWorkdir = entry?.workdir ?? '';
    viewState.startError = '';
  }

  function selectLaunchHistory(entryId) {
    const entry = viewState.launchHistory.find((item) => item.id === entryId);
    if (entry) {
      applyLaunchHistory(entry);
    }
  }

  function markLaunchHistoryEdited() {
    startCommandError = '';
    selectedLaunchHistoryId = '';
    viewState.startError = '';
  }

  function closeStartDialog() {
    if (viewState.startingTerminal) {
      return;
    }
    startDialogOpen = false;
    viewState.startError = '';
  }

  async function submitStartTerminal(event) {
    event.preventDefault();
    const parsed = parseTerminalCommandLine(startCommand);
    if (parsed.error) {
      startCommandError = t(`terminals.commandError.${parsed.error}`);
      await tick();
      document.getElementById('terminal-start-command')?.focus();
      return;
    }
    startCommandError = '';
    const { command, args } = parsed;
    const workdir = startWorkdir.trim();
    const name = startName.trim();
    const params = {};
    if (command) {
      params.command = command;
    }
    if (args.length) {
      params.args = args;
    }
    if (workdir) {
      params.workdir = workdir;
    }
    if (name) {
      params.name = name;
    }

    const started = await controller.startManualTerminal(params, {
      groupId: startGroupId,
    });
    if (!started) {
      return;
    }
    startDialogOpen = false;
    await onStarted(started.terminal_id);
    onToast({
      title: t('terminals.startedTitle', 'Terminal started'),
      message: t(
        'terminals.startedMessage',
        'The manual Terminal Session is live and ready for input.',
      ),
      variant: 'success',
    });
  }

  export function openCreateGroupDialog() {
    groupDialogMode = 'create';
    groupDialogName = '';
    groupDialogTargetId = '';
    viewState.actionError = '';
    groupDialogOpen = true;
  }

  export function openRenameGroupDialog(group) {
    if (!groupCanEdit(group)) {
      return;
    }
    groupDialogMode = 'rename';
    groupDialogName = group.name;
    groupDialogTargetId = group.group_id;
    viewState.actionError = '';
    groupDialogOpen = true;
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
      viewState.actionError = t(
        'terminals.groupNameRequired',
        'Enter a group name.',
      );
      return;
    }
    if (groupDialogMode === 'create') {
      const group = await controller.createGroup(name);
      if (!group) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupCreatedTitle', 'Group created'),
        message: t(
          'terminals.groupCreatedMessage',
          'The group is ready and appears in the terminal list.',
        ),
        variant: 'success',
      });
    } else {
      const renamed = await controller.renameGroup(groupDialogTargetId, name);
      if (!renamed) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupRenamedTitle', 'Group renamed'),
        message: t(
          'terminals.groupRenamedMessage',
          'The group name was updated.',
        ),
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
    deleteGroupDialogOpen = true;
  }

  function closeDeleteGroupDialog() {
    if (viewState.groupActionPending) {
      return;
    }
    deleteGroupDialogOpen = false;
    viewState.actionError = '';
  }

  async function confirmDeleteGroup() {
    const group = viewState.groups.find(
      (item) => item.group_id === deleteGroupTargetId,
    );
    if (!group) {
      deleteGroupDialogOpen = false;
      return;
    }
    const result = await controller.deleteGroup(deleteGroupTargetId);
    if (!result) {
      return;
    }
    deleteGroupDialogOpen = false;
    onToast({
      title: t('terminals.deleteGroupTitle', 'Delete group'),
      message: t(
        'terminals.deleteGroupMessage',
        'The group was deleted and its terminals were stopped.',
      ),
      variant: 'success',
    });
  }
</script>

{#if startDialogOpen}
  <Modal
    title={t('terminals.startTitle', 'New terminal')}
    labelledById="terminal-start-modal-title"
    class="terminals-view__start-modal"
    closeDisabled={viewState.startingTerminal}
    onClose={closeStartDialog}
  >
    {#snippet body()}
      <form id="terminal-start-form" onsubmit={submitStartTerminal}>
        <div class="modal-body terminals-view__start-form">
          {#if viewState.startError && !serverUnavailable}
            <Banner
              variant="error"
              role="alert"
              class="terminals-view__start-error"
            >
              {terminalError(viewState.startError)}
            </Banner>
          {/if}

          {#if viewState.launchHistory.length > 0}
            <FormField controlId="terminal-start-history" full>
              {#snippet labelContent()}
                <span>{t('terminals.historyLabel', 'Recent setup')}</span>
                <InfoHint
                  text={t('terminals.historyHelp')}
                  ariaLabel={t('terminals.historyLabel', 'Recent setup')}
                />
              {/snippet}

              {#snippet children(field)}
                <Dropdown
                  id={field.controlId}
                  value={selectedLaunchHistoryId}
                  options={launchHistoryOptions}
                  ariaLabel={t('terminals.historyLabel', 'Recent setup')}
                  ariaDescribedby={field.describedBy}
                  disabled={viewState.startingTerminal}
                  triggerClass="terminals-view__history-dropdown"
                  listClass="terminals-view__history-dropdown-list"
                  onValueChange={selectLaunchHistory}
                />
              {/snippet}
            </FormField>
          {/if}

          <FormField
            controlId="terminal-start-command"
            full
            error={startCommandError}
          >
            {#snippet labelContent()}
              <span>{t('terminals.commandLabel', 'Command line')}</span>
              <InfoHint
                text={t('terminals.commandHelp')}
                ariaLabel={t('terminals.commandLabel', 'Command line')}
              />
            {/snippet}

            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.commandLabel', 'Command line')}
                value={startCommand}
                invalid={field.invalid}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.commandPlaceholder', 'Default shell')}
                onInput={(next) => {
                  startCommand = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-workdir" full>
            {#snippet labelContent()}
              <span>{t('terminals.workdirLabel', 'Working directory')}</span>
              <InfoHint
                text={t('terminals.workdirHelp')}
                ariaLabel={t('terminals.workdirLabel', 'Working directory')}
              />
            {/snippet}

            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.workdirLabel', 'Working directory')}
                value={startWorkdir}
                disabled={viewState.startingTerminal}
                placeholder={t(
                  'terminals.workdirPlaceholder',
                  'User home directory',
                )}
                onInput={(next) => {
                  startWorkdir = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-name">
            {#snippet labelContent()}
              <span>{t('terminals.nameLabel', 'Name')}</span>
              <InfoHint
                text={t('terminals.nameHelp')}
                ariaLabel={t('terminals.nameLabel', 'Name')}
              />
            {/snippet}

            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.nameLabel', 'Name')}
                value={startName}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.namePlaceholder', 'Unnamed')}
                onInput={(next) => {
                  startName = next;
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-group">
            {#snippet labelContent()}
              <span>{t('terminals.startGroupLabel', 'Group')}</span>
              <InfoHint
                text={t('terminals.startGroupHelp')}
                ariaLabel={t('terminals.startGroupLabel', 'Group')}
              />
            {/snippet}

            {#snippet children(field)}
              <Dropdown
                id={field.controlId}
                value={startGroupId}
                options={[groupOptionAutomatic, ...groupOptions]}
                ariaLabel={t('terminals.startGroupLabel', 'Group')}
                ariaDescribedby={field.describedBy}
                disabled={viewState.startingTerminal}
                triggerClass="terminals-view__group-dropdown"
                listClass="terminals-view__group-dropdown-list"
                onValueChange={(next) => {
                  startGroupId = next;
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
        disabled={viewState.startingTerminal}
        onClick={closeStartDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-start-form"
        variant="primary"
        loading={viewState.startingTerminal}
      >
        {t('terminals.start', 'Start terminal')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if groupDialogOpen}
  <Modal
    title={groupDialogMode === 'create'
      ? t('terminals.createGroupTitle', 'New group')
      : t('terminals.renameGroupTitle', 'Rename group')}
    labelledById="terminal-group-modal-title"
    class="terminals-view__group-modal"
    closeDisabled={viewState.groupActionPending}
    onClose={closeGroupDialog}
  >
    {#snippet body()}
      <form id="terminal-group-form" onsubmit={submitGroupDialog}>
        <div class="modal-body">
          {#if viewState.actionError && !serverUnavailable}
            <Banner variant="error" role="alert">
              {terminalError(viewState.actionError)}
            </Banner>
          {/if}
          <FormField
            controlId="terminal-group-name"
            label={t('terminals.groupNameLabel', 'Group name')}
            help={t(
              'terminals.groupNameHelp',
              'Shown in the sidebar. Terminals you start here join this group.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={groupDialogName}
                disabled={viewState.groupActionPending}
                placeholder={t('terminals.groupNamePlaceholder', 'e.g. Work')}
                onInput={(next) => {
                  groupDialogName = next;
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
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-group-form"
        variant="primary"
        loading={viewState.groupActionPending}
      >
        {groupDialogMode === 'create'
          ? t('terminals.createGroup', 'Create group')
          : t('terminals.renameGroup', 'Rename')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if deleteGroupDialogOpen}
  {@const deleteGroup = viewState.groups.find(
    (group) => group.group_id === deleteGroupTargetId,
  )}
  <Modal
    title={t('terminals.deleteGroupTitle', 'Delete group')}
    labelledById="terminal-delete-group-title"
    closeDisabled={viewState.groupActionPending}
    onClose={closeDeleteGroupDialog}
  >
    {#snippet body()}
      <div class="modal-body">
        {#if viewState.actionError && !serverUnavailable}
          <Banner variant="error" role="alert">
            {terminalError(viewState.actionError)}
          </Banner>
        {/if}
        <p class="terminals-view__delete-intro">
          {t(
            'terminals.deleteGroupWarning',
            'Deleting this group stops every running terminal in it. Finished terminals remain available until they expire.',
          )}
        </p>
        {#if deleteGroup && deleteGroup.terminal_count > 0}
          <Banner variant="warn">
            {t(
              'terminals.deleteGroupCount',
              '{count} terminals are in this group.',
              {
                count: deleteGroup.terminal_count,
              },
            )}
          </Banner>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.groupActionPending}
        onClick={closeDeleteGroupDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="danger"
        loading={viewState.groupActionPending}
        onClick={() => void confirmDeleteGroup()}
      >
        {deleteGroup?.terminal_count
          ? t('terminals.deleteGroupConfirm', 'Delete {count} terminal(s)', {
              count: deleteGroup.terminal_count,
            })
          : t('terminals.deleteGroupEmptyConfirm', 'Delete group')}
      </Button>
    {/snippet}
  </Modal>
{/if}
