<script>
  import { t } from '$lib/i18n.js';
</script>

<section
  class="system-placeholder view active"
  aria-labelledby="system-prompt-title"
>
  <div class="system-placeholder__content">
    <svg
      class="system-placeholder__icon"
      viewBox="0 0 32 32"
      aria-hidden="true"
    >
      <rect x="4" y="4" width="24" height="24" rx="4" />
      <path d="M9 11h14M9 16h10M9 21h6" />
    </svg>
    <h2 id="system-prompt-title">
      {t('systemPrompt.title', 'System Prompt')}
    </h2>
    <p>
      {t(
        'systemPrompt.prototypePlaceholder',
        'Placeholder — coming in a later phase.',
      )}
    </p>
  </div>
</section>

<style>
  .system-placeholder {
    display: flex;
    min-height: 0;
    flex: 1;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    color: var(--text-lo);
  }

  .system-placeholder__content {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    padding: 32px;
    text-align: center;
  }

  .system-placeholder__icon {
    width: 38px;
    height: 38px;
  }

  h2 {
    color: var(--text-med);
    font-size: 14px;
    font-weight: 500;
    line-height: 1.3;
  }

  p {
    color: var(--text-lo);
    font-size: 13px;
    line-height: 1.5;
  }
</style>
