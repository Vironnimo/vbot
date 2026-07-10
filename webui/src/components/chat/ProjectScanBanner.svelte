<script>
  // Narrow, non-blocking banner shown above the project team bar when the
  // project's scan was not clean (`report.clean === false`). It is purely
  // informational — chatting with the project's agents stays available — and
  // offers a callback-prop link into the Projects tab where the full report
  // and re-point/manage actions live. No event dispatcher (webui convention).
  //
  // It renders nothing when the report is clean or absent; the parent decides
  // when to mount it, but the component is defensively self-guarding too.

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import { t } from '$lib/i18n.js';

  let { report = null, onNavigateToProjects = () => {} } = $props();

  let visible = $derived(report?.clean === false);
  let findingCount = $derived(
    Number.isFinite(report?.findingCount) ? report.findingCount : 0,
  );
  let message = $derived(
    findingCount > 0
      ? t(
          'chat.project.scanBannerCount',
          'This project’s scan found {count} issues. Some agents may not work as expected.',
          { count: findingCount },
        )
      : t(
          'chat.project.scanBanner',
          'This project’s scan found issues. Some agents may not work as expected.',
        ),
  );
</script>

{#if visible}
  <Banner
    variant="warn"
    class="project-scan-banner"
    role="status"
    aria-live="polite"
  >
    <span class="project-scan-banner__message">{message}</span>
    <Button
      variant="tertiary"
      class="project-scan-banner__link"
      onClick={() => onNavigateToProjects()}
    >
      {t('chat.project.scanBannerLink', 'Review in Projects')}
    </Button>
  </Banner>
{/if}

<style>
  :global(.project-scan-banner) {
    flex-shrink: 0;
    width: 100%;
    max-width: var(--chat-measure);
    margin-inline: auto;
    padding: 7px 14px;
  }

  .project-scan-banner__message {
    min-width: 0;
    color: var(--text-med);
    font-size: 12px;
    line-height: 1.4;
  }

  :global(.project-scan-banner__link) {
    flex-shrink: 0;
    color: var(--accent);
    text-decoration: underline;
  }

  :global(.project-scan-banner__link:hover),
  :global(.project-scan-banner__link:focus-visible) {
    color: var(--accent);
    text-decoration: none;
  }

  @media (max-width: 640px) {
    :global(.project-scan-banner) {
      align-items: flex-start;
      flex-direction: column;
      gap: 6px;
    }
  }
</style>
