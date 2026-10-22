<script>
  import { clearSessionCeiling, getSessionPermissions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  // A compact "Restricted" mark for a Session (or a draft's next Session)
  // whose Runs work under Permission Rules; nothing for full access. Its
  // hover card lists every layer that applies (`permissions.session`): the
  // Agent's own rules and each inherited limit, then the honest limits of
  // the checks (`.vorch/domain-maps/permissions.md`). A Session that stores
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

  const SCRATCH = '{scratch}';
  const SEPARATOR = ', ';

  // A row's value as items: the folders or rules of a list (`mono`), or one
  // word for no limit or nothing. The Agent's own rules keep `{scratch}`, the
  // conversation's scratch area, which has no folder before a Run creates it:
  // it reads as a name, and a rule below it is explained under the rows.
  function listed(values, anything) {
    if (values === null || values === undefined) return [{ text: anything }];
    if (values.length === 0)
      return [{ text: t('permissions.preview.nothing') }];
    return values.map((value) =>
      value === SCRATCH
        ? { text: t('permissions.session.scratch') }
        : { text: value, mono: true },
    );
  }

  function codes(values) {
    return values.map((value) => ({ text: value, mono: true }));
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
      { label: sourceLabel(layer.source), items: [] },
      {
        label: t('permissions.session.workdir'),
        items: codes(layer.workdir ? [layer.workdir] : []),
      },
      {
        label: t('permissions.preview.read'),
        items: listed(layer.read, t('permissions.preview.anywhere')),
      },
      {
        label: t('permissions.preview.write'),
        items: listed(layer.write, t('permissions.preview.anywhere')),
      },
      {
        label: t('permissions.preview.commands'),
        items: listed(layer.commands, t('permissions.preview.anyCommand')),
      },
      {
        label: t('permissions.preview.web'),
        items: listed(layer.web, t('permissions.preview.anySite')),
      },
    ];
    if ((layer.deny ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.deny'),
        items: codes(layer.deny),
      });
    }
    if ((layer.inactive ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.inactive'),
        items: codes(layer.inactive),
      });
    }
    return rows;
  }

  let rows = $derived(
    summary?.restricted ? (summary.layers ?? []).flatMap(layerRows) : [],
  );
  // A folder or rule below the scratch area still names `{scratch}`.
  let namesScratch = $derived(
    rows.some((row) =>
      row.items.some((item) => item.mono && item.text.includes(SCRATCH)),
    ),
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
      <!-- Only the rows scroll: the limits of the checks and the action stay
           in view on a short window. -->
      <div class="session-permissions-card__body">
        <dl class="app-tooltip__rows">
          {#each rows as row, index (index)}
            <dt class="app-tooltip__label">{row.label}</dt>
            <dd class="app-tooltip__value">
              {#each row.items as item, position (position)}
                {#if position > 0}{SEPARATOR}{/if}<span
                  class:app-tooltip__mono={item.mono}>{item.text}</span
                >
              {/each}
            </dd>
          {/each}
        </dl>
        {#if namesScratch}
          <p class="session-permissions-card__note">
            {t('permissions.preview.scratch', { scratch: SCRATCH })}
          </p>
        {/if}
      </div>
      <p
        class="session-permissions-card__note session-permissions-card__limits"
      >
        {t('permissions.limits')}
        <InfoHint text={t('permissions.limitsHelp')} />
      </p>
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
    display: flex;
    flex-direction: column;
    width: min(360px, calc(100vw - 16px));
    max-width: min(360px, calc(100vw - 16px));
    overflow: hidden;
    font-size: var(--fs-label-sm);
    line-height: 1.4;
    user-select: text;
  }
  .session-permissions-card__body {
    flex: 0 1 auto;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .session-permissions-card__limits,
  .session-permissions-card__footer {
    flex-shrink: 0;
  }
  .session-permissions-card__note {
    margin: 6px 0 0;
    color: var(--text-lo);
  }
  .session-permissions-card__limits {
    margin-top: 8px;
    padding-top: 7px;
    border-top: 1px solid var(--border-2);
  }
  .session-permissions-card__limits :global(.info-hint) {
    margin-left: 2px;
    vertical-align: -3px;
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
