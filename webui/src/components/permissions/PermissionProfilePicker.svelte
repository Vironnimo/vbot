<script>
  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import PermissionRulesEditor from './PermissionRulesEditor.svelte';
  import { defaultOptionLabel } from '$lib/defaultChoice.js';
  import { t } from '$lib/i18n.js';
  import {
    permissionProfiles,
    refreshPermissionProfiles,
    permissionProfileLabel,
  } from '$lib/permissionProfiles.js';

  let {
    profile = null,
    rules = null,
    inherited = null,
    level = 'agent',
    agentId = '',
    compact = false,
    disabled = false,
    idPrefix,
    onChange,
  } = $props();

  // A reference stays pinned even when its profile happens to match today's inherited rules.
  let selected = $derived(
    profile ? `profile:${profile}` : rules ? 'local' : '',
  );
  let options = $derived.by(() => {
    const entries = [
      {
        value: '',
        label: defaultOptionLabel(
          inherited
            ? t('permissions.profile.inherited')
            : t('permissions.profile.inheritedFull'),
        ),
      },
    ];
    for (const { name } of $permissionProfiles.profiles) {
      entries.push({
        value: `profile:${name}`,
        label: permissionProfileLabel(name),
      });
    }
    if (profile && !entries.some((entry) => entry.value === selected)) {
      entries.push({
        value: selected,
        label:
          !$permissionProfiles.loading && !$permissionProfiles.error
            ? t('permissions.profile.missing', { name: profile })
            : permissionProfileLabel(profile),
        disabled: true,
      });
    }
    if (rules && !profile)
      entries.push({ value: 'local', label: t('permissions.profile.local') });
    return entries;
  });
  function select(value) {
    if (value === selected || value === 'local') return;
    onChange(value ? value.slice('profile:'.length) : null, null);
  }
</script>

{#snippet picker()}
  <Dropdown
    id={`${idPrefix}-profile`}
    value={selected}
    {options}
    disabled={disabled ||
      ($permissionProfiles.loading && !$permissionProfiles.profiles.length)}
    ariaLabel={t('permissions.profile.label')}
    onValueChange={select}
  />
{/snippet}

{#snippet summary()}
  {#if profile === 'full_access' && inherited}
    <p class="s-row-desc" role="status">
      {level === 'override'
        ? t('permissions.preset.ignoresInherited')
        : t('permissions.preset.ignoresDefault')}
    </p>
  {:else if profile === 'restricted'}
    <p class="s-row-desc">{t('permissions.profile.restrictedSummary')}</p>
  {/if}
{/snippet}

<div class="permission-profile" data-permission-profile>
  {#if compact}
    {@render picker()}
    {@render summary()}
  {:else}
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">{t('permissions.profile.label')}</div>
          {@render summary()}
        </div>
        <div class="s-row-control">{@render picker()}</div>
      </div>
      {#if (profile && profile !== 'full_access') || (!profile && inherited && !rules)}
        <div class="s-group__block s-group__block--attached s-group__note">
          <p>
            {t('permissions.limits')}
            <InfoHint text={t('permissions.limitsHelp')} />
          </p>
        </div>
      {/if}
    </div>
  {/if}
  {#if $permissionProfiles.error}
    <div role="alert" class="s-row-error">
      {$permissionProfiles.error}
      <Button variant="tertiary" onClick={refreshPermissionProfiles}
        >{t('common.retry')}</Button
      >
    </div>
  {/if}
  {#if !profile && rules && !compact}
    <PermissionRulesEditor
      value={rules}
      {inherited}
      {level}
      {agentId}
      {idPrefix}
      {disabled}
      onChange={(next) => onChange(null, next)}
    />
  {:else if compact && ((profile && profile !== 'full_access') || (!profile && inherited))}
    <p class="s-row-desc">
      {t('permissions.limits')}
      <InfoHint text={t('permissions.limitsHelp')} />
    </p>
  {/if}
</div>

<style>
  .permission-profile {
    display: grid;
    gap: var(--space-sm);
    min-width: 0;
  }
  .permission-profile > p {
    margin: 0;
  }
</style>
