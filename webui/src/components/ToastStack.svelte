<script>
  import { t } from '$lib/i18n.js';
  import { watchToastClearance } from '$lib/toastClearance.js';
  import Button from './ui/Button.svelte';

  // `toasts` report events and carry a title. `notices` are app-wide states
  // that stay while the state lasts: each carries a message, optional
  // `actions`, and an `onDismiss` only when it may be closed. `children` is
  // a live control such as the Speech player, kept below everything else and
  // outside the live region.
  let { toasts = [], notices = [], onDismiss, children } = $props();

  let stack = $state(null);
  let clearance = $state(0);

  $effect(() => {
    if (stack) return watchToastClearance(stack, (px) => (clearance = px));
  });
</script>

{#snippet card(toast, dismiss)}
  <article
    class="toast"
    class:error={toast.variant === 'error'}
    class:warn={toast.variant === 'warn'}
    class:info={toast.variant === 'info'}
    class:success={toast.variant === 'success'}
    data-toast-id={toast.id}
  >
    <div class="toast-body">
      {#if toast.title}
        <div class="toast-title">{toast.title}</div>
      {/if}
      {#if toast.message}
        <div class="toast-msg">{toast.message}</div>
      {/if}
      {#if toast.actions?.length}
        <div class="toast-actions">
          {#each toast.actions as action (action.label)}
            <Button
              variant={action.variant ?? 'secondary'}
              loading={action.loading}
              onClick={action.onClick}
            >
              {action.label}
            </Button>
          {/each}
        </div>
      {/if}
    </div>
    {#if dismiss}
      <button
        class="toast-close"
        type="button"
        aria-label={t('common.close')}
        onclick={dismiss}
      >
        <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
          ><path d="m6 6 12 12M18 6 6 18" /></svg
        >
      </button>
    {/if}
  </article>
{/snippet}

<div
  class="toast-stack"
  bind:this={stack}
  style:bottom={clearance ? `${clearance}px` : null}
>
  {@render children?.()}
  <div class="toast-stack__live" aria-live="polite" aria-atomic="false">
    {#each notices as notice (notice.id)}
      {@render card(notice, notice.onDismiss)}
    {/each}
    {#each toasts as toast (toast.id)}
      {@render card(toast, () => onDismiss?.(toast.id))}
    {/each}
  </div>
</div>
