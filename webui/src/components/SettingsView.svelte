<script>
  import { t } from '$lib/i18n.js';
</script>

<section class="placeholder-view" aria-labelledby="settings-title">
  <p class="placeholder-view__status">
    {t('settings.serverStatus', 'Server status')}
  </p>
  <h2 id="settings-title">{t('settings.title', 'Settings')}</h2>
  <p>{t('settings.comingSoon', 'Settings controls are coming soon.')}</p>
  <div
    class="placeholder-view__panel"
    aria-label={t('placeholders.previewLabel', 'Upcoming view preview')}
  >
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
  </div>
</section>
