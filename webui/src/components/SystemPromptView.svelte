<script>
  import { t } from '$lib/i18n.js';
</script>

<section
  class="placeholder-view placeholder-view--panel"
  aria-labelledby="system-prompt-title"
>
  <div class="placeholder-view__card">
    <svg class="placeholder-view__icon" aria-hidden="true" viewBox="0 0 32 32">
      <rect x="4" y="4" width="24" height="24" rx="4" />
      <path d="M9 11h14M9 16h10M9 21h6" />
    </svg>
    <p class="placeholder-view__status">
      {t('systemPrompt.pieces', 'Prompt pieces')}
    </p>
    <h2 id="system-prompt-title">{t('systemPrompt.title', 'System Prompt')}</h2>
    <p>
      {t('systemPrompt.comingSoon', 'Editable prompt pieces are coming soon.')}
    </p>
    <p>
      {t(
        'systemPrompt.description',
        'The current minimal WebUI uses the server-managed system prompt.',
      )}
    </p>
  </div>
</section>
