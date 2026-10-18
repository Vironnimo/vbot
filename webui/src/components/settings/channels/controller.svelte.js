import { onDestroy, onMount } from 'svelte';
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
import { createDebouncedAutosave, useAutosaveContext } from '$lib/autosave.js';
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

export function createChannelsSettings(context) {
  let channelPanelState = $state(createChannelPanelState());
  let channelAgents = $state([]);
  let channelFormVisible = $state(false);
  let channelFormMode = $state(CHANNEL_FORM_MODE_CREATE);
  let channelFormValues = $state(createChannelFormValues());
  let channelBusy = $state(false);
  let channelActionChannelId = $state('');
  // Local form-validation message only (e.g. "select an agent"). Operation and
  // server errors go to `context.onError` (a sticky error toast); success feedback goes
  // to `context.onToast`.
  let channelFormError = $state('');
  // The channel awaiting delete confirmation (null = dialog closed). The delete
  // only runs once the confirm dialog resolves.
  let deleteConfirmChannel = $state(null);
  let lastChannelsRefreshToken = $state(null);
  let lastAgentsRefreshToken = null;
  // Agent list reads are numbered; a response older than the shown Agents is
  // dropped, also when a newer read failed.
  let channelAgentsRequested = 0;
  let channelAgentsShown = 0;
  let pendingExternalReload = $state(false);

  let channelBaseline = $state('');
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => channelFormValues,
    hasChanges: () =>
      channelFormVisible &&
      channelFormMode === CHANNEL_FORM_MODE_EDIT &&
      JSON.stringify(channelFormValues) !== channelBaseline,
    save: persistChannelForm,
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
      return persistChannelForm();
    return autosave.participant.runSave('manual');
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
    const token = context.channelsRefreshToken;
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

  // Agent names and the Agent picker follow the roster at once: rereading the
  // Agents changes no form value or selection.
  $effect(() => {
    const token = context.agentsRefreshToken;
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = token;
      return;
    }
    if (token === lastAgentsRefreshToken) {
      return;
    }
    lastAgentsRefreshToken = token;
    void reloadChannelAgents();
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
    context.onError('');
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

    const agentsRequest = ++channelAgentsRequested;
    try {
      const [agentsResult, channelsResult] = await Promise.all([
        listAgents(),
        listChannels(),
      ]);
      showChannelAgents(agentsRequest, agentsResult);

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

  async function reloadChannelAgents() {
    const request = ++channelAgentsRequested;
    try {
      showChannelAgents(request, await listAgents());
    } catch {
      // The shown names stay until the next roster change or panel load.
    }
  }

  function showChannelAgents(request, result) {
    if (request < channelAgentsShown) return;
    channelAgentsShown = request;
    channelAgents = getAgentItems(result);
  }

  async function persistChannelForm() {
    if (channelBusy) return false;

    if (!channelFormValues.agent_id) {
      clearChannelFeedback();
      channelFormError = t('settings.channels.agent.required');
      return false;
    }

    const creating = channelFormMode === CHANNEL_FORM_MODE_CREATE;
    const submitted = JSON.stringify(channelFormValues);
    if (!creating && submitted === channelBaseline) return true;
    channelBusy = true;
    clearChannelFeedback();

    try {
      if (channelFormMode === CHANNEL_FORM_MODE_CREATE) {
        await createChannel(buildChannelCreatePayload(channelFormValues));
        context.onToast({
          title: t('settings.channels.createSuccess'),
          variant: 'success',
        });
      } else {
        await updateChannel(buildChannelUpdatePayload(channelFormValues));
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
      context.onError(`${t('settings.saveError')} ${error.message}`);
      return false;
    } finally {
      channelBusy = false;
    }
  }

  async function toggleChannelEnabled(channel) {
    await runChannelAction(channel.id, async () => {
      if (channel.enabled) {
        await disableChannel(channel.id);
        context.onToast({
          title: t('settings.channels.disableSuccess'),
          variant: 'success',
        });
        return;
      }

      await enableChannel(channel.id);
      context.onToast({
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
      context.onToast({
        title: t('settings.channels.denied.allowSuccess'),
        variant: 'success',
      });
    });
  }

  async function setOwnIdentity(channel, participant) {
    await runChannelAction(channel.id, async () => {
      await setChannelIdentity(channel.id, participant.user_id);
      context.onToast({
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
      context.onToast({
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
      context.onToast({
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
      context.onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      channelActionChannelId = '';
    }
  }
  return {
    get channelPanelState() {
      return channelPanelState;
    },
    get channelAgents() {
      return channelAgents;
    },
    get channelFormVisible() {
      return channelFormVisible;
    },
    get channelFormMode() {
      return channelFormMode;
    },
    get channelFormValues() {
      return channelFormValues;
    },
    get channelBusy() {
      return channelBusy;
    },
    get channelActionChannelId() {
      return channelActionChannelId;
    },
    get channelFormError() {
      return channelFormError;
    },
    get deleteConfirmChannel() {
      return deleteConfirmChannel;
    },
    participant: autosave.participant,
    get channelPlatformOptions() {
      return channelPlatformOptions;
    },
    get channelDmScopeOptions() {
      return channelDmScopeOptions;
    },
    get channelAgentOptions() {
      return channelAgentOptions;
    },
    get channelPanelBusy() {
      return channelPanelBusy;
    },
    submitChannelForm,
    startCreateChannel,
    cancelChannelForm,
    setChannelFormField,
    channelPlatformLabel,
    channelAgentName,
    channelStatusChip,
    isEditingChannel,
    toggleChannelDetails,
    handleChannelHeadClick,
    loadChannelsPanel,
    toggleChannelEnabled,
    allowDeniedChat,
    setOwnIdentity,
    toggleParticipantRole,
    deniedChatLabel,
    deleteChannel,
    cancelDeleteChannel,
    confirmDeleteChannel,
  };
}
