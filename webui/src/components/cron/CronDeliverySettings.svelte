<script>
  // The Delivery section of a job's form: where each Run's final answer goes
  // and, for a chat, the Channel, chat and optional thread.
  import { t } from '$lib/i18n.js';
  import { CRON_DELIVERY_CHAT } from '$lib/cronView.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import { channelPlatformLabel } from '../settings/channels/controller.svelte.js';
  import { deliveryKindOptions } from './presentation.js';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  // `channels` are the Channels `channel.list` returned; only those of the
  // job's Agent are offered, since a job delivers only to its own Agent's chats.
  let { editor, channels = [] } = $props();

  let values = $derived(editor.formValues);
  // Channels belong to Identity Agents, so a Project Agent's job cannot
  // deliver to a chat; the server refuses it.
  let isProjectTarget = $derived(
    parseAgentAddress(values.agent_id).projectId !== null,
  );
  let isChat = $derived(values.delivery_kind === CRON_DELIVERY_CHAT);
  // A chat chosen before the target became a Project Agent stays shown, so
  // the form says why it cannot be saved.
  let deliveryKinds = $derived(
    deliveryKindOptions().filter(
      (option) =>
        option.value !== CRON_DELIVERY_CHAT || !isProjectTarget || isChat,
    ),
  );
  let agentChannels = $derived(
    channels.filter((channel) => channel.agent_id === values.agent_id),
  );
  // A stored Channel that is not offered (gone, or of another Agent) stays
  // selectable, so the form shows what is saved and the save names the cause.
  let channelOptions = $derived.by(() => {
    const options = agentChannels.map((channel) => ({
      value: channel.id,
      label: `${channel.id} · ${channelPlatformLabel(channel.platform)}`,
    }));
    const stored = values.delivery_channel_id;
    if (stored && !options.some((option) => option.value === stored)) {
      options.push({ value: stored, label: stored });
    }
    return options;
  });
  // The chosen Channel's allowed chats are offered as chat IDs.
  let chatSuggestions = $derived(
    agentChannels.find((channel) => channel.id === values.delivery_channel_id)
      ?.allowed_chat_ids ?? [],
  );

  // Choosing a chat preselects the Agent's only Channel.
  function chooseKind(kind) {
    editor.updateFormField('delivery_kind', kind);
    if (
      kind === CRON_DELIVERY_CHAT &&
      !values.delivery_channel_id &&
      agentChannels.length === 1
    ) {
      chooseChannel(agentChannels[0].id);
    }
    if (kind === CRON_DELIVERY_CHAT && values.delivery_channel_id)
      chooseChannel(values.delivery_channel_id);
  }

  function rememberPlatform() {
    const channel = agentChannels.find(
      (item) => item.id === values.delivery_channel_id,
    );
    if (channel) editor.updateFormField('delivery_platform', channel.platform);
  }

  function chooseChannel(channelId) {
    const channel = agentChannels.find((item) => item.id === channelId);
    if (
      channelId !== values.delivery_channel_id ||
      channel?.platform !== values.delivery_platform
    ) {
      editor.updateFormField('delivery_platform_target', '');
      editor.updateFormField('delivery_thread_id', '');
    }
    editor.updateFormField('delivery_channel_id', channelId);
    rememberPlatform();
  }

  function updateTarget(field, value) {
    editor.updateFormField(field, value);
    rememberPlatform();
  }
</script>

<section class="s-section" aria-labelledby="cron-section-delivery">
  <header class="s-section__head">
    <h3 class="s-section__title" id="cron-section-delivery">
      {t('cron.sections.delivery')}
    </h3>
  </header>
  <p class="s-section__desc">
    {t('cron.sections.deliverySubtitle')}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-job-delivery">
            {t('cron.form.delivery')}
          </label>
          {#if isChat && isProjectTarget}
            <div class="s-row-desc" data-testid="cron-delivery-chat-project">
              {t('cron.form.deliveryChatProject')}
            </div>
          {/if}
        </div>
        <div class="s-row-control">
          <Dropdown
            id="cron-job-delivery"
            value={values.delivery_kind}
            options={deliveryKinds}
            ariaLabel={t('cron.form.delivery')}
            disabled={editor.isCreating && editor.submittingForm}
            triggerClass="cron-dropdown"
            listClass="cron-dropdown-list"
            onValueChange={chooseKind}
          />
        </div>
      </div>

      {#if isChat && !isProjectTarget}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-delivery-channel">
              {t('cron.form.deliveryChannel')}
              <span class="cron-required" aria-hidden="true">*</span>
            </label>
            {#if channelOptions.length === 0}
              <div class="s-row-desc">
                {t('cron.form.deliveryChannelNone')}
              </div>
            {/if}
          </div>
          <div class="s-row-control">
            <Dropdown
              id="cron-job-delivery-channel"
              value={values.delivery_channel_id}
              options={channelOptions}
              placeholder={t('cron.form.deliveryChannelPlaceholder')}
              ariaLabel={t('cron.form.deliveryChannel')}
              disabled={channelOptions.length === 0 ||
                (editor.isCreating && editor.submittingForm)}
              triggerClass="cron-dropdown"
              listClass="cron-dropdown-list"
              onValueChange={chooseChannel}
            />
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <label
              class="s-row-label cron-label-with-hint"
              for="cron-job-delivery-chat"
            >
              {t('cron.form.deliveryChat')}
              <span class="cron-required" aria-hidden="true">*</span>
              <InfoHint text={t('cron.form.deliveryChatHelp')} />
            </label>
          </div>
          <div class="s-row-control">
            <TextField
              id="cron-job-delivery-chat"
              code
              list="cron-job-delivery-chats"
              value={values.delivery_platform_target}
              placeholder={t('cron.form.deliveryChatPlaceholder')}
              disabled={editor.isCreating && editor.submittingForm}
              onInput={(next) => updateTarget('delivery_platform_target', next)}
            />
            <datalist id="cron-job-delivery-chats">
              {#each chatSuggestions as chatId (chatId)}
                <option value={chatId}></option>
              {/each}
            </datalist>
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <label
              class="s-row-label cron-label-with-hint"
              for="cron-job-delivery-thread"
            >
              {t('cron.form.deliveryThread')}
              <InfoHint text={t('cron.form.deliveryThreadHelp')} />
            </label>
          </div>
          <div class="s-row-control">
            <TextField
              id="cron-job-delivery-thread"
              code
              value={values.delivery_thread_id}
              placeholder={t('cron.form.deliveryThreadPlaceholder')}
              disabled={editor.isCreating && editor.submittingForm}
              onInput={(next) => updateTarget('delivery_thread_id', next)}
            />
          </div>
        </div>
      {/if}
    </div>
  </div>
</section>
