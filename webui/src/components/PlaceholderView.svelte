<script>
  import { t } from '$lib/i18n.js';

  let { view } = $props();
</script>

<section class="placeholder-view" aria-labelledby="placeholder-view-title">
  <p class="placeholder-view__status">
    {t('placeholders.status', 'Foundation placeholder')}
  </p>
  <h2 id="placeholder-view-title">{t(view.labelKey, view.labelFallback)}</h2>
  <p>{t(view.descriptionKey, view.descriptionFallback)}</p>
  <div
    class="placeholder-view__panel"
    aria-label={t('placeholders.previewLabel', 'Upcoming view preview')}
  >
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
    <span aria-hidden="true"></span>
  </div>
</section>
