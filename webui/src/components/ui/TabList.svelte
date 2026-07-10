<script>
  // Shared content-tab navigation. It owns both the ARIA tab contract and the
  // keyboard model, while callers keep panel content and active state. The
  // underline appearance serves view/section tabs; segmented serves compact
  // alternate representations of the same content.

  const generatedId = $props.id();

  let {
    items = [],
    value = '',
    ariaLabel = '',
    appearance = 'underline',
    density = 'default',
    idPrefix = '',
    class: className = '',
    onChange = () => {},
    ...rest
  } = $props();

  const APPEARANCES = new Set(['underline', 'segmented']);
  const DENSITIES = new Set(['default', 'compact']);

  let appearanceClass = $derived(
    APPEARANCES.has(appearance) ? appearance : 'underline',
  );
  let densityClass = $derived(DENSITIES.has(density) ? density : 'default');
  let resolvedIdPrefix = $derived(idPrefix || generatedId);
  let tabListClass = $derived(
    [
      'tab-list',
      `tab-list--${appearanceClass}`,
      `tab-list--${densityClass}`,
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  function itemToken(item) {
    return String(item.id).replace(/[^A-Za-z0-9_-]/g, '-');
  }

  function tabId(item) {
    return `${resolvedIdPrefix}-tab-${itemToken(item)}`;
  }

  function panelId(item) {
    return item.panelId || `${resolvedIdPrefix}-panel-${itemToken(item)}`;
  }

  function select(item) {
    if (!item.disabled && item.id !== value) {
      onChange(item.id, item);
    }
  }

  function handleKeydown(event) {
    const tabs = Array.from(
      event.currentTarget
        .closest('[role="tablist"]')
        ?.querySelectorAll('[role="tab"]:not(:disabled)') ?? [],
    );
    const currentIndex = tabs.indexOf(event.currentTarget);
    if (currentIndex < 0 || tabs.length === 0) {
      return;
    }

    let nextIndex;
    switch (event.key) {
      case 'ArrowRight':
        nextIndex = (currentIndex + 1) % tabs.length;
        break;
      case 'ArrowLeft':
        nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = tabs.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    tabs[nextIndex].focus();
    tabs[nextIndex].click();
  }
</script>

<div
  {...rest}
  class={tabListClass}
  role="tablist"
  aria-label={ariaLabel || undefined}
  aria-orientation="horizontal"
>
  {#each items as item (item.id)}
    <button
      type="button"
      class="tab-list__tab"
      class:tab-list__tab--active={item.id === value}
      role="tab"
      id={tabId(item)}
      aria-controls={panelId(item)}
      aria-selected={item.id === value}
      tabindex={item.id === value ? 0 : -1}
      disabled={item.disabled || undefined}
      onclick={() => select(item)}
      onkeydown={handleKeydown}
    >
      {item.label}
    </button>
  {/each}
</div>
