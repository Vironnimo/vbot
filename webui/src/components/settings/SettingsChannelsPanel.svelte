<script>
  import ChannelGroupAccess from './ChannelGroupAccess.svelte';
  import { CHANNEL_FORM_MODE_CREATE } from '$lib/settingsView.js';
  import { createChannelsSettings } from './channels/controller.svelte.js';
  import ChannelSettingsForm from './ChannelSettingsForm.svelte';

  import WhatsAppSetup from './WhatsAppSetup.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import StatusChip from '../ui/StatusChip.svelte';

  import Toggle from '../ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    active = true,
    onToast = noop,
    onError = noop,
    channelsRefreshToken = 0,
    agentsRefreshToken = 0,
  } = $props();
  const uid = $props.id();

  const channels = createChannelsSettings({
    get active() {
      return active;
    },
    get onToast() {
      return onToast;
    },
    get onError() {
      return onError;
    },
    get channelsRefreshToken() {
      return channelsRefreshToken;
    },
    get agentsRefreshToken() {
      return agentsRefreshToken;
    },
  });
</script>

{#snippet addChannelButton()}
  <Button
    variant="secondary"
    disabled={channels.channelPanelBusy}
    onClick={channels.startCreateChannel}
  >
    {t('settings.channels.add')}
  </Button>
{/snippet}

<!-- Reloads keep an already listed Channel on screen (an open edit form lives
     inside its row); only the first load shows the loading state. -->
{#if channels.channelPanelState.loading && channels.channelPanelState.channels.length === 0}
  <Banner variant="neutral">
    {t('common.loading')}
  </Banner>
{:else}
  {#if channels.channelPanelState.error}
    <Banner variant="error" role="alert">
      <span>{channels.channelPanelState.error}</span>
      <Button
        variant="secondary"
        disabled={channels.channelPanelBusy}
        onClick={channels.loadChannelsPanel}
      >
        {t('common.retry')}
      </Button>
    </Banner>
  {/if}

  {#if !channels.channelPanelState.error && channels.channelPanelState.channels.length === 0}
    {#if !(channels.channelFormVisible && channels.channelFormMode === CHANNEL_FORM_MODE_CREATE)}
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
      {#if channels.channelPanelState.channels.length > 0}
        <span class="s-group-toolbar__meta">
          {t('settings.channels.count', {
            count: channels.channelPanelState.channels.length,
          })}
        </span>
      {/if}
      <div class="s-group-toolbar__actions s-list-toolbar__end">
        {@render addChannelButton()}
      </div>
    </div>
  {/if}
{/if}

{#if channels.channelFormVisible && channels.channelFormMode === CHANNEL_FORM_MODE_CREATE}
  <form class="s-channel-form s-group" onsubmit={channels.submitChannelForm}>
    <div class="s-channel-form-body">
      <h3 class="s-channel-form-title">{t('settings.channels.add')}</h3>
      <ChannelSettingsForm
        channelFormMode={channels.channelFormMode}
        channelFormError={channels.channelFormError}
        channelFormValues={channels.channelFormValues}
        channelBusy={channels.channelBusy}
        channelPlatformOptions={channels.channelPlatformOptions}
        channelAgentOptions={channels.channelAgentOptions}
        channelAgents={channels.channelAgents}
        channelDmScopeOptions={channels.channelDmScopeOptions}
        setChannelFormField={channels.setChannelFormField}
      />
      <div class="s-channel-form-actions">
        <Button variant="secondary" onClick={channels.cancelChannelForm}>
          {t('common.cancel')}
        </Button>
        <Button variant="primary" type="submit" disabled={channels.channelBusy}>
          {channels.channelBusy ? t('common.saving') : t('common.create')}
        </Button>
      </div>
    </div>
  </form>
{/if}

{#if channels.channelPanelState.channels.length > 0}
  <!-- One row per Channel: name, platform and Agent, run status, the enabled
       switch and the details disclosure. Setup that still needs doing and
       blocked chats stay visible under the head; the settings, group access
       and delete action open in the details. -->
  <div class="s-group s-channel-list">
    {#each channels.channelPanelState.channels as channel, index (channel.id)}
      {@const rowBusy =
        channels.channelBusy || channels.channelActionChannelId === channel.id}
      {@const expanded = channels.isEditingChannel(channel)}
      {@const statusChip = channels.channelStatusChip(channel)}
      <div class="s-channel-card s-entity">
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
        <div
          class="s-entity__head s-entity__head--toggle"
          onclick={(event) =>
            channels.handleChannelHeadClick(event, channel, rowBusy)}
        >
          <div class="s-row-info">
            <div class="s-row-label">{channel.id}</div>
            <div class="s-row-desc">
              {channels.channelPlatformLabel(channel.platform)} · {channels.channelAgentName(
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
              onChange={() => channels.toggleChannelEnabled(channel)}
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
              onClick={() => channels.toggleChannelDetails(channel)}
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
                {active}
                channelId={channel.id}
                onChanged={channels.loadChannelsPanel}
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
                      {channels.deniedChatLabel(deniedChat)}
                    </span>
                    <Button
                      variant="secondary"
                      disabled={rowBusy}
                      ariaLabel={t('settings.channels.denied.allowAria', {
                        id: deniedChat.chat_id,
                      })}
                      onClick={() =>
                        channels.allowDeniedChat(channel, deniedChat.chat_id)}
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
            <form class="s-channel-form" onsubmit={channels.submitChannelForm}>
              <ChannelSettingsForm
                channelFormMode={channels.channelFormMode}
                channelFormError={channels.channelFormError}
                channelFormValues={channels.channelFormValues}
                channelBusy={channels.channelBusy}
                channelPlatformOptions={channels.channelPlatformOptions}
                channelAgentOptions={channels.channelAgentOptions}
                channelAgents={channels.channelAgents}
                channelDmScopeOptions={channels.channelDmScopeOptions}
                setChannelFormField={channels.setChannelFormField}
              />
            </form>
          {/if}

          <ChannelGroupAccess
            {channel}
            {rowBusy}
            setOwnIdentity={channels.setOwnIdentity}
            toggleParticipantRole={channels.toggleParticipantRole}
          />

          <div class="s-channel-details-footer">
            <Button
              variant="danger"
              disabled={rowBusy}
              ariaLabel={t('settings.channels.delete', {
                id: channel.id,
              })}
              onClick={() => channels.deleteChannel(channel)}
            >
              {t('common.delete')}
            </Button>
            {#if expanded}
              <SaveStatus
                saving={channels.channelBusy}
                pending={channels.participant.hasChanges()}
                onClick={() => channels.participant.runSave('manual')}
              />
            {/if}
          </div>
        </div>
      </div>
    {/each}

    <!-- A Channel's settings form mounts only while it is edited. These
         declarations let Settings search find its fields meanwhile; a field
         the open form renders takes their place in the results. -->
    <span
      hidden
      data-search-label={t('settings.channels.platform')}
      data-search-terms={t('settings.channels.platform.help')}
    ></span>
    <span hidden data-search-label={t('settings.channels.agent')}></span>
    <span
      hidden
      data-search-label={t('settings.channels.allowed_chat_ids')}
      data-search-terms={t('settings.channels.allowed_chat_ids.help')}
    ></span>
    <span
      hidden
      data-search-label={t('settings.channels.dm_scope')}
      data-search-terms={t('settings.channels.dm_scope.help')}
    ></span>
    <span hidden data-search-label={t('settings.channels.server_url')}></span>
    <span
      hidden
      data-search-label={t('settings.channels.token_env_var')}
      data-search-terms={t('settings.channels.token_env_var.help')}
    ></span>
    <span
      hidden
      data-search-label={t('settings.channels.app_token_env')}
      data-search-terms={t('settings.channels.app_token_help')}
    ></span>
  </div>
{/if}

{#if channels.deleteConfirmChannel}
  <ConfirmDialog
    title={t('settings.channels.delete_confirm_title')}
    body={t('settings.channels.delete_confirm', {
      id: channels.deleteConfirmChannel.id,
    })}
    confirmLabel={t('common.delete')}
    onConfirm={channels.confirmDeleteChannel}
    onCancel={channels.cancelDeleteChannel}
  />
{/if}
