<script>
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  // The summary handles keyboard and screen-reader interaction. The side line
  // lets pointer users close the disclosure from anywhere along its body.
  function scrollContainer(element) {
    for (let node = element.parentElement; node; node = node.parentElement) {
      const { overflowY } = getComputedStyle(node);
      if (overflowY === 'auto' || overflowY === 'scroll') return node;
    }
    return null;
  }

  function collapse(event) {
    event.preventDefault();
    event.stopPropagation();
    const details = event.currentTarget.closest('details');
    if (!details) return;
    const summary = details.querySelector(':scope > summary');
    const scroller = scrollContainer(details);
    // Closing fires the disclosure's own toggle handling, so its open state
    // and remembered view state follow as if the summary had been clicked.
    details.open = false;
    if (!summary || !scroller) return;
    const offset =
      summary.getBoundingClientRect().top -
      scroller.getBoundingClientRect().top;
    if (offset < 0) scroller.scrollTop += offset - 8;
  }
</script>

<button
  type="button"
  class="collapse-rail"
  tabindex="-1"
  aria-hidden="true"
  use:tooltip={{ text: t('chat.collapseBlock'), placement: 'pointer' }}
  onclick={collapse}
></button>
