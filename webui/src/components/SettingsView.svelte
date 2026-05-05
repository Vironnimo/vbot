<script>
  import { t } from '$lib/i18n.js';
</script>

<section
  class="placeholder-view placeholder-view--panel"
  aria-labelledby="settings-title"
>
  <div class="placeholder-view__card">
    <svg class="placeholder-view__icon" aria-hidden="true" viewBox="0 0 32 32">
      <circle cx="16" cy="16" r="4.5" />
      <path
        d="M16 3v4M16 25v4M3 16h4M25 16h4M6.8 6.8l2.8 2.8M22.4 22.4l2.8 2.8M25.2 6.8l-2.8 2.8M9.6 22.4l-2.8 2.8"
      />
    </svg>
    <p class="placeholder-view__status">
      {t('settings.serverStatus', 'Server status')}
    </p>
    <h2 id="settings-title">{t('settings.title', 'Settings')}</h2>
    <p>{t('settings.comingSoon', 'Settings controls are coming soon.')}</p>
    <p>
      {t('settings.subtitle', 'Runtime and WebUI settings will live here.')}
    </p>
  </div>
</section>
