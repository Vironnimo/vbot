<script>
  import { t } from '$lib/i18n.js';
</script>

<section class="placeholder-view" aria-labelledby="system-prompt-title">
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
  <div
    class="placeholder-view__panel"
    aria-label={t('placeholders.previewLabel', 'Upcoming view preview')}
  >
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
  </div>
</section>
