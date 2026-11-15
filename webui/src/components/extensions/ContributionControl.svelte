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
  let revision = 0;
  let sourceHandle;
  let visible = $state(false);
  const repeated = $derived(
    ['chat.message', 'chat.message.actions', 'chat.session.item'].includes(
      descriptor.surface,
    ),
  );
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

  function refresh() {
    sourceHandle?.refresh();
  }

  $effect(() => {
    const active = !repeated || visible;
    let parameters;
    let bindingError;
    try {
      parameters = bindContributionParams(descriptor.source?.params, context);
    } catch (failure) {
      parameters = null;
      bindingError = failure;
    }
    JSON.stringify([
      descriptor.id,
      descriptor.epoch,
      descriptor.source,
      parameters,
    ]);
    return untrack(() => {
      const generation = ++revision;
      response = null;
      error = bindingError?.message ?? '';
      if (!descriptor.source || !active || parameters === null) return;
      sourceHandle = registry.watchSource(descriptor, parameters, (next) => {
        if (generation !== revision || busy) return;
        response = next.result;
        error = next.error?.message ?? '';
      });
      const current = sourceHandle;
      return () => {
        ++revision;
        current.release();
        sourceHandle = null;
      };
    });
  });
  onDestroy(() => ++revision);

  function viewport(element) {
    return {
      destroy: registry.observeSource(element, (value) => (visible = value)),
    };
  }

  async function act(item) {
    if (busy || item.disabled || !item.action) return;
    const action = item.action;
    const generation = revision;
    busy = true;
    try {
      const params = bindContributionParams(action.params, context);
      if (action.operation) {
        if (!descriptor.operations?.includes(action.operation))
          throw new Error(
            t('contributions.extensionActionUnavailable', {
              name: action.operation,
            }),
          );
        await invokeExtensionContribution(descriptor, action.operation, params);
      } else if (action.command)
        await registry.command(action.command, params, commands);
      if (generation === revision) error = '';
    } catch (failure) {
      if (generation === revision) {
        error = failure.message;
        registry?.notifyError(failure);
      }
    } finally {
      busy = false;
      refresh();
    }
  }
</script>

{#if descriptor.source && repeated}
  <span class="contribution-viewport" aria-hidden="true" use:viewport></span>
{/if}

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
  .contribution-viewport {
    display: inline-block;
    width: 0;
    height: 1px;
  }
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
