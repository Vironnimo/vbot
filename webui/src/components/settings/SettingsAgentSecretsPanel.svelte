<script>
  // Settings -> Secrets: the Agent Secrets Agents use through
  // `{{secret:NAME}}` references without seeing their values
  // (`agent_secrets.*`), apart from vBot's own credentials under
  // Integrations -> Credentials. A row shows a Secret's name, kind, lifetime,
  // description and Source state; its details the fields (visible values, a
  // concealed one only after Reveal and until Hide, never kept), the granted
  // Agents, the sinks and the recent uses. Add, Edit and Delete change
  // Secrets; the password managers below bind entries as Secrets. The
  // overview reloads on mount and whenever `agentSecretsRefreshToken` bumps
  // (`resource_changed(kind:"agent_secrets")` or a replay gap); only the
  // newest read replaces it, and every reload hides revealed values.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import AgentSecretEditor from './AgentSecretEditor.svelte';
  import AgentSecretSources from './AgentSecretSources.svelte';
  import {
    deleteAgentSecret,
    listAgentSecrets,
    revealAgentSecret,
  } from '$lib/api.js';
  import {
    fieldLabel,
    kindLabel,
    lifetimeLabel,
    normalizeSecretOverview,
    sinkLabel,
  } from '$lib/agentSecrets.js';
  import { t } from '$lib/i18n.js';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/timeText.js';

  const componentId = $props.id();
  const noop = () => {};
  const RECENT_USES_SHOWN = 5;
  const SOURCE_CHIPS = {
    locked: {
      variant: 'warn',
      label: () => t('settings.agentSecrets.sourceLocked'),
    },
    unavailable: {
      variant: 'neutral',
      label: () => t('settings.agentSecrets.sourceUnavailable'),
    },
  };

  let {
    agentSecretsRefreshToken = 0,
    // The Identity Agents a Secret can be granted to.
    agents = [],
    onToast = noop,
  } = $props();

  let overview = $state({ secrets: [], sources: [], requests: [] });
  let loaded = $state(false);
  let loadError = $state('');
  let loadGeneration = 0;
  let expanded = $state([]);
  // Revealed values by `<id>.<field>`, only while shown.
  let revealed = $state({});
  let revealing = $state('');
  let rowError = $state('');
  // `{ mode: 'create' }`, `{ mode: 'edit', secret }` or
  // `{ mode: 'bind', binding }`.
  let editor = $state(null);
  let removeTarget = $state(null);
  let busy = $state(false);
  let dialogError = $state('');

  const agentNames = $derived(
    new Map(agents.map((agent) => [agent.id, agent.name || agent.id])),
  );

  $effect(() => {
    void agentSecretsRefreshToken;
    void load();
  });

  async function load() {
    const generation = ++loadGeneration;
    try {
      const result = await listAgentSecrets();
      if (generation !== loadGeneration) return;
      overview = normalizeSecretOverview(result);
      revealed = {};
      loadError = '';
      loaded = true;
    } catch (error) {
      if (generation === loadGeneration) {
        loadError = `${t('settings.agentSecrets.loadError')} ${error?.message ?? ''}`;
      }
    }
  }

  function lifetimeText(secret) {
    if (secret.lifetime === 'until' && secret.expires_at) {
      return t('settings.agentSecrets.until', {
        time: formatAbsoluteTime(secret.expires_at),
      });
    }
    return lifetimeLabel(secret.lifetime);
  }

  function summary(secret) {
    return [kindLabel(secret.kind), lifetimeText(secret), secret.description]
      .filter(Boolean)
      .join(' · ');
  }

  function grantsText(secret) {
    if (!['permanent', 'until'].includes(secret.lifetime)) {
      return t('settings.agentSecrets.sessionOnly');
    }
    const names = (secret.agents ?? []).map((id) => agentNames.get(id) ?? id);
    return names.length > 0
      ? names.join(', ')
      : t('settings.agentSecrets.noGrants');
  }

  function useText(use) {
    return [
      fieldLabel(use.field),
      sinkLabel(use.sink),
      agentNames.get(use.agent_id) ?? use.agent_id,
      formatRelativeTime(use.at),
    ]
      .filter(Boolean)
      .join(' · ');
  }

  function isExpanded(id) {
    return expanded.includes(id);
  }

  function hideAll(id) {
    revealed = Object.fromEntries(
      Object.entries(revealed).filter(([key]) => !key.startsWith(`${id}.`)),
    );
  }

  function toggleDetails(id) {
    if (isExpanded(id)) {
      expanded = expanded.filter((item) => item !== id);
      hideAll(id);
    } else {
      expanded = [...expanded, id];
    }
  }

  function handleHeadClick(event, id) {
    if (
      event.target.closest('button, a, input, select, textarea') ||
      window.getSelection()?.toString()
    )
      return;
    toggleDetails(id);
  }

  async function toggleReveal(secret, field) {
    const key = `${secret.id}.${field.name}`;
    if (Object.hasOwn(revealed, key)) {
      revealed = Object.fromEntries(
        Object.entries(revealed).filter(([shownKey]) => shownKey !== key),
      );
      return;
    }
    revealing = key;
    rowError = '';
    try {
      const result = await revealAgentSecret(secret.id, field.name);
      if (isExpanded(secret.id)) {
        revealed = { ...revealed, [key]: String(result?.value ?? '') };
      }
    } catch (error) {
      rowError = `${t('settings.agentSecrets.revealError', { name: secret.name })} ${error?.message ?? ''}`;
    } finally {
      revealing = '';
    }
  }

  function closeDialogs() {
    editor = null;
    removeTarget = null;
    busy = false;
    dialogError = '';
  }

  function saved(secret, mode) {
    closeDialogs();
    onToast({
      title:
        mode === 'edit'
          ? t('settings.agentSecrets.updated', { name: secret.name })
          : t('settings.agentSecrets.created', { name: secret.name }),
      variant: 'success',
    });
    void load();
  }

  async function confirmRemove() {
    const secret = removeTarget;
    if (!secret || busy) return;
    busy = true;
    dialogError = '';
    try {
      await deleteAgentSecret(secret.id);
      closeDialogs();
      onToast({
        title: t('settings.agentSecrets.deleted', { name: secret.name }),
        variant: 'success',
      });
      void load();
    } catch (error) {
      busy = false;
      dialogError = `${t('settings.agentSecrets.deleteError')} ${error?.message ?? ''}`;
    }
  }
</script>

{#if loadError}
  <Banner variant="error" role="alert">
    <span>{loadError}</span>
    <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
  </Banner>
{/if}

<div class="s-group-toolbar s-list-toolbar">
  <span class="s-group-toolbar__meta agent-secrets-meta">
    {#if overview.secrets.length > 0}
      <span>
        {t('settings.agentSecrets.count', { count: overview.secrets.length })}
      </span>
    {/if}
    <InfoHint text={t('settings.agentSecrets.help')} />
  </span>
  <div class="s-group-toolbar__actions s-list-toolbar__end">
    <Button
      variant="secondary"
      disabled={!loaded}
      onClick={() => {
        closeDialogs();
        editor = { mode: 'create' };
      }}
    >
      {t('settings.agentSecrets.add')}
    </Button>
  </div>
</div>

{#if rowError}
  <Banner variant="error" role="alert">{rowError}</Banner>
{/if}

{#if !loaded}
  {#if !loadError}
    <Banner variant="neutral">{t('common.loading')}</Banner>
  {/if}
{:else if overview.secrets.length === 0}
  <EmptyState
    density="compact"
    description={t('settings.agentSecrets.empty')}
  />
{:else}
  <div
    class="s-group"
    role="list"
    aria-label={t('settings.agentSecrets.listLabel')}
  >
    {#each overview.secrets as secret, index (secret.id)}
      {@const open = isExpanded(secret.id)}
      {@const sourceChip = secret.source
        ? SOURCE_CHIPS[secret.source.state]
        : null}
      {@const uses = (secret.recent_uses ?? []).slice(0, RECENT_USES_SHOWN)}
      <div
        class="s-entity"
        role="listitem"
        data-agent-secret={secret.name}
        data-agent-secret-lifetime={secret.lifetime}
      >
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
        <div
          class="s-entity__head s-entity__head--toggle"
          onclick={(event) => handleHeadClick(event, secret.id)}
        >
          <div class="s-row-info">
            <div class="s-row-label agent-secrets-mono">{secret.name}</div>
            <div class="s-row-desc">{summary(secret)}</div>
          </div>
          <div class="s-entity__end">
            {#if sourceChip}
              <StatusChip variant={sourceChip.variant}>
                {sourceChip.label()}
              </StatusChip>
            {/if}
            <Button
              variant="tertiary"
              ariaLabel={t('settings.agentSecrets.editAria', {
                name: secret.name,
              })}
              onClick={() => {
                closeDialogs();
                editor = { mode: 'edit', secret };
              }}
            >
              {t('common.edit')}
            </Button>
            <Button
              variant="secondary"
              ariaLabel={t('settings.agentSecrets.deleteAria', {
                name: secret.name,
              })}
              onClick={() => {
                closeDialogs();
                removeTarget = secret;
              }}
            >
              {t('common.delete')}
            </Button>
            <Button
              variant="tertiary"
              icon
              class="s-disclosure-btn"
              aria-controls={`${componentId}-details-${index}`}
              ariaLabel={t('settings.agentSecrets.detailsAria', {
                name: secret.name,
              })}
              aria-expanded={open}
              onClick={() => toggleDetails(secret.id)}
            >
              <span
                class="disclosure-chevron"
                class:disclosure-chevron--open={open}
                aria-hidden="true"
              ></span>
            </Button>
          </div>
        </div>
        <div
          id={`${componentId}-details-${index}`}
          class="s-disclosure-sub"
          hidden={!open}
        >
          <dl class="agent-secrets-facts">
            <dt>{t('settings.agentSecrets.reference')}</dt>
            <dd class="agent-secrets-mono">{secret.reference}</dd>
            {#if secret.source}
              <dt>{t('settings.agentSecrets.source')}</dt>
              <dd>
                {secret.source.source} ·
                <span class="agent-secrets-mono">{secret.source.reference}</span
                >
              </dd>
            {/if}
            <dt>{t('settings.agentSecrets.fields')}</dt>
            <dd>
              <ul class="agent-secrets-fields">
                {#each secret.fields ?? [] as field (field.name)}
                  {@const key = `${secret.id}.${field.name}`}
                  {@const shown = Object.hasOwn(revealed, key)}
                  <li data-secret-field={field.name}>
                    <span class="agent-secrets-mono agent-secrets-field-name">
                      {field.name}
                    </span>
                    {#if !field.concealed}
                      <span class="agent-secrets-mono agent-secrets-value">
                        {field.value ?? ''}
                      </span>
                    {:else}
                      <span
                        class="agent-secrets-mono agent-secrets-value"
                        class:agent-secrets-value--masked={!shown}
                        data-revealed={shown}
                      >
                        {shown ? revealed[key] : '••••••••'}
                      </span>
                      {#if open}
                        <Button
                          variant="tertiary"
                          ariaLabel={shown
                            ? t('settings.agentSecrets.hideAria', {
                                field: field.name,
                                name: secret.name,
                              })
                            : t('settings.agentSecrets.revealAria', {
                                field: field.name,
                                name: secret.name,
                              })}
                          loading={revealing === key}
                          onClick={() => toggleReveal(secret, field)}
                        >
                          {shown
                            ? t('secrets.input.hide')
                            : t('settings.agentSecrets.reveal')}
                        </Button>
                      {/if}
                    {/if}
                  </li>
                {/each}
              </ul>
            </dd>
            <dt>{t('settings.agentSecrets.agents')}</dt>
            <dd>{grantsText(secret)}</dd>
            <dt>{t('settings.agentSecrets.sinks')}</dt>
            <dd>
              {(secret.sinks ?? []).map(sinkLabel).join(', ') ||
                t('settings.agentSecrets.noSinks')}
            </dd>
            <dt>{t('settings.agentSecrets.recentUses')}</dt>
            <dd>
              {#if uses.length === 0}
                {t('settings.agentSecrets.noUses')}
              {:else}
                <ul class="agent-secrets-uses">
                  {#each uses as use, useIndex (`${use.at}:${useIndex}`)}
                    <li>{useText(use)}</li>
                  {/each}
                </ul>
              {/if}
            </dd>
          </dl>
        </div>
      </div>
    {/each}
  </div>
{/if}

<AgentSecretSources
  sources={overview.sources}
  {onToast}
  onChanged={load}
  onBind={(binding) => {
    closeDialogs();
    editor = { mode: 'bind', binding };
  }}
/>

{#if editor}
  <AgentSecretEditor
    mode={editor.mode}
    secret={editor.secret ?? null}
    binding={editor.binding ?? null}
    {agents}
    onSaved={saved}
    onClose={closeDialogs}
  />
{/if}

{#if removeTarget}
  <ConfirmDialog
    title={t('settings.agentSecrets.deleteTitle', { name: removeTarget.name })}
    body={t('settings.agentSecrets.deleteBody', {
      reference: removeTarget.reference,
    })}
    confirmLabel={t('common.delete')}
    danger
    {busy}
    error={dialogError}
    onConfirm={confirmRemove}
    onCancel={closeDialogs}
  />
{/if}

<style>
  .agent-secrets-meta {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .agent-secrets-mono {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }

  .agent-secrets-facts {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 6px 16px;
    margin: 0;
  }

  .agent-secrets-facts dt {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .agent-secrets-facts dd {
    min-width: 0;
    margin: 0;
    font-size: var(--fs-body-sm);
  }

  .agent-secrets-fields,
  .agent-secrets-uses {
    display: grid;
    gap: 4px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .agent-secrets-fields li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .agent-secrets-field-name {
    color: var(--text-med);
  }

  .agent-secrets-value {
    min-width: 0;
    white-space: pre-wrap;
  }

  .agent-secrets-value--masked {
    color: var(--text-lo);
  }

  @media (max-width: 640px) {
    .agent-secrets-facts {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
