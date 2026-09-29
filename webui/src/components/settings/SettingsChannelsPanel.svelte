<script>
  import { onDestroy, onMount } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import WhatsAppSetup from './WhatsAppSetup.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createChannel,
    deleteChannel as deleteChannelRequest,
    disableChannel,
    enableChannel,
    getChannelAccess,
    getChannelStatus,
    grantChannelAdmin,
    listAgents,
    listChannels,
    revokeChannelAdmin,
    setChannelIdentity,
    updateChannel,
  } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    CHANNEL_DM_SCOPES,
    CHANNEL_FORM_MODE_CREATE,
    CHANNEL_FORM_MODE_EDIT,
    CHANNEL_PLATFORMS,
    applyChannelPanelList,
    buildChannelCreatePayload,
    buildChannelUpdatePayload,
    createChannelFormValues,
    createChannelPanelState,
    getAgentItems,
    mergeChannelStatuses,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let { onToast = noop, onError = noop, channelsRefreshToken = 0 } = $props();
  const uid = $props.id();

  let channelPanelState = $state(createChannelPanelState());
  let channelAgents = $state([]);
  let channelFormVisible = $state(false);
  let channelFormMode = $state(CHANNEL_FORM_MODE_CREATE);
  let channelFormValues = $state(createChannelFormValues());
  let channelBusy = $state(false);
  let channelActionChannelId = $state('');
  // Local form-validation message only (e.g. "select an agent"). Operation and
  // server errors go to `onError` (a sticky error toast); success feedback goes
  // to `onToast`.
  let channelFormError = $state('');
  // The channel awaiting delete confirmation (null = dialog closed). The delete
  // only runs once the confirm dialog resolves.
  let deleteConfirmChannel = $state(null);
  let lastChannelsRefreshToken = $state(null);
  let pendingExternalReload = $state(false);

  let channelBaseline = $state('');
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => channelFormValues,
    hasChanges: () =>
      channelFormVisible &&
      channelFormMode === CHANNEL_FORM_MODE_EDIT &&
      JSON.stringify(channelFormValues) !== channelBaseline,
    save: (reason) => persistChannelForm(reason),
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);
  $effect(() => {
    if (channelBusy || !autosave.participant.hasPending()) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });
  function submitChannelForm(event) {
    event.preventDefault();
    if (channelFormMode === CHANNEL_FORM_MODE_CREATE)
      return persistChannelForm('manual');
    return autosave.participant.runSave('manual', { force: true });
  }

  let channelPlatformOptions = $derived(
    CHANNEL_PLATFORMS.map((platformId) => ({
      value: platformId,
      label: channelPlatformLabel(platformId),
    })),
  );
  let channelDmScopeOptions = $derived(
    CHANNEL_DM_SCOPES.map((scopeId) => ({
      value: scopeId,
      label: channelDmScopeLabel(scopeId),
    })),
  );
  let channelAgentOptions = $derived(
    channelAgents.map((agent) => ({
      value: agent.id,
      label: agent.name,
    })),
  );
  let creatingChannel = $derived(
    channelFormVisible && channelFormMode === CHANNEL_FORM_MODE_CREATE,
  );
  let channelPanelBusy = $derived(
    channelBusy ||
      channelPanelState.loading ||
      channelActionChannelId.length > 0,
  );

  onMount(() => {
    void loadChannelsPanel();
  });

  $effect(() => {
    const token = channelsRefreshToken;
    if (lastChannelsRefreshToken === null) {
      lastChannelsRefreshToken = token;
      return;
    }
    if (token === lastChannelsRefreshToken) {
      return;
    }
    lastChannelsRefreshToken = token;
    pendingExternalReload = true;
  });

  // An open row's edit form does not hold outside changes back; only a new
  // Channel form and unsaved edits do.
  $effect(() => {
    if (
      !pendingExternalReload ||
      creatingChannel ||
      channelPanelBusy ||
      autosave.participant.hasChanges()
    ) {
      return;
    }
    pendingExternalReload = false;
    void loadChannelsPanel();
  });

  function clearChannelFeedback() {
    channelFormError = '';
    onError('');
  }

  function startCreateChannel() {
    return autosaveContext.requestTransition(() => startCreateChannelNow());
  }
  function startCreateChannelNow() {
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function startEditChannel(channel) {
    return autosaveContext.requestTransition(() =>
      startEditChannelNow(channel),
    );
  }
  function startEditChannelNow(channel) {
    channelFormMode = CHANNEL_FORM_MODE_EDIT;
    channelFormValues = createChannelFormValues(channel);
    channelBaseline = JSON.stringify(channelFormValues);
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function cancelChannelForm() {
    return autosaveContext.requestTransition(() => cancelChannelFormNow());
  }
  function cancelChannelFormNow() {
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = false;
    clearChannelFeedback();
  }

  // An action outside the form saved the edited Channel: fields the user has
  // not touched adopt the saved values, so the next autosave cannot send the
  // old ones back.
  function rebaseChannelForm(channel) {
    if (!isEditingChannel(channel)) return;
    const baseline = JSON.parse(channelBaseline);
    const saved = createChannelFormValues(channel);
    const next = { ...channelFormValues };
    for (const [field, value] of Object.entries(saved)) {
      if (next[field] === baseline[field]) next[field] = value;
    }
    channelFormValues = next;
    channelBaseline = JSON.stringify(saved);
  }

  function setChannelFormField(fieldName, value) {
    channelFormValues = {
      ...channelFormValues,
      [fieldName]: value,
    };
    if (fieldName === 'platform') {
      channelFormValues.app_token_env_var = '';
      channelFormValues.server_url = '';
      if (value === 'whatsapp') {
        channelFormValues.token_env_var = '';
        channelFormValues.allowed_chat_ids = 'self';
      } else if (channelFormValues.allowed_chat_ids === 'self') {
        channelFormValues.allowed_chat_ids = '';
      }
    }
    clearChannelFeedback();
  }

  function channelDmScopeLabel(dmScope) {
    switch (dmScope) {
      case 'main':
        return t('settings.channels.dm_scope.main');
      case 'per_peer':
        return t('settings.channels.dm_scope.per_peer');
      case 'per_account_channel_peer':
        return t('settings.channels.dm_scope.per_account_channel_peer');
      case 'per_conversation':
      default:
        return t('settings.channels.dm_scope.per_conversation');
    }
  }

  function channelPlatformLabel(platformId) {
    switch (platformId) {
      case 'telegram':
        return t('settings.channels.platform.telegram');
      case 'discord':
        return t('settings.channels.platform.discord');
      case 'slack':
        return t('settings.channels.platform.slack');
      case 'mattermost':
        return t('settings.channels.platform.mattermost');
      case 'whatsapp':
        return t('settings.channels.platform.whatsapp');
      default:
        return platformId;
    }
  }

  function channelAgentName(agentId) {
    return channelAgents.find((agent) => agent.id === agentId)?.name ?? agentId;
  }

  // The one status chip in a Channel row. A disabled Channel shows none (its
  // switch already says so); an enabled one shows whether it actually runs,
  // since a saved and enabled Channel is not necessarily connected.
  function channelStatusChip(channel) {
    if (channel.failure_reason) {
      return { label: t('settings.channels.failed'), variant: 'error' };
    }
    if (!channel.enabled) {
      return null;
    }
    if (channel.running === true) {
      return { label: t('settings.channels.running'), variant: 'success' };
    }
    if (channel.running === false) {
      return { label: t('settings.channels.stopped'), variant: 'warn' };
    }
    return { label: t('common.unknown'), variant: 'neutral' };
  }

  // Expanding a Channel row opens its settings for editing; collapsing it
  // closes the form. Both go through the autosave transition, so pending
  // edits are flushed first.
  function isEditingChannel(channel) {
    return (
      channelFormVisible &&
      channelFormMode === CHANNEL_FORM_MODE_EDIT &&
      channelFormValues.id === channel.id
    );
  }

  function toggleChannelDetails(channel) {
    return isEditingChannel(channel)
      ? cancelChannelForm()
      : startEditChannel(channel);
  }

  // A stray click on a row must not discard a new Channel form; the chevron
  // still switches explicitly.
  function handleChannelHeadClick(event, channel, rowBusy) {
    if (
      rowBusy ||
      creatingChannel ||
      event.target.closest('button, a, input, select, textarea') ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    toggleChannelDetails(channel);
  }

  async function loadChannelsPanel() {
    channelPanelState = {
      ...channelPanelState,
      loading: true,
      error: null,
    };

    try {
      const [agentsResult, channelsResult] = await Promise.all([
        listAgents(),
        listChannels(),
      ]);
      channelAgents = getAgentItems(agentsResult);

      const nextState = applyChannelPanelList(
        channelPanelState,
        channelsResult,
      );
      const statusResults = await Promise.all(
        nextState.channels.map(async (channel) => {
          try {
            const status = await getChannelStatus(channel.id);
            let access = null;
            try {
              access = await getChannelAccess(channel.id);
            } catch {
              // Channel status remains useful when access state cannot be loaded.
            }
            return { ...status, access };
          } catch {
            return {
              id: channel.id,
              enabled: channel.enabled,
              running: channel.running,
            };
          }
        }),
      );

      channelPanelState = {
        ...nextState,
        channels: mergeChannelStatuses(nextState.channels, statusResults),
        loading: false,
        error: null,
      };
      // A Channel removed elsewhere takes its open edit form with it.
      if (
        channelFormVisible &&
        channelFormMode === CHANNEL_FORM_MODE_EDIT &&
        !channelPanelState.channels.some(
          (channel) => channel.id === channelFormValues.id,
        )
      ) {
        cancelChannelFormNow();
      }
    } catch (error) {
      channelPanelState = {
        ...channelPanelState,
        loading: false,
        error: `${t('settings.loadError')} ${error.message}`,
      };
    }
  }

  async function persistChannelForm(reason) {
    if (channelBusy) return false;

    if (!channelFormValues.agent_id) {
      clearChannelFeedback();
      channelFormError = t('settings.channels.agent.required');
      return false;
    }

    const creating = channelFormMode === CHANNEL_FORM_MODE_CREATE;
    const submitted = JSON.stringify(channelFormValues);
    if (!creating && submitted === channelBaseline) {
      if (reason === 'manual')
        onToast({
          title: t('common.alreadySaved'),
          variant: 'success',
        });
      return true;
    }
    channelBusy = true;
    clearChannelFeedback();

    try {
      if (channelFormMode === CHANNEL_FORM_MODE_CREATE) {
        await createChannel(buildChannelCreatePayload(channelFormValues));
        onToast({
          title: t('settings.channels.createSuccess'),
          variant: 'success',
        });
      } else {
        await updateChannel(buildChannelUpdatePayload(channelFormValues));
        if (reason === 'manual')
          onToast({
            title: t('settings.channels.updateSuccess'),
            variant: 'success',
          });
      }

      if (creating) {
        channelFormVisible = false;
        channelFormMode = CHANNEL_FORM_MODE_CREATE;
        channelFormValues = createChannelFormValues();
      } else {
        channelBaseline = submitted;
      }
      await loadChannelsPanel();
      return true;
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
      return false;
    } finally {
      channelBusy = false;
    }
  }

  async function toggleChannelEnabled(channel) {
    await runChannelAction(channel.id, async () => {
      if (channel.enabled) {
        await disableChannel(channel.id);
        onToast({
          title: t('settings.channels.disableSuccess'),
          variant: 'success',
        });
        return;
      }

      await enableChannel(channel.id);
      onToast({
        title: t('settings.channels.enableSuccess'),
        variant: 'success',
      });
    });
  }

  async function allowDeniedChat(channel, chatId) {
    await runChannelAction(channel.id, async () => {
      const allowedChatIds = Array.isArray(channel.allowed_chat_ids)
        ? channel.allowed_chat_ids.map((value) => String(value))
        : [];
      if (!allowedChatIds.includes(chatId)) {
        allowedChatIds.push(chatId);
      }

      await updateChannel({
        id: channel.id,
        allowed_chat_ids: allowedChatIds,
      });
      const draftIds = channelFormValues.allowed_chat_ids;
      rebaseChannelForm({ ...channel, allowed_chat_ids: allowedChatIds });
      // A list the user is still editing keeps their text and gains the chat.
      if (
        isEditingChannel(channel) &&
        channelFormValues.allowed_chat_ids === draftIds &&
        !draftIds.split(/[\s,]+/).includes(chatId)
      ) {
        channelFormValues = {
          ...channelFormValues,
          allowed_chat_ids: draftIds.trim()
            ? `${draftIds.trim()}, ${chatId}`
            : chatId,
        };
      }
      onToast({
        title: t('settings.channels.denied.allowSuccess'),
        variant: 'success',
      });
    });
  }

  async function setOwnIdentity(channel, participant) {
    await runChannelAction(channel.id, async () => {
      await setChannelIdentity(channel.id, participant.user_id);
      onToast({
        title: t('settings.channels.access.identitySuccess'),
        variant: 'success',
      });
    });
  }

  async function toggleParticipantRole(channel, group, participant) {
    await runChannelAction(channel.id, async () => {
      if (participant.role === 'admin') {
        await revokeChannelAdmin(
          channel.id,
          group.access_scope_id,
          participant.user_id,
        );
      } else {
        await grantChannelAdmin(
          channel.id,
          group.access_scope_id,
          participant.user_id,
        );
      }
      onToast({
        title: t('settings.channels.access.roleSuccess'),
        variant: 'success',
      });
    });
  }

  function deniedChatLabel(entry) {
    const kindLabel =
      entry.kind === 'group'
        ? t('settings.channels.denied.group')
        : t('settings.channels.denied.direct');
    const namePart = entry.display_name ? `${entry.display_name} · ` : '';
    return `${namePart}${kindLabel} · ID ${entry.chat_id}`;
  }

  function deleteChannel(channel) {
    deleteConfirmChannel = channel;
  }

  function cancelDeleteChannel() {
    deleteConfirmChannel = null;
  }

  async function confirmDeleteChannel() {
    const channel = deleteConfirmChannel;
    deleteConfirmChannel = null;
    if (!channel) {
      return;
    }

    await runChannelAction(channel.id, async () => {
      await deleteChannelRequest(channel.id);
      // The edits of a deleted Channel have nowhere to go.
      if (isEditingChannel(channel)) cancelChannelFormNow();
      onToast({
        title: t('settings.channels.deleteSuccess'),
        variant: 'success',
      });
    });
  }

  async function runChannelAction(channelId, action) {
    if (channelActionChannelId.length > 0) {
      return;
    }

    channelActionChannelId = channelId;
    clearChannelFeedback();

    try {
      await action();
      await loadChannelsPanel();
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      channelActionChannelId = '';
    }
  }
</script>

{#snippet addChannelButton()}
  <Button
    variant="secondary"
    disabled={channelPanelBusy}
    onClick={startCreateChannel}
  >
    {t('settings.channels.add')}
  </Button>
{/snippet}

<!-- The Channel settings as label/control rows. Creating shows them in their
     own group above the list; editing shows them in the expanded row. The
     order runs from who talks to which Agent to the technical connection. -->
{#snippet channelSettingsRows()}
  {@const creating = channelFormMode === CHANNEL_FORM_MODE_CREATE}
  {#if channelFormError}
    <Banner variant="error">{channelFormError}</Banner>
  {/if}

  <div class="s-group__rows s-channel-settings">
    {#if creating}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="channel-id-input">
            {t('settings.channels.id')}
          </label>
          <div class="s-row-desc">{t('settings.channels.idHelp')}</div>
        </div>
        <div class="s-row-control">
          <TextField
            id="channel-id-input"
            value={channelFormValues.id}
            required
            disabled={channelBusy}
            ariaLabel={t('settings.channels.id')}
            onInput={(next) => setChannelFormField('id', next)}
          />
        </div>
      </div>
    {/if}

    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-platform-select">
          {t('settings.channels.platform')}
          {#if !creating}
            <InfoHint text={t('settings.channels.platform.help')} />
          {:else if channelFormValues.platform === 'whatsapp'}
            <InfoHint
              text={t('settings.channels.whatsapp.help')}
              ariaLabel={t('settings.channels.whatsapp.helpAria')}
            />
          {/if}
        </label>
        {#if creating && channelFormValues.platform === 'whatsapp'}
          <div class="s-row-desc">{t('settings.channels.whatsapp.risk')}</div>
        {/if}
      </div>
      <div class="s-row-control">
        <Dropdown
          id="channel-platform-select"
          value={channelFormValues.platform}
          options={channelPlatformOptions}
          ariaLabel={t('settings.channels.platform')}
          disabled={channelBusy && creating}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('platform', value)}
        />
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-agent-select">
          {t('settings.channels.agent')}
        </label>
      </div>
      <div class="s-row-control">
        <Dropdown
          id="channel-agent-select"
          value={channelFormValues.agent_id}
          options={channelAgentOptions}
          placeholder={channelAgents.length > 0
            ? t('settings.channels.agent.placeholder')
            : t('settings.channels.agent.none')}
          ariaLabel={t('settings.channels.agent')}
          disabled={channelAgents.length === 0}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('agent_id', value)}
        />
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-allowed-chat-ids-input">
          {t('settings.channels.allowed_chat_ids')}
          <InfoHint text={t('settings.channels.allowed_chat_ids.help')} />
        </label>
        <div class="s-row-desc">
          {t('settings.channels.allowed_chat_ids.description')}
        </div>
      </div>
      <div class="s-row-control">
        <TextField
          id="channel-allowed-chat-ids-input"
          code
          value={channelFormValues.allowed_chat_ids}
          disabled={channelBusy && creating}
          placeholder={t('settings.channels.allowed_chat_ids.placeholder')}
          ariaLabel={t('settings.channels.allowed_chat_ids')}
          onInput={(next) => setChannelFormField('allowed_chat_ids', next)}
        />
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-dm-scope-select">
          {t('settings.channels.dm_scope')}
          <InfoHint text={t('settings.channels.dm_scope.help')} />
        </label>
      </div>
      <div class="s-row-control">
        <Dropdown
          id="channel-dm-scope-select"
          value={channelFormValues.dm_scope}
          options={channelDmScopeOptions}
          ariaLabel={t('settings.channels.dm_scope')}
          disabled={channelBusy && creating}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('dm_scope', value)}
        />
      </div>
    </div>

    {#if channelFormValues.platform === 'mattermost'}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="channel-server-url-input">
            {t('settings.channels.server_url')}
          </label>
        </div>
        <div class="s-row-control">
          <TextField
            id="channel-server-url-input"
            code
            required
            placeholder="https://chat.example.org"
            value={channelFormValues.server_url}
            ariaLabel={t('settings.channels.server_url')}
            onInput={(next) => setChannelFormField('server_url', next)}
          />
        </div>
      </div>
    {/if}

    {#if channelFormValues.platform !== 'whatsapp'}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="channel-token-env-input">
            {t('settings.channels.token_env_var')}
            <InfoHint text={t('settings.channels.token_env_var.help')} />
          </label>
        </div>
        <div class="s-row-control">
          <TextField
            id="channel-token-env-input"
            code
            value={channelFormValues.token_env_var}
            required
            disabled={channelBusy && creating}
            ariaLabel={t('settings.channels.token_env_var')}
            onInput={(next) => setChannelFormField('token_env_var', next)}
          />
        </div>
      </div>
    {/if}

    {#if channelFormValues.platform === 'slack'}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="channel-app-token-env-input">
            {t('settings.channels.app_token_env')}
            <InfoHint text={t('settings.channels.app_token_help')} />
          </label>
        </div>
        <div class="s-row-control">
          <TextField
            id="channel-app-token-env-input"
            code
            required
            value={channelFormValues.app_token_env_var}
            ariaLabel={t('settings.channels.app_token_env')}
            onInput={(next) => setChannelFormField('app_token_env_var', next)}
          />
        </div>
      </div>
    {/if}
  </div>
{/snippet}

<!-- Reloads keep an already listed Channel on screen (an open edit form lives
     inside its row); only the first load shows the loading state. -->
{#if channelPanelState.loading && channelPanelState.channels.length === 0}
  <Banner variant="neutral">
    {t('common.loading')}
  </Banner>
{:else}
  {#if channelPanelState.error}
    <Banner variant="error" role="alert">
      <span>{channelPanelState.error}</span>
      <Button
        variant="secondary"
        disabled={channelPanelBusy}
        onClick={loadChannelsPanel}
      >
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}

  {#if !channelPanelState.error && channelPanelState.channels.length === 0}
    {#if !(channelFormVisible && channelFormMode === CHANNEL_FORM_MODE_CREATE)}
      <EmptyState
        density="compact"
        title={t('settings.channels.empty')}
        description={t('settings.channels.emptyHint')}
      >
        {#snippet actions()}
          {@render addChannelButton()}
        {/snippet}
      </EmptyState>
    {/if}
  {:else}
    <div class="s-group-toolbar s-list-toolbar">
      {#if channelPanelState.channels.length > 0}
        <span class="s-group-toolbar__meta">
          {t('settings.channels.count', {
            count: channelPanelState.channels.length,
          })}
        </span>
      {/if}
      <div class="s-group-toolbar__actions s-list-toolbar__end">
        {@render addChannelButton()}
      </div>
    </div>
  {/if}
{/if}

{#if channelFormVisible && channelFormMode === CHANNEL_FORM_MODE_CREATE}
  <form class="s-channel-form s-group" onsubmit={submitChannelForm}>
    <div class="s-channel-form-body">
      <h3 class="s-channel-form-title">{t('settings.channels.add')}</h3>
      {@render channelSettingsRows()}
      <div class="s-channel-form-actions">
        <Button variant="secondary" onClick={cancelChannelForm}>
          {t('common.cancel')}
        </Button>
        <Button variant="primary" type="submit" disabled={channelBusy}>
          {channelBusy ? t('common.saving') : t('common.create')}
        </Button>
      </div>
    </div>
  </form>
{/if}

{#if channelPanelState.channels.length > 0}
  <!-- One row per Channel: name, platform and Agent, run status, the enabled
       switch and the details disclosure. Setup that still needs doing and
       blocked chats stay visible under the head; the settings, group access
       and delete action open in the details. -->
  <div class="s-group s-channel-list">
    {#each channelPanelState.channels as channel, index (channel.id)}
      {@const rowBusy = channelBusy || channelActionChannelId === channel.id}
      {@const expanded = isEditingChannel(channel)}
      {@const statusChip = channelStatusChip(channel)}
      <div class="s-channel-card s-entity">
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
        <div
          class="s-entity__head s-entity__head--toggle"
          onclick={(event) => handleChannelHeadClick(event, channel, rowBusy)}
        >
          <div class="s-row-info">
            <div class="s-row-label">{channel.id}</div>
            <div class="s-row-desc">
              {channelPlatformLabel(channel.platform)} · {channelAgentName(
                channel.agent_id,
              )}
            </div>
            {#if channel.failure_reason}
              <div class="s-row-desc s-channel-failure">
                {channel.failure_reason}
              </div>
            {/if}
          </div>

          <div class="s-entity__end">
            {#if statusChip}
              <StatusChip variant={statusChip.variant}>
                {statusChip.label}
              </StatusChip>
            {/if}
            <Toggle
              checked={channel.enabled === true}
              disabled={rowBusy}
              ariaLabel={t('settings.channels.enableAria', {
                id: channel.id,
              })}
              onChange={() => toggleChannelEnabled(channel)}
            />
            <Button
              variant="tertiary"
              icon
              class="s-disclosure-btn"
              aria-controls={`${uid}-details-${index}`}
              disabled={rowBusy}
              ariaLabel={t('settings.channels.edit', {
                id: channel.id,
              })}
              aria-expanded={expanded}
              onClick={() => toggleChannelDetails(channel)}
            >
              <span
                class="disclosure-chevron"
                class:disclosure-chevron--open={expanded}
                aria-hidden="true"
              ></span>
            </Button>
          </div>
        </div>

        {#if channel.platform === 'whatsapp' || channel.denied_chats?.length}
          <div class="s-channel-attention">
            {#if channel.platform === 'whatsapp'}
              <WhatsAppSetup
                channelId={channel.id}
                onChanged={loadChannelsPanel}
                hideWhenConnected={!expanded}
              />
            {/if}

            {#if channel.denied_chats?.length}
              <div class="s-channel-denied">
                <div class="s-channel-part-title">
                  {t('settings.channels.denied.title')}
                  <InfoHint
                    text={t('settings.channels.denied.help')}
                    ariaLabel={t('settings.channels.denied.helpAria')}
                  />
                </div>
                {#each channel.denied_chats as deniedChat (deniedChat.chat_id)}
                  <div class="s-channel-denied-row">
                    <span class="s-channel-denied-info">
                      {deniedChatLabel(deniedChat)}
                    </span>
                    <Button
                      variant="secondary"
                      disabled={rowBusy}
                      ariaLabel={t('settings.channels.denied.allowAria', {
                        id: deniedChat.chat_id,
                      })}
                      onClick={() =>
                        allowDeniedChat(channel, deniedChat.chat_id)}
                    >
                      {t('settings.channels.denied.allow')}
                    </Button>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        {/if}

        <!-- Group access stays in the DOM while collapsed so settings search
             still finds it; the settings form mounts only while editing. -->
        <div
          id={`${uid}-details-${index}`}
          class="s-disclosure-sub s-channel-details"
          hidden={!expanded}
        >
          {#if expanded}
            <form class="s-channel-form" onsubmit={submitChannelForm}>
              {@render channelSettingsRows()}
            </form>
          {/if}

          <div class="s-channel-access">
            <div class="s-channel-access-heading">
              <div class="s-channel-part-title">
                {t('settings.channels.access.title')}
                <InfoHint
                  text={t('settings.channels.access.help')}
                  ariaLabel={t('settings.channels.access.helpAria')}
                />
              </div>
              {#if channel.access?.self_user_id}
                <div class="s-row-desc">
                  {t('settings.channels.access.identity')}:
                  {channel.access.self_user_id}
                </div>
              {/if}
            </div>

            {#if !channel.access?.groups?.length}
              <div class="s-row-desc">
                {t('settings.channels.access.empty')}
              </div>
            {:else}
              {#each channel.access.groups as group (group.access_scope_id)}
                <div class="s-channel-access-group">
                  <div class="s-channel-access-group-title">
                    {t('settings.channels.access.group')} · ID {group.access_scope_id}
                  </div>

                  {#if group.participants.length === 0}
                    <div class="s-row-desc">
                      {t('settings.channels.access.noParticipants')}
                    </div>
                  {:else}
                    {#each group.participants as participant (participant.user_id)}
                      {@const isOwnIdentity =
                        participant.user_id === channel.access.self_user_id}
                      <div class="s-channel-access-row">
                        <div class="s-channel-access-participant">
                          <span class="s-channel-access-name">
                            {participant.display_name}
                          </span>
                          <span class="s-row-desc"
                            >ID {participant.user_id}</span
                          >
                        </div>
                        <StatusChip
                          variant={participant.role === 'admin'
                            ? 'success'
                            : 'info'}
                        >
                          {participant.role === 'admin'
                            ? t('settings.channels.access.admin')
                            : t('settings.channels.access.member')}
                        </StatusChip>
                        <div class="s-entity__end">
                          <Button
                            variant="secondary"
                            disabled={rowBusy || isOwnIdentity}
                            ariaLabel={t(
                              'settings.channels.access.thisIsMeAria',
                              { name: participant.display_name },
                            )}
                            onClick={() => setOwnIdentity(channel, participant)}
                          >
                            {isOwnIdentity
                              ? t('settings.channels.access.me')
                              : t('settings.channels.access.thisIsMe')}
                          </Button>
                          <Button
                            variant="secondary"
                            disabled={rowBusy || isOwnIdentity}
                            ariaLabel={participant.role === 'admin'
                              ? t('settings.channels.access.makeMemberAria', {
                                  name: participant.display_name,
                                })
                              : t('settings.channels.access.makeAdminAria', {
                                  name: participant.display_name,
                                })}
                            onClick={() =>
                              toggleParticipantRole(
                                channel,
                                group,
                                participant,
                              )}
                          >
                            {participant.role === 'admin'
                              ? t('settings.channels.access.makeMember')
                              : t('settings.channels.access.makeAdmin')}
                          </Button>
                        </div>
                      </div>
                    {/each}
                  {/if}
                </div>
              {/each}
            {/if}
          </div>

          <div class="s-channel-details-footer">
            <Button
              variant="danger"
              disabled={rowBusy}
              ariaLabel={t('settings.channels.delete', {
                id: channel.id,
              })}
              onClick={() => deleteChannel(channel)}
            >
              {t('common.delete')}
            </Button>
            {#if expanded}
              <SaveButton
                saving={channelBusy}
                pending={autosave.participant.hasChanges()}
                onClick={() =>
                  autosave.participant.runSave('manual', { force: true })}
              />
            {/if}
          </div>
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if deleteConfirmChannel}
  <ConfirmDialog
    title={t('settings.channels.delete_confirm_title')}
    body={t('settings.channels.delete_confirm', {
      id: deleteConfirmChannel.id,
    })}
    confirmLabel={t('common.delete')}
    onConfirm={confirmDeleteChannel}
    onCancel={cancelDeleteChannel}
  />
{/if}
