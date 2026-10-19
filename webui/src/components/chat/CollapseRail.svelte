<script>
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  // The side line of an open disclosure body (Working group, Thinking, Tool
  // details) as a way to close it from anywhere in a long body: clicking the
  // line, or the knob that follows the viewport along it while the pointer is
  // over the body, closes the enclosing <details> and brings its summary back
  // into view. The summary stays the keyboard and screen-reader control, so
  // the rail is pointer-only. The host body is a `.collapse-host`
  // (styles/chat-timeline/entries.css), whose `--collapse-rail-line` names
  // its border width.
  let rail = $state(null);

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
    const details = rail?.closest('details');
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
  bind:this={rail}
  type="button"
  class="collapse-rail"
  tabindex="-1"
  aria-hidden="true"
  use:tooltip={t('chat.collapseBlock')}
  onclick={collapse}
>
  <span class="collapse-rail__knob">
    <svg viewBox="0 0 16 16" width="10" height="10">
      <path d="m4 10 4-4 4 4" />
    </svg>
  </span>
</button>
