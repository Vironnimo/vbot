<script>
  import { getSessionPermissions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  // A compact "Restricted" mark for a Session (or a draft's next Session)
  // whose Runs work under Permission Rules; nothing for full access. Its
  // tooltip lists every layer that applies (`permissions.session`): the
  // Agent's own rules and each inherited limit.
  let {
    agentAddress = '',
    sessionId = '',
    // A draft's chosen Working Project (null: the Agent's Workspace);
    // undefined for a Session or a Team member, whose folder is known.
    workingProjectId = undefined,
  } = $props();

  let summary = $state(null);
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

  let details = $derived(
    summary?.restricted
      ? {
          title: t('permissions.session.title'),
          text: t('permissions.session.text'),
          rows: (summary.layers ?? []).flatMap(layerRows),
          selectable: true,
        }
      : null,
  );
</script>

{#if details}
  <button
    type="button"
    class="session-permissions"
    aria-label={`${t('permissions.session.badge')}: ${t('permissions.session.title')}`}
    use:tooltip={details}
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
{/if}

<style>
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
</style>
