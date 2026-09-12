import { tick } from 'svelte';
import { computePanelPosition } from '$lib/dropdownPanel.js';

export function createSessionMenus() {
  // Row-action state: which row's "…" menu is open, which row is being renamed
  // inline, the draft title, and any rename error. Only ever one of each at a
  // time — opening a menu or starting an edit on another row supersedes.
  let openMenuSessionId = $state(null);

  let menuTriggerElement = $state(null);

  let menuElement = $state(null);

  let menuStyle = $state('visibility: hidden;');

  let menuPlacement = $state('bottom');

  // Filter-dropdown state: the header button's portaled panel, positioned like
  // the row action menu.
  let filterMenuOpen = $state(false);

  let filterMenuTriggerElement = $state(null);

  let filterMenuElement = $state(null);

  let filterMenuStyle = $state('visibility: hidden;');

  let filterMenuPlacement = $state('bottom');

  const SESSION_ACTION_MENU_FALLBACK_WIDTH = 160;

  const SESSION_FILTER_MENU_WIDTH = 230;

  // -- filter dropdown -------------------------------------------------------
  const toggleFilterMenu = async (triggerElement) => {
    if (filterMenuOpen) {
      closeFilterMenu();
      return;
    }

    closeMenu();
    filterMenuOpen = true;
    filterMenuTriggerElement = triggerElement;
    filterMenuStyle = 'visibility: hidden;';
    await tick();
    updateFilterMenuPosition();
  };

  const closeFilterMenu = () => {
    filterMenuOpen = false;
    filterMenuTriggerElement = null;
    filterMenuElement = null;
    filterMenuStyle = 'visibility: hidden;';
    filterMenuPlacement = 'bottom';
  };

  const updateFilterMenuPosition = () => {
    if (!filterMenuOpen || !filterMenuTriggerElement || !filterMenuElement) {
      return;
    }

    const panelRect = filterMenuElement.getBoundingClientRect();
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(filterMenuTriggerElement, {
        contentHeight: filterMenuElement.scrollHeight || panelRect.height,
        panelWidth: panelRect.width || SESSION_FILTER_MENU_WIDTH,
        horizontalAlign: 'end',
      });

    filterMenuPlacement = placement;
    filterMenuStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  };

  const toggleMenu = async (sessionKey, triggerElement) => {
    if (openMenuSessionId === sessionKey) {
      closeMenu();
      return;
    }

    openMenuSessionId = sessionKey;
    menuTriggerElement = triggerElement;
    menuStyle = 'visibility: hidden;';
    await tick();
    updateMenuPosition();
  };

  const closeMenu = () => {
    openMenuSessionId = null;
    menuTriggerElement = null;
    menuElement = null;
    menuStyle = 'visibility: hidden;';
    menuPlacement = 'bottom';
  };

  const updateMenuPosition = () => {
    if (openMenuSessionId === null || !menuTriggerElement || !menuElement) {
      return;
    }

    const menuRect = menuElement.getBoundingClientRect();
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(menuTriggerElement, {
        contentHeight: menuElement.scrollHeight || menuRect.height,
        panelWidth: menuRect.width || SESSION_ACTION_MENU_FALLBACK_WIDTH,
        horizontalAlign: 'end',
      });

    menuPlacement = placement;
    menuStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  };
  return {
    get openMenuSessionId() {
      return openMenuSessionId;
    },
    set openMenuSessionId(value) {
      openMenuSessionId = value;
    },
    get menuElement() {
      return menuElement;
    },
    set menuElement(value) {
      menuElement = value;
    },
    get menuStyle() {
      return menuStyle;
    },
    set menuStyle(value) {
      menuStyle = value;
    },
    get menuPlacement() {
      return menuPlacement;
    },
    set menuPlacement(value) {
      menuPlacement = value;
    },
    get filterMenuOpen() {
      return filterMenuOpen;
    },
    set filterMenuOpen(value) {
      filterMenuOpen = value;
    },
    get filterMenuElement() {
      return filterMenuElement;
    },
    set filterMenuElement(value) {
      filterMenuElement = value;
    },
    get filterMenuStyle() {
      return filterMenuStyle;
    },
    set filterMenuStyle(value) {
      filterMenuStyle = value;
    },
    get filterMenuPlacement() {
      return filterMenuPlacement;
    },
    set filterMenuPlacement(value) {
      filterMenuPlacement = value;
    },
    get toggleFilterMenu() {
      return toggleFilterMenu;
    },
    get closeFilterMenu() {
      return closeFilterMenu;
    },
    get toggleMenu() {
      return toggleMenu;
    },
    get closeMenu() {
      return closeMenu;
    },
  };
}
