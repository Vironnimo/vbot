<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    CHANNEL_DM_SCOPES,
    CHANNEL_FORM_MODE_CREATE,
    CHANNEL_FORM_MODE_EDIT,
    CHANNEL_PLATFORMS,
    applyChannelPanelList,
    buildChannelCreatePayload,
    buildChannelUpdatePayload,
    channelEnabledChipClass,
    channelRunningChipClass,
    createChannelFormValues,
    createChannelPanelState,
    formatAllowedChatIds,
    getAgentItems,
    mergeChannelStatuses,
  } from '$lib/settingsView.js';

  let channelPanelState = $state(createChannelPanelState());
  let channelAgents = $state([]);
  let channelFormVisible = $state(false);
  let channelFormMode = $state(CHANNEL_FORM_MODE_CREATE);
  let channelFormValues = $state(createChannelFormValues());
  let channelBusy = $state(false);
  let channelActionChannelId = $state('');
  let channelNotice = $state('');
  let channelError = $state('');

  let channelPlatformOptions = $derived(
    CHANNEL_PLATFORMS.map((platformId) => ({
      id: platformId,
      label:
        platformId === 'telegram'
          ? t('sessions.platform_telegram', 'Telegram')
          : platformId,
    })),
  );
  let channelDmScopeOptions = $derived(
    CHANNEL_DM_SCOPES.map((scopeId) => ({
      id: scopeId,
      label: channelDmScopeLabel(scopeId),
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

  function clearChannelFeedback() {
    channelError = '';
    channelNotice = '';
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

  async function reloadChannelsPanel() {
    await loadChannelsPanel();
  }

  async function loadChannelsPanel() {
    channelPanelState = {
      ...channelPanelState,
      loading: true,
      error: null,
    };

    try {
      const [agentsResult, channelsResult] = await Promise.all([
        rpc('agent.list'),
        rpc('channel.list'),
      ]);
      channelAgents = getAgentItems(agentsResult);

      const nextState = applyChannelPanelList(
        channelPanelState,
        channelsResult,
      );
      const statusResults = await Promise.all(
        nextState.channels.map(async (channel) => {
          try {
            return await rpc('channel.status', { id: channel.id });
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

    channelBusy = true;
    clearChannelFeedback();

    try {
      if (channelFormMode === CHANNEL_FORM_MODE_CREATE) {
        await rpc(
          'channel.create',
          buildChannelCreatePayload(channelFormValues),
        );
        channelNotice = t(
          'settings.channels.createSuccess',
          'Channel created.',
        );
      } else {
        await rpc(
          'channel.update',
          buildChannelUpdatePayload(channelFormValues),
        );
        channelNotice = t(
          'settings.channels.updateSuccess',
          'Channel updated.',
        );
      }

      channelFormVisible = false;
      channelFormMode = CHANNEL_FORM_MODE_CREATE;
      channelFormValues = createChannelFormValues();
      await loadChannelsPanel();
    } catch (error) {
      channelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      channelBusy = false;
    }
  }

  async function toggleChannelEnabled(channel) {
    await runChannelAction(channel.id, async () => {
      if (channel.enabled) {
        await rpc('channel.disable', { id: channel.id });
        channelNotice = t(
          'settings.channels.disableSuccess',
          'Channel disabled.',
        );
        return;
      }

      await rpc('channel.enable', { id: channel.id });
      channelNotice = t('settings.channels.enableSuccess', 'Channel enabled.');
    });
  }

  async function deleteChannel(channel) {
    const confirmed = confirm(
      t('settings.channels.delete_confirm', 'Delete channel {id}?', {
        id: channel.id,
      }),
    );
    if (!confirmed) {
      return;
    }

    await runChannelAction(channel.id, async () => {
      await rpc('channel.delete', { id: channel.id });
      channelNotice = t('settings.channels.deleteSuccess', 'Channel deleted.');
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
      channelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      channelActionChannelId = '';
    }
  }
</script>

<div class="s-row s-row--stacked s-row--channels-header">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.channels.title', 'Channels')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.channels.subtitle',
        'Manage channel routing and runtime status.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <div class="s-row-actions s-row-actions--channel-header">
      <button
        class="btn-outline"
        type="button"
        disabled={channelPanelBusy}
        onclick={reloadChannelsPanel}
      >
        {t('common.refresh', 'Refresh')}
      </button>
      <button
        class="btn-primary"
        type="button"
        disabled={channelPanelBusy}
        onclick={startCreateChannel}
      >
        {t('settings.channels.add', 'Add channel')}
      </button>
    </div>
  </div>
</div>

{#if channelError}
  <div class="s-feedback s-feedback--error">{channelError}</div>
{:else if channelNotice}
  <div class="s-feedback s-feedback--success">{channelNotice}</div>
{/if}

{#if channelFormVisible}
  <form class="s-channel-form" onsubmit={submitChannelForm}>
    <div class="s-channel-form-header">
      <h3 class="s-channel-form-title">
        {channelFormMode === CHANNEL_FORM_MODE_CREATE
          ? t('settings.channels.add', 'Add channel')
          : t('common.edit', 'Edit')}
      </h3>
    </div>

    <div class="s-channel-form-grid">
      <label class="s-field" for="channel-id-input">
        <span class="s-field-label">
          {t('sessions.link_channel_id', 'Channel ID')}
        </span>
        <input
          id="channel-id-input"
          class="s-input"
          type="text"
          value={channelFormValues.id}
          required
          disabled={channelBusy || channelFormMode === CHANNEL_FORM_MODE_EDIT}
          oninput={(event) =>
            setChannelFormField('id', event.currentTarget.value)}
        />
      </label>

      <label class="s-field" for="channel-platform-select">
        <span class="s-field-label">
          {t('settings.channels.platform', 'Platform')}
        </span>
        <select
          id="channel-platform-select"
          class="s-select"
          value={channelFormValues.platform}
          disabled={channelBusy}
          onchange={(event) =>
            setChannelFormField('platform', event.currentTarget.value)}
        >
          {#each channelPlatformOptions as option (option.id)}
            <option value={option.id}>{option.label}</option>
          {/each}
        </select>
      </label>

      <label class="s-field" for="channel-agent-select">
        <span class="s-field-label">
          {t('settings.channels.agent', 'Agent')}
        </span>
        <select
          id="channel-agent-select"
          class="s-select"
          value={channelFormValues.agent_id}
          required
          disabled={channelBusy || channelAgents.length === 0}
          onchange={(event) =>
            setChannelFormField('agent_id', event.currentTarget.value)}
        >
          <option value="" disabled>
            {channelAgents.length > 0
              ? t('settings.channels.agent.placeholder', 'Select agent')
              : t('settings.channels.agent.none', 'No agents available')}
          </option>
          {#each channelAgents as agent (agent.id)}
            <option value={agent.id}>{agent.name}</option>
          {/each}
        </select>
      </label>

      <label class="s-field" for="channel-dm-scope-select">
        <span class="s-field-label">
          {t('settings.channels.dm_scope', 'DM scope')}
        </span>
        <select
          id="channel-dm-scope-select"
          class="s-select"
          value={channelFormValues.dm_scope}
          disabled={channelBusy}
          onchange={(event) =>
            setChannelFormField('dm_scope', event.currentTarget.value)}
        >
          {#each channelDmScopeOptions as option (option.id)}
            <option value={option.id}>{option.label}</option>
          {/each}
        </select>
      </label>

      <label class="s-field" for="channel-token-env-input">
        <span class="s-field-label">
          {t('settings.channels.token_env_var', 'Token env var')}
        </span>
        <input
          id="channel-token-env-input"
          class="s-input"
          type="text"
          value={channelFormValues.token_env_var}
          required
          disabled={channelBusy}
          oninput={(event) =>
            setChannelFormField('token_env_var', event.currentTarget.value)}
        />
      </label>

      <label class="s-field s-field--full" for="channel-allowed-chat-ids-input">
        <span class="s-field-label">
          {t('settings.channels.allowed_chat_ids', 'Allowed chat IDs')}
        </span>
        <input
          id="channel-allowed-chat-ids-input"
          class="s-input"
          type="text"
          value={channelFormValues.allowed_chat_ids}
          disabled={channelBusy}
          placeholder={t(
            'settings.channels.allowed_chat_ids.placeholder',
            '12345, -1009876543210',
          )}
          oninput={(event) =>
            setChannelFormField('allowed_chat_ids', event.currentTarget.value)}
        />
      </label>
    </div>

    <div class="s-channel-form-actions">
      <button class="btn-outline" type="button" onclick={cancelChannelForm}>
        {t('common.cancel', 'Cancel')}
      </button>
      <button class="btn-primary" type="submit" disabled={channelBusy}>
        {channelBusy
          ? t('common.saving', 'Saving…')
          : channelFormMode === CHANNEL_FORM_MODE_CREATE
            ? t('common.create', 'Create')
            : t('common.save', 'Save')}
      </button>
    </div>
  </form>
{/if}

{#if channelPanelState.loading}
  <div class="s-feedback s-feedback--neutral">
    {t('common.loading', 'Loading…')}
  </div>
{:else if channelPanelState.error}
  <div class="s-feedback s-feedback--error">
    {channelPanelState.error}
  </div>
{:else if channelPanelState.channels.length === 0}
  <div class="s-feedback s-feedback--neutral">
    {t('settings.channels.empty', 'No channels configured.')}
  </div>
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
              <span class={`chip ${channelEnabledChipClass(channel.enabled)}`}>
                {channelEnabledLabel(channel.enabled)}
              </span>
              <span class={`chip ${channelRunningChipClass(channel.running)}`}>
                {channelRunningLabel(channel.running)}
              </span>
            </div>

            <div class="s-row-actions s-row-actions--channel">
              <button
                class="btn-outline"
                type="button"
                disabled={rowBusy}
                aria-label={t('settings.channels.edit', 'Edit channel {id}', {
                  id: channel.id,
                })}
                onclick={() => startEditChannel(channel)}
              >
                {t('common.edit', 'Edit')}
              </button>
              <button
                class="btn-outline"
                type="button"
                disabled={rowBusy}
                aria-label={channel.enabled
                  ? t('settings.channels.disableAria', 'Disable channel {id}', {
                      id: channel.id,
                    })
                  : t('settings.channels.enableAria', 'Enable channel {id}', {
                      id: channel.id,
                    })}
                onclick={() => toggleChannelEnabled(channel)}
              >
                {channel.enabled
                  ? t('settings.channels.disable', 'Disable')
                  : t('settings.channels.enable', 'Enable')}
              </button>
              <button
                class="btn-outline"
                type="button"
                disabled={rowBusy}
                aria-label={t(
                  'settings.channels.delete',
                  'Delete channel {id}',
                  {
                    id: channel.id,
                  },
                )}
                onclick={() => deleteChannel(channel)}
              >
                {t('common.delete', 'Delete')}
              </button>
            </div>
          </div>
        </div>
      </div>
    {/each}
  </div>
{/if}
