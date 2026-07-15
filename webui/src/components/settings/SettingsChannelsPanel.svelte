<script>
  import { onMount } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    createChannel,
    deleteChannel as deleteChannelRequest,
    disableChannel,
    enableChannel,
    getChannelStatus,
    listAgents,
    listChannels,
    updateChannel,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    CHANNEL_DM_SCOPES,
    CHANNEL_FORM_MODE_CREATE,
    CHANNEL_FORM_MODE_EDIT,
    CHANNEL_PLATFORMS,
    applyChannelPanelList,
    buildChannelCreatePayload,
    buildChannelUpdatePayload,
    channelEnabledChipVariant,
    channelRunningChipVariant,
    createChannelFormValues,
    createChannelPanelState,
    formatAllowedChatIds,
    getAgentItems,
    mergeChannelStatuses,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let { onToast = noop, onError = noop, channelsRefreshToken = 0 } = $props();

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

  let channelPlatformOptions = $derived(
    CHANNEL_PLATFORMS.map((platformId) => ({
      value: platformId,
      label:
        platformId === 'telegram'
          ? t('sessions.platform_telegram', 'Telegram')
          : platformId,
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

  $effect(() => {
    if (!pendingExternalReload || channelFormVisible || channelPanelBusy) {
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
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function startEditChannel(channel) {
    channelFormMode = CHANNEL_FORM_MODE_EDIT;
    channelFormValues = createChannelFormValues(channel);
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function cancelChannelForm() {
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = false;
    clearChannelFeedback();
  }

  function setChannelFormField(fieldName, value) {
    channelFormValues = {
      ...channelFormValues,
      [fieldName]: value,
    };
    clearChannelFeedback();
  }

  function channelDmScopeLabel(dmScope) {
    switch (dmScope) {
      case 'main':
        return t('settings.channels.dm_scope.main', 'Main');
      case 'per_peer':
        return t('settings.channels.dm_scope.per_peer', 'Per peer');
      case 'per_account_channel_peer':
        return t(
          'settings.channels.dm_scope.per_account_channel_peer',
          'Per account + channel + peer',
        );
      case 'per_conversation':
      default:
        return t(
          'settings.channels.dm_scope.per_conversation',
          'Per conversation',
        );
    }
  }

  function channelEnabledLabel(enabled) {
    return enabled
      ? t('settings.channels.enabled', 'Enabled')
      : t('settings.channels.disabled', 'Disabled');
  }

  function channelRunningLabel(running) {
    if (running === true) {
      return t('settings.channels.running', 'Running');
    }

    if (running === false) {
      return t('settings.channels.stopped', 'Stopped');
    }

    return t('common.unknown', 'Unknown');
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
            return await getChannelStatus(channel.id);
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
    } catch (error) {
      channelPanelState = {
        ...channelPanelState,
        loading: false,
        error: `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`,
      };
    }
  }

  async function submitChannelForm(event) {
    event.preventDefault();

    if (channelBusy) {
      return;
    }

    if (!channelFormValues.agent_id) {
      clearChannelFeedback();
      channelFormError = t(
        'settings.channels.agent.required',
        'Select an agent before saving.',
      );
      return;
    }

    channelBusy = true;
    clearChannelFeedback();

    try {
      if (channelFormMode === CHANNEL_FORM_MODE_CREATE) {
        await createChannel(buildChannelCreatePayload(channelFormValues));
        onToast({
          title: t('settings.channels.createSuccess', 'Channel created.'),
          variant: 'success',
        });
      } else {
        await updateChannel(buildChannelUpdatePayload(channelFormValues));
        onToast({
          title: t('settings.channels.updateSuccess', 'Channel updated.'),
          variant: 'success',
        });
      }

      channelFormVisible = false;
      channelFormMode = CHANNEL_FORM_MODE_CREATE;
      channelFormValues = createChannelFormValues();
      await loadChannelsPanel();
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      channelBusy = false;
    }
  }

  async function toggleChannelEnabled(channel) {
    await runChannelAction(channel.id, async () => {
      if (channel.enabled) {
        await disableChannel(channel.id);
        onToast({
          title: t('settings.channels.disableSuccess', 'Channel disabled.'),
          variant: 'success',
        });
        return;
      }

      await enableChannel(channel.id);
      onToast({
        title: t('settings.channels.enableSuccess', 'Channel enabled.'),
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
      onToast({
        title: t('settings.channels.denied.allowSuccess', 'Chat allowed.'),
        variant: 'success',
      });
    });
  }

  function deniedChatLabel(entry) {
    const kindLabel =
      entry.kind === 'group'
        ? t('settings.channels.denied.group', 'Group')
        : t('settings.channels.denied.direct', 'Direct');
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
      onToast({
        title: t('settings.channels.deleteSuccess', 'Channel deleted.'),
        variant: 'success',
      });
    });

    if (
      channelFormMode === CHANNEL_FORM_MODE_EDIT &&
      channelFormValues.id === channel.id
    ) {
      cancelChannelForm();
    }
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
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      channelActionChannelId = '';
    }
  }
</script>

<div class="s-row s-row--stacked s-row--channels-header">
  <div class="s-row-control">
    <div class="s-row-actions s-row-actions--channel-header">
      <Button
        variant="primary"
        disabled={channelPanelBusy}
        onClick={startCreateChannel}
      >
        {t('settings.channels.add', 'Add channel')}
      </Button>
    </div>
  </div>
</div>

{#if channelFormVisible}
  <form class="s-channel-form" onsubmit={submitChannelForm}>
    {#if channelFormError}
      <Banner variant="error" class="s-channel-form-error"
        >{channelFormError}</Banner
      >
    {/if}

    <div class="s-channel-form-header">
      <h3 class="s-channel-form-title">
        {channelFormMode === CHANNEL_FORM_MODE_CREATE
          ? t('settings.channels.add', 'Add channel')
          : t('common.edit', 'Edit')}
      </h3>
    </div>

    <div class="s-channel-form-grid">
      <FormField
        controlId="channel-id-input"
        required
        label={t('sessions.link_channel_id', 'Channel ID')}
        help={t(
          'settings.channels.idHelp',
          'A name you choose for this channel. It cannot be changed after creation.',
        )}
      >
        <TextField
          id="channel-id-input"
          value={channelFormValues.id}
          required
          disabled={channelBusy || channelFormMode === CHANNEL_FORM_MODE_EDIT}
          onInput={(next) => setChannelFormField('id', next)}
        />
      </FormField>

      <FormField
        controlId="channel-platform-select"
        label={t('settings.channels.platform', 'Platform')}
      >
        <Dropdown
          id="channel-platform-select"
          value={channelFormValues.platform}
          options={channelPlatformOptions}
          ariaLabel={t('settings.channels.platform', 'Platform')}
          disabled={channelBusy}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('platform', value)}
        />
      </FormField>

      <FormField
        controlId="channel-agent-select"
        label={t('settings.channels.agent', 'Agent')}
      >
        <Dropdown
          id="channel-agent-select"
          value={channelFormValues.agent_id}
          options={channelAgentOptions}
          placeholder={channelAgents.length > 0
            ? t('settings.channels.agent.placeholder', 'Select agent')
            : t('settings.channels.agent.none', 'No agents available')}
          ariaLabel={t('settings.channels.agent', 'Agent')}
          disabled={channelBusy || channelAgents.length === 0}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('agent_id', value)}
        />
      </FormField>

      <FormField controlId="channel-dm-scope-select">
        {#snippet labelContent()}
          {t('settings.channels.dm_scope', 'DM scope')}
          <InfoHint
            text={t(
              'settings.channels.dm_scope.help',
              'How direct messages are grouped into chat sessions:\n\nMain — all DMs share one session. Per peer — one session per person. Per conversation — one session per chat. Per account, channel & peer — one session per chat and person.\n\nGroup chats always share one session per group, regardless of this setting.',
            )}
          />
        {/snippet}
        <Dropdown
          id="channel-dm-scope-select"
          value={channelFormValues.dm_scope}
          options={channelDmScopeOptions}
          ariaLabel={t('settings.channels.dm_scope', 'DM scope')}
          disabled={channelBusy}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('dm_scope', value)}
        />
      </FormField>

      <FormField
        controlId="channel-token-env-input"
        required
        label={t('settings.channels.token_env_var', 'Token env var')}
        help={t(
          'settings.channels.token_env_var.help',
          'Name of the environment variable that holds the bot token. Set the variable itself in the .env file in the vBot data directory — only the name goes here.',
        )}
      >
        <TextField
          id="channel-token-env-input"
          value={channelFormValues.token_env_var}
          required
          disabled={channelBusy}
          onInput={(next) => setChannelFormField('token_env_var', next)}
        />
      </FormField>

      <FormField controlId="channel-allowed-chat-ids-input" full>
        {#snippet labelContent()}
          {t('settings.channels.allowed_chat_ids', 'Allowed chat IDs')}
          <InfoHint
            text={t(
              'settings.channels.allowed_chat_ids.help',
              'Comma-separated chat IDs allowed to talk to this channel. An empty list allows nobody. Messages from chats not on the list are rejected and appear on the channel card below with a one-click Allow.',
            )}
          />
        {/snippet}
        <TextField
          id="channel-allowed-chat-ids-input"
          value={channelFormValues.allowed_chat_ids}
          disabled={channelBusy}
          placeholder={t(
            'settings.channels.allowed_chat_ids.placeholder',
            '12345, -1009876543210',
          )}
          onInput={(next) => setChannelFormField('allowed_chat_ids', next)}
        />
      </FormField>
    </div>

    <div class="s-channel-form-actions">
      <Button variant="secondary" onClick={cancelChannelForm}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button variant="primary" type="submit" disabled={channelBusy}>
        {channelBusy
          ? t('common.saving', 'Saving…')
          : channelFormMode === CHANNEL_FORM_MODE_CREATE
            ? t('common.create', 'Create')
            : t('common.save', 'Save')}
      </Button>
    </div>
  </form>
{/if}

{#if channelPanelState.loading}
  <Banner variant="neutral">
    {t('common.loading', 'Loading…')}
  </Banner>
{:else if channelPanelState.error}
  <Banner variant="error" role="alert">
    <span>{channelPanelState.error}</span>
    <Button
      variant="secondary"
      disabled={channelPanelBusy}
      onClick={loadChannelsPanel}
    >
      {t('common.retry', 'Retry')}
    </Button>
  </Banner>
{:else if channelPanelState.channels.length === 0}
  <EmptyState
    density="compact"
    description={t('settings.channels.empty', 'No channels configured.')}
  />
{:else}
  <div class="s-channel-list">
    {#each channelPanelState.channels as channel (channel.id)}
      {@const rowBusy = channelBusy || channelActionChannelId === channel.id}
      <div class="s-channel-card">
        <div class="s-channel-head">
          <div class="s-row-info">
            <div class="s-row-label">{channel.id}</div>
            <div class="s-row-desc">
              {t('settings.channels.platform', 'Platform')}: {channel.platform}
              · {t('settings.channels.agent', 'Agent')}: {channel.agent_id}
            </div>
            <div class="s-row-desc">
              {t('settings.channels.dm_scope', 'DM scope')}: {channelDmScopeLabel(
                channel.dm_scope,
              )}
            </div>
            <div class="s-row-desc">
              {t('settings.channels.token_env_var', 'Token env var')}: {channel.token_env_var}
            </div>
            <div class="s-row-desc">
              {t('settings.channels.allowed_chat_ids', 'Allowed chat IDs')}: {formatAllowedChatIds(
                channel.allowed_chat_ids,
              ) || t('settings.channels.allowed_chat_ids.none', 'None')}
            </div>
          </div>

          <div class="s-channel-controls">
            <div class="s-channel-chips">
              <StatusChip variant={channelEnabledChipVariant(channel.enabled)}>
                {channelEnabledLabel(channel.enabled)}
              </StatusChip>
              <StatusChip variant={channelRunningChipVariant(channel.running)}>
                {channelRunningLabel(channel.running)}
              </StatusChip>
            </div>

            <div class="s-row-actions s-row-actions--channel">
              <Button
                variant="secondary"
                disabled={rowBusy}
                ariaLabel={t('settings.channels.edit', 'Edit channel {id}', {
                  id: channel.id,
                })}
                onClick={() => startEditChannel(channel)}
              >
                {t('common.edit', 'Edit')}
              </Button>
              <Button
                variant="secondary"
                disabled={rowBusy}
                ariaLabel={channel.enabled
                  ? t('settings.channels.disableAria', 'Disable channel {id}', {
                      id: channel.id,
                    })
                  : t('settings.channels.enableAria', 'Enable channel {id}', {
                      id: channel.id,
                    })}
                onClick={() => toggleChannelEnabled(channel)}
              >
                {channel.enabled
                  ? t('settings.channels.disable', 'Disable')
                  : t('settings.channels.enable', 'Enable')}
              </Button>
              <Button
                variant="secondary"
                disabled={rowBusy}
                ariaLabel={t(
                  'settings.channels.delete',
                  'Delete channel {id}',
                  {
                    id: channel.id,
                  },
                )}
                onClick={() => deleteChannel(channel)}
              >
                {t('common.delete', 'Delete')}
              </Button>
            </div>
          </div>
        </div>

        {#if channel.denied_chats?.length}
          <div class="s-channel-denied">
            <div class="s-channel-denied-title">
              {t(
                'settings.channels.denied.title',
                'Recent requests from chats not on the allowlist',
              )}
            </div>
            {#each channel.denied_chats as deniedChat (deniedChat.chat_id)}
              <div class="s-channel-denied-row">
                <span class="s-channel-denied-info">
                  {deniedChatLabel(deniedChat)}
                </span>
                <Button
                  variant="secondary"
                  disabled={rowBusy}
                  ariaLabel={t(
                    'settings.channels.denied.allowAria',
                    'Allow chat {id}',
                    { id: deniedChat.chat_id },
                  )}
                  onClick={() => allowDeniedChat(channel, deniedChat.chat_id)}
                >
                  {t('settings.channels.denied.allow', 'Allow')}
                </Button>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}

{#if deleteConfirmChannel}
  <ConfirmDialog
    title={t('settings.channels.delete_confirm_title', 'Delete channel')}
    body={t(
      'settings.channels.delete_confirm',
      'Delete channel "{id}" permanently? vBot stops listening on it and its configuration is removed.',
      { id: deleteConfirmChannel.id },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDeleteChannel}
    onCancel={cancelDeleteChannel}
  />
{/if}
