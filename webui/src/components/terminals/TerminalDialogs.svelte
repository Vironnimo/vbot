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
    launchHistoryDetails,
    launchHistoryLabel,
    launchHistoryWorkdir,
  } from './terminalLabels.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import PathField from '../ui/PathField.svelte';
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
      tooltip: launchHistoryDetails(entry),
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
    label: t('terminals.groupAutomatic'),
    secondaryLabel: t('terminals.kind.manual'),
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
      title: t('terminals.startedTitle'),
      message: t('terminals.startedMessage'),
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
      viewState.actionError = t('terminals.groupNameRequired');
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
      title: t('terminals.deleteGroupTitle'),
      message: t('terminals.deleteGroupMessage'),
      variant: 'success',
    });
  }
</script>

{#if startDialogOpen}
  <Modal
    title={t('terminals.startTitle')}
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
                <span>{t('terminals.historyLabel')}</span>
                <InfoHint
                  text={t('terminals.historyHelp')}
                  ariaLabel={t('terminals.historyLabel')}
                />
              {/snippet}

              {#snippet children(field)}
                <Dropdown
                  id={field.controlId}
                  ariaLabelledby={field.labelId}
                  value={selectedLaunchHistoryId}
                  options={launchHistoryOptions}
                  ariaLabel={t('terminals.historyLabel')}
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
              <span>{t('terminals.commandLabel')}</span>
              <InfoHint
                text={t('terminals.commandHelp')}
                ariaLabel={t('terminals.commandLabel')}
              />
            {/snippet}

            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.commandLabel')}
                code
                value={startCommand}
                invalid={field.invalid}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.commandPlaceholder')}
                onInput={(next) => {
                  startCommand = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-workdir" full>
            {#snippet labelContent()}
              <span>{t('terminals.workdirLabel')}</span>
              <InfoHint
                text={t('terminals.workdirHelp')}
                ariaLabel={t('terminals.workdirLabel')}
              />
            {/snippet}

            {#snippet children(field)}
              <PathField
                id={field.controlId}
                variant="modal"
                mode="directory"
                projectShortcuts
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.workdirLabel')}
                value={startWorkdir}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.workdirPlaceholder')}
                onInput={(next) => {
                  startWorkdir = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-name">
            {#snippet labelContent()}
              <span>{t('terminals.nameLabel')}</span>
              <InfoHint
                text={t('terminals.nameHelp')}
                ariaLabel={t('terminals.nameLabel')}
              />
            {/snippet}

            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                ariaLabel={t('terminals.nameLabel')}
                value={startName}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.namePlaceholder')}
                onInput={(next) => {
                  startName = next;
                }}
              />
            {/snippet}
          </FormField>

          <FormField controlId="terminal-start-group">
            {#snippet labelContent()}
              <span>{t('terminals.startGroupLabel')}</span>
              <InfoHint
                text={t('terminals.startGroupHelp')}
                ariaLabel={t('terminals.startGroupLabel')}
              />
            {/snippet}

            {#snippet children(field)}
              <Dropdown
                id={field.controlId}
                ariaLabelledby={field.labelId}
                value={startGroupId}
                options={[groupOptionAutomatic, ...groupOptions]}
                ariaLabel={t('terminals.startGroupLabel')}
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
        {t('common.cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-start-form"
        variant="primary"
        loading={viewState.startingTerminal}
      >
        {t('terminals.start')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if groupDialogOpen}
  <Modal
    title={groupDialogMode === 'create'
      ? t('terminals.createGroupTitle')
      : t('terminals.renameGroupTitle')}
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
            label={t('terminals.groupNameLabel')}
            help={t('terminals.groupNameHelp')}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={groupDialogName}
                disabled={viewState.groupActionPending}
                placeholder={t('terminals.groupNamePlaceholder')}
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

{#if deleteGroupDialogOpen}
  {@const deleteGroup = viewState.groups.find(
    (group) => group.group_id === deleteGroupTargetId,
  )}
  <Modal
    title={t('terminals.deleteGroupTitle')}
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
          {t('terminals.deleteGroupWarning')}
        </p>
        {#if deleteGroup && deleteGroup.terminal_count > 0}
          <Banner variant="warn">
            {t('terminals.deleteGroupCount', {
              count: deleteGroup.terminal_count,
            })}
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
        {t('common.cancel')}
      </Button>
      <Button
        variant="danger"
        loading={viewState.groupActionPending}
        onClick={() => void confirmDeleteGroup()}
      >
        {deleteGroup?.terminal_count
          ? t('terminals.deleteGroupConfirm', {
              count: deleteGroup.terminal_count,
            })
          : t('terminals.deleteGroupEmptyConfirm')}
      </Button>
    {/snippet}
  </Modal>
{/if}
