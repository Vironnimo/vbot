<script>
  import { t } from '$lib/i18n.js';
  import { CHANNEL_FORM_MODE_CREATE } from '$lib/settingsView.js';
  import Banner from '../ui/Banner.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Dropdown from '../Dropdown.svelte';
  let {
    channelFormMode,
    channelFormError,
    channelFormValues,
    channelBusy,
    channelPlatformOptions,
    channelAgentOptions,
    channelAgents,
    channelDmScopeOptions,
    channelSmtpSecurityOptions,
    setChannelFormField,
  } = $props();
  let creating = $derived(channelFormMode === CHANNEL_FORM_MODE_CREATE);
  let email = $derived(channelFormValues.platform === 'email');
  // The email account's text rows: [field, label key, placeholder].
  const EMAIL_TEXT_ROWS = [
    ['email_address', 'address', 'me@example.com'],
    ['email_username', 'username', ''],
    ['imap_host', 'imap_host', 'imap.example.com'],
    ['imap_port', 'imap_port', '993'],
    ['smtp_host', 'smtp_host', 'smtp.example.com'],
    ['smtp_port', 'smtp_port', '465'],
    ['email_folders', 'folders', 'INBOX'],
  ];
</script>

<!-- The Channel settings as label/control rows. Creating shows them in their
     own group above the list; editing shows them in the expanded row. The
     order runs from who talks to which Agent to the technical connection. -->

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
        {email
          ? t('settings.channels.email.allowed')
          : t('settings.channels.allowed_chat_ids')}
        <InfoHint
          text={email
            ? t('settings.channels.email.allowed.help')
            : t('settings.channels.allowed_chat_ids.help')}
        />
      </label>
      <div class="s-row-desc">
        {email
          ? t('settings.channels.email.allowed.description')
          : t('settings.channels.allowed_chat_ids.description')}
      </div>
    </div>
    <div class="s-row-control">
      <TextField
        id="channel-allowed-chat-ids-input"
        code
        value={channelFormValues.allowed_chat_ids}
        disabled={channelBusy && creating}
        placeholder={email
          ? 'you@example.com'
          : t('settings.channels.allowed_chat_ids.placeholder')}
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

  {#if email}
    {#each EMAIL_TEXT_ROWS as [field, label, placeholder] (field)}
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for={`channel-${field}-input`}>
            {t(`settings.channels.email.${label}`)}
          </label>
          {#if field === 'email_username'}
            <div class="s-row-desc">
              {t('settings.channels.email.username.description')}
            </div>
          {:else if field === 'email_folders'}
            <div class="s-row-desc">
              {t('settings.channels.email.folders.description')}
            </div>
          {/if}
        </div>
        <div class="s-row-control">
          <TextField
            id={`channel-${field}-input`}
            code
            required={field === 'email_address' || field.endsWith('_host')}
            inputmode={field.endsWith('_port') ? 'numeric' : undefined}
            {placeholder}
            value={channelFormValues[field]}
            ariaLabel={t(`settings.channels.email.${label}`)}
            onInput={(next) => setChannelFormField(field, next)}
          />
        </div>
      </div>
    {/each}

    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-smtp-security-select">
          {t('settings.channels.email.smtp_security')}
        </label>
      </div>
      <div class="s-row-control">
        <Dropdown
          id="channel-smtp-security-select"
          value={channelFormValues.smtp_security}
          options={channelSmtpSecurityOptions}
          ariaLabel={t('settings.channels.email.smtp_security')}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setChannelFormField('smtp_security', value)}
        />
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <span class="s-row-label">
          {t('settings.channels.email.require_sender_authentication')}
          <InfoHint
            text={t(
              'settings.channels.email.require_sender_authentication.help',
            )}
          />
        </span>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={channelFormValues.require_sender_authentication}
          ariaLabel={t('settings.channels.email.require_sender_authentication')}
          onChange={(next) =>
            setChannelFormField('require_sender_authentication', next)}
        />
      </div>
    </div>
  {/if}

  {#if channelFormValues.platform !== 'whatsapp'}
    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for="channel-token-env-input">
          {email
            ? t('settings.channels.email.password_env_var')
            : t('settings.channels.token_env_var')}
          <InfoHint
            text={email
              ? t('settings.channels.email.password_env_var.help')
              : t('settings.channels.token_env_var.help')}
          />
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
