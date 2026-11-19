<script>
  import { t } from '$lib/i18n.js';
  import {
    PERMISSION_BASE_PARTS,
    PERMISSION_RULE_EFFECTS,
    permissionBaseLabel,
    permissionPartLabel,
    permissionProfileLabel,
  } from '$lib/permissionRules.js';

  let { profile, command } = $props();

  const rules = $derived(
    profile.action === 'delete' ? profile.base : profile.rules,
  );

  function title({ action, name: id, target, base }) {
    const name = permissionProfileLabel(id);
    switch (action) {
      case 'save':
        return base
          ? t('approvals.profile.update', { name })
          : t('approvals.profile.create', { name });
      case 'delete':
        return t('approvals.profile.delete', { name });
      case 'assign':
        return t('approvals.profile.assign', { name, target });
      case 'inherit':
        return t('approvals.profile.inherit', { target });
      case 'inline':
        return t('approvals.profile.inline', { target });
      default:
        return t('approvals.need.agentAccess');
    }
  }
</script>

{#snippet definition(rules)}
  <dl class="profile-review__rules">
    {#each PERMISSION_BASE_PARTS as part (part)}
      {#if rules[part] != null}
        <dt>{permissionPartLabel(part)}</dt>
        <dd>{permissionBaseLabel(part, rules[part])}</dd>
      {/if}
    {/each}
    {#each PERMISSION_RULE_EFFECTS as effect (effect)}
      {#if Array.isArray(rules[effect])}
        <dt class="profile-review__effect-label">
          {effect === 'allow'
            ? t('permissions.allowTitle')
            : t('permissions.denyTitle')}
        </dt>
        <dd class="profile-review__effect-list">
          {#if rules[effect].length}
            <ul>
              {#each rules[effect] as rule, index (index)}
                <li><code>{rule}</code></li>
              {/each}
            </ul>
          {:else}
            {t('permissions.rulesNone')}
          {/if}
        </dd>
      {/if}
    {/each}
  </dl>
{/snippet}

<div class="profile-review">
  <p class="profile-review__title">{title(profile)}</p>
  {#if rules}
    {@render definition(rules)}
  {:else if rules === null && (profile.action === 'inherit' || profile.action === 'inline')}
    <p class="profile-review__access">{t('permissions.preset.full_access')}</p>
  {/if}
  {#if profile.action === 'save' && profile.base}
    <details>
      <summary>{t('approvals.profile.previous')}</summary>
      {@render definition(profile.base)}
    </details>
  {/if}
  {#if command && profile.action !== 'save' && profile.action !== 'delete'}
    <code class="profile-review__command">{command}</code>
  {/if}
</div>

<style>
  .profile-review {
    display: grid;
    container: profile-review / inline-size;
    min-width: 0;
    gap: var(--space-sm);
    margin-block: var(--space-xs);
  }
  .profile-review__access,
  .profile-review__title {
    margin: 0;
  }
  .profile-review__title {
    font-weight: 600;
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .profile-review__rules {
    display: grid;
    grid-template-columns: fit-content(46%) minmax(0, 1fr);
    gap: var(--space-xs) var(--space-md);
    align-items: baseline;
    margin: 0;
  }
  .profile-review__rules dt {
    color: var(--text-med);
  }
  .profile-review__rules dd,
  .profile-review__rules ul {
    margin: 0;
  }
  .profile-review__rules ul {
    display: grid;
    gap: var(--space-xs);
    padding: 0;
    list-style: none;
  }
  .profile-review__command {
    color: var(--text-med);
  }
  .profile-review__command,
  .profile-review__rules code {
    font: var(--fs-mono-xs) / 1.5 var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @container profile-review (max-width: 26rem) {
    .profile-review__effect-label,
    .profile-review__effect-list {
      grid-column: 1 / -1;
    }
    .profile-review__effect-label {
      margin-top: var(--space-xs);
    }
  }
  .profile-review summary {
    width: fit-content;
    color: var(--text-med);
    cursor: pointer;
  }
  .profile-review details[open] summary {
    margin-bottom: var(--space-sm);
  }
</style>
