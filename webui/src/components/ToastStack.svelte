<script>
  import { t } from '$lib/i18n.js';

  let { toasts = [], onDismiss } = $props();
</script>

{#if toasts.length > 0}
  <div class="toast-stack" aria-live="polite" aria-atomic="false">
    {#each toasts as toast (toast.id)}
      <article
        class="toast"
        class:error={toast.variant === 'error'}
        class:warn={toast.variant === 'warn'}
        class:info={toast.variant === 'info'}
        class:success={toast.variant === 'success'}
      >
        <div class="toast-body">
          <div class="toast-title">{toast.title}</div>
          {#if toast.message}
            <div class="toast-msg">{toast.message}</div>
          {/if}
        </div>
        <button
          class="toast-close"
          type="button"
          aria-label={t('common.close', 'Close')}
          onclick={() => onDismiss?.(toast.id)}
        >
          <span aria-hidden="true">×</span>
        </button>
      </article>
    {/each}
  </div>
{/if}
