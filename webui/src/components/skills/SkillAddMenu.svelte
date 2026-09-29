<script>
  // The one "Add skills" entry point: installing a package, writing a new
  // Skill, or connecting Skill folders. A small menu button with the usual
  // keyboard behavior (arrows, Home/End, Escape returns focus, Tab closes).
  import { tick } from 'svelte';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import Button from '../ui/Button.svelte';

  const noop = () => {};
  const uid = $props.id();
  const menuId = `${uid}-menu`;

  let {
    disabled = false,
    onInstall = noop,
    onCreate = noop,
    onFolders = noop,
  } = $props();

  let open = $state(false);
  let anchor = $state();
  let menu = $state();
  let menuStyle = $state('visibility: hidden');

  let items = $derived([
    { id: 'install', label: t('skills.add.install'), run: onInstall },
    { id: 'create', label: t('skills.add.create'), run: onCreate },
    { id: 'folders', label: t('skills.add.folders'), run: onFolders },
  ]);

  function trigger() {
    return anchor?.querySelector('button');
  }

  export function focus() {
    trigger()?.focus();
  }

  async function openMenu() {
    open = true;
    menuStyle = 'visibility: hidden';
    await tick();
    const button = trigger();
    if (!open || !menu || !button) return;
    const position = computePanelPosition(button, {
      panelWidth: 240,
      contentHeight: menu.scrollHeight,
    });
    menuStyle = `left: ${position.left}px; ${position.verticalRule}; width: ${position.width}px; max-height: ${position.optionsMaxHeight}px`;
    menu.querySelector('[role="menuitem"]')?.focus();
  }

  function closeMenu(restoreFocus = false) {
    if (!open) return;
    open = false;
    if (restoreFocus) trigger()?.focus();
  }

  // Back and Forward close the open menu before navigating.
  const shell = useNavigation();
  $effect(() => {
    if (!open) return;
    return shell?.registerLayer({ close: () => closeMenu(true) });
  });

  function choose(item) {
    closeMenu(true);
    item.run();
  }

  function menuKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === 'Tab') {
      closeMenu();
      return;
    }
    const buttons = [...menu.querySelectorAll('[role="menuitem"]')];
    const current = buttons.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (current + 1) % buttons.length;
    else if (event.key === 'ArrowUp')
      next = (current - 1 + buttons.length) % buttons.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = buttons.length - 1;
    else return;
    event.preventDefault();
    buttons[next]?.focus();
  }
</script>

<svelte:window onresize={() => closeMenu()} />
<svelte:document
  onpointerdown={(event) => {
    if (
      open &&
      !menu?.contains(event.target) &&
      !anchor?.contains(event.target)
    )
      closeMenu();
  }}
/>

<span class="skills-add" bind:this={anchor}>
  <Button
    variant="secondary"
    icon
    ariaLabel={t('skills.addSkills')}
    tooltip={t('skills.addSkills')}
    aria-haspopup="menu"
    aria-expanded={open}
    aria-controls={open ? menuId : undefined}
    {disabled}
    onClick={() => (open ? closeMenu(true) : openMenu())}
  >
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      aria-hidden="true"><path d="M9 3v12M3 9h12" /></svg
    >
  </Button>
</span>

{#if open}
  <div
    bind:this={menu}
    use:portal
    class="skills-add-menu"
    id={menuId}
    role="menu"
    tabindex="-1"
    aria-label={t('skills.addSkills')}
    data-positioning="fixed"
    style={menuStyle}
    onkeydown={menuKeydown}
  >
    {#each items as item (item.id)}
      <button
        type="button"
        class="skills-add-menu__item"
        role="menuitem"
        tabindex="-1"
        onclick={() => choose(item)}>{item.label}</button
      >
    {/each}
  </div>
{/if}
