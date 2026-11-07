<script>
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    getConfiguredConnections,
    getConnectionAccounts,
    isConnectionEnabled,
    isKeylessConnection,
    isExternalConnection,
    isSharedOpenCodeConnection,
    describeSharedOpenCodeKey,
    accountDisplayName,
    isAccountUsable,
    accountUnusableReason,
    accountSourceTooltip,
    describeAccountSource,
    describeExternalStatus,
    isOAuthDeviceFlowConnection,
    isOAuthAccount,
    isOAuthConnection,
    isProcessEnvAccount,
    connectionSupportsAddAccount,
  } from '$lib/settingsView.js';
  import Button from '../../ui/Button.svelte';
  import InfoHint from '../../ui/InfoHint.svelte';
  import StatusChip from '../../ui/StatusChip.svelte';
  import ClaudeExecutableSetting from './ClaudeExecutableSetting.svelte';
  let {
    provider,
    summaryStatus,
    connectionToggleBusy,
    connectionStatus,
    statusChip,
    setConnectionEnabled,
    disconnectOAuthAccount,
    openReplaceKeyModal,
    removeApiKey,
    openAddAccountModal,
  } = $props();
  // A connection shows a visible line only for a consequence that is not
  // obvious from its label and actions; longer explanations go into its "?".
  function connectionNote(connection) {
    if (!isConnectionEnabled(connection)) {
      return t('settings.providers.disabledDescription');
    }
    if (isSharedOpenCodeConnection(connection)) {
      return t('settings.providers.opencode.sharedKeyShort');
    }
    return '';
  }

  function connectionHelp(connection) {
    if (isSharedOpenCodeConnection(connection)) {
      return describeSharedOpenCodeKey();
    }
    if (
      isOAuthConnection(connection) &&
      !isOAuthDeviceFlowConnection(connection)
    ) {
      return t('settings.providers.oauthTokenHelp');
    }
    return '';
  }
</script>

<div class="s-provider-connections">
  {#each getConfiguredConnections(provider) as connection (connection.id)}
    {@const status = connectionStatus(connection)}
    {@const note = connectionNote(connection)}
    {@const help = connectionHelp(connection)}
    <div class="s-provider-connection-row">
      <div class="s-provider-connection-head">
        <div class="s-row-info">
          <div class="s-provider-connection-label">
            {connection.label ?? connection.id}
            {#if help}
              <InfoHint text={help} />
            {/if}
          </div>
          {#if note}
            <div class="s-row-desc">{note}</div>
          {/if}
        </div>

        <div class="s-entity__end">
          {#if status !== summaryStatus}
            <span class="tooltip-anchor" use:tooltip={statusChip(status).hint}>
              <StatusChip variant={statusChip(status).variant}>
                {statusChip(status).label}
              </StatusChip>
            </span>
          {/if}
          {#if !isConnectionEnabled(connection)}
            <Button
              variant="secondary"
              disabled={connectionToggleBusy}
              ariaLabel={t('settings.providers.enableAria', {
                id: connection.id,
              })}
              onClick={() => setConnectionEnabled(provider, connection, true)}
            >
              {t('settings.providers.enable')}
            </Button>
          {:else}
            <Button
              variant="secondary"
              disabled={connectionToggleBusy}
              ariaLabel={t('settings.providers.disableAria', {
                id: connection.id,
              })}
              onClick={() => setConnectionEnabled(provider, connection, false)}
            >
              {t('settings.providers.disable')}
            </Button>
          {/if}
        </div>
      </div>

      {#if isExternalConnection(connection)}
        <p class="s-row-desc">
          {t('settings.providers.external.setupHelp', {
            provider: provider.name,
          })}
        </p>
        {#if connection.setup_command}<code>{connection.setup_command}</code
          >{/if}
        <p class="s-row-desc">
          {describeExternalStatus(provider, connection.external_auth?.status)}
        </p>
        {#if provider.id === 'claude-agent'}
          <ClaudeExecutableSetting {provider} />
        {/if}
        {#if connection.setup_url}
          <a href={connection.setup_url} target="_blank" rel="noreferrer"
            >{t('settings.providers.external.installGuide')}</a
          >
        {/if}
        {#if isConnectionEnabled(connection)}
          <Button
            variant="tertiary"
            disabled={connectionToggleBusy}
            onClick={() => setConnectionEnabled(provider, connection, true)}
          >
            {t('settings.providers.external.checkLogin')}
          </Button>
        {/if}
      {/if}
      {#if isConnectionEnabled(connection) && !isKeylessConnection(connection) && !isExternalConnection(connection)}
        {#if getConnectionAccounts(connection).length > 0}
          <ul class="s-connection-accounts">
            {#each getConnectionAccounts(connection) as account (account.id)}
              <li class="s-connection-account-row">
                <span class="s-connection-account-id">
                  {accountDisplayName(account)}
                </span>
                {#if !isAccountUsable(account)}
                  <span
                    class="tooltip-anchor"
                    use:tooltip={accountUnusableReason(account)}
                  >
                    <StatusChip variant="warn">
                      {t('settings.providers.accounts.notUsable')}
                    </StatusChip>
                  </span>
                {/if}
                <span
                  class="s-connection-account-source"
                  use:tooltip={accountSourceTooltip(account)}
                >
                  {describeAccountSource(account)}
                </span>
                <div class="s-connection-account-actions">
                  {#if isOAuthDeviceFlowConnection(connection) && isOAuthAccount(account)}
                    <Button
                      variant="secondary"
                      onClick={() =>
                        disconnectOAuthAccount(provider, connection, account)}
                    >
                      {t('settings.providers.disconnect')}
                    </Button>
                  {:else if !isOAuthConnection(connection)}
                    <Button
                      variant="secondary"
                      onClick={() =>
                        openReplaceKeyModal(provider, connection, account)}
                    >
                      {t('settings.providers.replaceKey')}
                    </Button>
                    {#if isProcessEnvAccount(account)}
                      <span
                        class="s-connection-account-locked"
                        use:tooltip={t(
                          'settings.providers.accounts.removeEnvHint',
                        )}
                      >
                        <Button variant="danger" disabled>
                          {t('common.remove')}
                        </Button>
                      </span>
                    {:else}
                      <Button
                        variant="danger"
                        onClick={() =>
                          removeApiKey(provider, connection, account)}
                      >
                        {isSharedOpenCodeConnection(connection)
                          ? t('settings.providers.opencode.removeKey')
                          : t('common.remove')}
                      </Button>
                    {/if}
                  {/if}
                </div>
              </li>
            {/each}
          </ul>
        {/if}
        {#if connectionSupportsAddAccount(connection)}
          <div class="s-provider-inline-actions s-connection-add-account">
            <Button
              variant="tertiary"
              onClick={() => openAddAccountModal(provider, connection)}
            >
              {t('settings.providers.accounts.addButton')}
            </Button>
          </div>
        {/if}
      {/if}
    </div>
  {/each}
</div>
