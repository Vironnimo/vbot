<script>
  import { clearSessionCeiling, getSessionPermissions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';

  // A compact "Restricted" mark for a Session (or a draft's next Session)
  // whose Runs work under Permission Rules; nothing for full access. Its
  // hover card lists every layer that applies (`permissions.session`): the
  // Agent's own rules and each inherited limit. A Session that stores
  // inherited limits (from the Session that started it or a scheduled job)
  // offers to remove them, after a confirmation
  // (`permissions.clear_session_ceiling`).
  let {
    agentAddress = '',
    sessionId = '',
    // A draft's chosen Working Project (null: the Agent's Workspace);
    // undefined for a Session or a Team member, whose folder is known.
    workingProjectId = undefined,
  } = $props();

  let summary = $state(null);
  let confirming = $state(false);
  let clearing = $state(false);
  let clearError = $state('');
  let sequence = 0;

  let requestKey = $derived(
    JSON.stringify([agentAddress, sessionId, workingProjectId ?? '\u0000']),
  );

  $effect(() => {
    const key = requestKey;
    void load(key);
  });

  async function load(key) {
    const ticket = ++sequence;
    if (!agentAddress) {
      summary = null;
      return;
    }
    try {
      const response = await getSessionPermissions({
        agentId: agentAddress,
        sessionId,
        workingProjectId,
      });
      if (ticket !== sequence || key !== requestKey) return;
      summary = response;
    } catch {
      // Unknown limits show no mark; the Tools still enforce them.
      if (ticket === sequence) summary = null;
    }
  }

  async function clearInherited() {
    clearing = true;
    clearError = '';
    try {
      await clearSessionCeiling({ agentId: agentAddress, sessionId });
      confirming = false;
      await load(requestKey);
    } catch (error) {
      clearError =
        typeof error?.message === 'string' && error.message.trim()
          ? error.message.trim()
          : t('permissions.session.clearFailed');
    } finally {
      clearing = false;
    }
  }

  function cancelClear() {
    if (clearing) return;
    confirming = false;
    clearError = '';
  }

  function listed(values, anything) {
    if (values === null || values === undefined) return anything;
    return values.length > 0
      ? values.join(', ')
      : t('permissions.preview.nothing');
  }

  const SOURCE_LABELS = {
    agent: () => t('permissions.session.source.agent'),
    parent: () => t('permissions.session.source.parent'),
    extension: () => t('permissions.session.source.extension'),
    cron: () => t('permissions.session.source.cron'),
  };

  function sourceLabel(source) {
    return (
      SOURCE_LABELS[source] ?? (() => t('permissions.session.source.other'))
    )();
  }

  function layerRows(layer) {
    const rows = [
      { label: sourceLabel(layer.source), value: '' },
      {
        label: t('permissions.session.workdir'),
        value: layer.workdir ?? '',
        mono: true,
      },
      {
        label: t('permissions.preview.read'),
        value: listed(layer.read, t('permissions.preview.anywhere')),
        mono: Array.isArray(layer.read),
      },
      {
        label: t('permissions.preview.write'),
        value: listed(layer.write, t('permissions.preview.anywhere')),
        mono: Array.isArray(layer.write),
      },
      {
        label: t('permissions.preview.commands'),
        value: listed(layer.commands, t('permissions.preview.anyCommand')),
        mono: Array.isArray(layer.commands),
      },
      {
        label: t('permissions.preview.web'),
        value: listed(layer.web, t('permissions.preview.anySite')),
        mono: Array.isArray(layer.web),
      },
    ];
    if ((layer.deny ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.deny'),
        value: layer.deny.join(', '),
        mono: true,
      });
    }
    if ((layer.inactive ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.inactive'),
        value: layer.inactive.join(', '),
        mono: true,
      });
    }
    return rows;
  }

  let rows = $derived(
    summary?.restricted ? (summary.layers ?? []).flatMap(layerRows) : [],
  );
  let clearable = $derived(
    Boolean(summary?.restricted && summary.stored_ceiling && sessionId),
  );
</script>

{#if summary?.restricted}
  <span class="session-permissions-anchor">
    <button
      type="button"
      class="session-permissions"
      aria-label={`${t('permissions.session.badge')}: ${t('permissions.session.title')}`}
      data-session-permissions
    >
      <svg
        class="session-permissions__icon"
        viewBox="0 0 16 16"
        width="13"
        height="13"
        aria-hidden="true"
      >
        <rect x="3.5" y="7" width="9" height="6.5" rx="1" />
        <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
      </svg>
      <span>{t('permissions.session.badge')}</span>
    </button>
    <div
      class="floating-card session-permissions-card"
      use:floatingHoverCard={{ openOnPress: true }}
      data-session-permissions-card
    >
      <div class="app-tooltip__title">{t('permissions.session.title')}</div>
      <div class="app-tooltip__text">{t('permissions.session.text')}</div>
      <dl class="app-tooltip__rows">
        {#each rows as row, index (index)}
          <dt class="app-tooltip__label">{row.label}</dt>
          <dd class="app-tooltip__value" class:app-tooltip__mono={row.mono}>
            {row.value}
          </dd>
        {/each}
      </dl>
      {#if clearable}
        <div class="session-permissions-card__footer">
          <Button
            variant="secondary"
            class="session-permissions-card__action"
            onClick={() => (confirming = true)}
          >
            {t('permissions.session.clear')}
          </Button>
        </div>
      {/if}
    </div>
  </span>
{/if}

{#if confirming}
  <ConfirmDialog
    title={t('permissions.session.clearTitle')}
    body={t('permissions.session.clearText')}
    confirmLabel={t('permissions.session.clearConfirm')}
    busy={clearing}
    error={clearError}
    onConfirm={clearInherited}
    onCancel={cancelClear}
  />
{/if}

<style>
  .session-permissions-anchor {
    display: inline-flex;
    flex-shrink: 0;
  }
  .session-permissions {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 4px;
    min-height: 26px;
    padding: 2px 6px;
    border: 1px solid transparent;
    border-radius: var(--r-md);
    color: var(--amber);
    background: transparent;
    font: inherit;
    font-size: var(--fs-label-sm);
    line-height: 1.4;
    cursor: default;
  }
  button.session-permissions:hover,
  button.session-permissions:focus-visible {
    background: var(--surface-3);
  }
  button.session-permissions:focus-visible {
    box-shadow: var(--field-focus-ring);
    outline: none;
  }
  .session-permissions__icon {
    flex-shrink: 0;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.3;
  }
  /* The card on the shared floating card (styles/app/hints.css), portaled to
     <body>; its title, text and rows use the details-card styles of the
     quick tooltip, so the limits read as they did there. */
  .session-permissions-card.floating-card {
    width: min(360px, calc(100vw - 16px));
    max-width: min(360px, calc(100vw - 16px));
    font-size: var(--fs-label-sm);
    line-height: 1.4;
    user-select: text;
  }
  .session-permissions-card__footer {
    display: flex;
    justify-content: flex-end;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--border);
  }
  /* Compact action with pointer precision; phones keep the 40px touch target. */
  @media (min-width: 641px) {
    .session-permissions-card__footer
      :global(.btn-secondary.session-permissions-card__action) {
      min-height: 28px;
      padding: 4px 11px;
    }
  }
</style>
