<script>
  import { onDestroy, untrack } from 'svelte';
  import { invokeExtensionContribution } from '$lib/api.js';
  import {
    bindContributionParams,
    contributionData,
    normalizeContributionItems,
  } from '$lib/contributions.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ExtensionPage from '../ExtensionPage.svelte';
  import ContributionIcon from './ContributionIcon.svelte';

  let {
    descriptor,
    registry,
    context = {},
    data = {},
    commands = {},
  } = $props();
  let response = $state(null);
  let error = $state('');
  let busy = $state(false);
  let disposed = false;
  let revision = 0;
  let reading = false;
  let queued = false;
  const items = $derived(
    normalizeContributionItems(descriptor, response, context),
  );
  const ui = $derived({
    surface: descriptor.surface,
    context,
    data: contributionData(data),
    commands: [
      ...new Set([...(registry?.commandNames ?? []), ...Object.keys(commands)]),
    ],
  });

  async function refresh() {
    if (!descriptor.source || disposed) return;
    queued = true;
    if (reading || busy) return;
    reading = true;
    try {
      while (queued && !disposed && !busy) {
        queued = false;
        const generation = revision;
        try {
          const result = await invokeExtensionContribution(
            descriptor,
            descriptor.source.operation,
            bindContributionParams(descriptor.source.params, context),
          );
          if (!disposed && generation === revision) {
            response = result;
            error = '';
          }
        } catch (failure) {
          if (!disposed && generation === revision) error = failure.message;
        }
      }
    } finally {
      reading = false;
    }
  }

  $effect(() => {
    let parameters;
    try {
      parameters = bindContributionParams(descriptor.source?.params, context);
    } catch {
      parameters = null;
    }
    JSON.stringify([descriptor.id, descriptor.epoch, parameters]);
    untrack(() => {
      revision++;
      response = null;
      error = '';
      void refresh();
    });
  });
  $effect(() =>
    registry?.subscribe((change) => {
      if (change.owner != null && change.owner !== descriptor.extension) return;
      if (
        descriptor.source?.resource &&
        change.change?.resource &&
        descriptor.source.resource !== change.change.resource
      )
        return;
      void refresh();
    }),
  );
  onDestroy(() => {
    disposed = true;
    revision++;
  });

  async function act(item) {
    if (busy || item.disabled || !item.action) return;
    const action = item.action;
    const generation = ++revision;
    busy = true;
    try {
      const params = bindContributionParams(action.params, context);
      if (action.operation) {
        if (!descriptor.operations?.includes(action.operation))
          throw new Error(`Extension action unavailable: ${action.operation}`);
        await invokeExtensionContribution(descriptor, action.operation, params);
      } else if (action.command)
        await registry.command(action.command, params, commands);
      if (!disposed && generation === revision) error = '';
    } catch (failure) {
      if (!disposed && generation === revision) {
        error = failure.message;
        registry?.notifyError(failure);
      }
    } finally {
      busy = false;
      if (!disposed) void refresh();
    }
  }
</script>

{#if descriptor.kind === 'view' && descriptor.page}
  <section
    class="contribution-view"
    aria-label={descriptor.title}
    style={`--contribution-height: ${Math.max(160, Math.min(1600, descriptor.height ?? 480))}px`}
  >
    <ExtensionPage
      descriptor={descriptor.page}
      apiCapabilities={registry?.apiCapabilities}
      theme={registry?.theme}
      locale={registry?.locale}
      timezone={registry?.timezone}
      subscribeInvalidations={registry?.subscribe}
      onOpenSession={registry?.openSession}
      onToast={registry?.toast}
      uiContext={ui}
      embedded
      onUiCommand={(name, params) => registry.command(name, params, commands)}
    />
  </section>
{:else}
  {#each items as item (item.id)}
    {#if item.action}
      <Button
        variant={item.tone === 'danger' ? 'danger' : 'tertiary'}
        disabled={busy || item.disabled}
        loading={busy}
        ariaLabel={item.label}
        tooltip={error || item.tooltip || item.label}
        onClick={() => act(item)}
      >
        <ContributionIcon icon={item.icon ?? descriptor.icon} />
        {item.text}
      </Button>
    {:else}
      <span
        class="contribution-status"
        role="status"
        use:tooltip={item.tooltip || item.label}
      >
        <ContributionIcon icon={item.icon ?? descriptor.icon} />{item.text}
      </span>
    {/if}
  {/each}
  {#if error && !items.length}
    <Button variant="tertiary" tooltip={error} onClick={() => refresh()}>
      {t('contributions.retry', { name: descriptor.title })}
    </Button>
  {/if}
{/if}

<style>
  .contribution-status {
    display: inline-flex;
    gap: 4px;
    align-items: center;
    color: var(--text-med);
    font-size: 12px;
  }
  .contribution-view {
    width: 100%;
    min-width: 0;
    height: var(--contribution-height);
  }
</style>
