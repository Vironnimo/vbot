<script>
  import MemberToolsEditor from './channels/MemberToolsEditor.svelte';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  let {
    channel,
    rowBusy,
    setOwnIdentity,
    toggleParticipantRole,
    tools = [],
    saveMemberTools,
    onError,
  } = $props();
</script>

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

  {#if channel.access?.loaded}
    <p class="s-row-desc">{t('settings.channels.memberTools.help')}</p>
    <MemberToolsEditor
      channelId={channel.id}
      value={channel.access.member_tools}
      {tools}
      onSave={saveMemberTools}
      {onError}
    />
  {/if}

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

        {#if channel.access?.loaded}
          <MemberToolsEditor
            channelId={channel.id}
            scopeId={group.access_scope_id}
            value={group.member_tools}
            inherited={channel.access.member_tools}
            {tools}
            onSave={saveMemberTools}
            {onError}
          />
        {/if}

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
                <span class="s-row-desc">ID {participant.user_id}</span>
              </div>
              <StatusChip
                variant={participant.role === 'admin' ? 'success' : 'info'}
              >
                {participant.role === 'admin'
                  ? t('settings.channels.access.admin')
                  : t('settings.channels.access.member')}
              </StatusChip>
              <div class="s-entity__end">
                <Button
                  variant="secondary"
                  disabled={rowBusy || isOwnIdentity}
                  ariaLabel={t('settings.channels.access.thisIsMeAria', {
                    name: participant.display_name,
                  })}
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
                    toggleParticipantRole(channel, group, participant)}
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
