import { tick } from 'svelte';
import { computePanelPosition } from '$lib/dropdownPanel.js';

// The filter dropdown of the drawer header: a portaled panel of toggles,
// positioned below its trigger. Row actions use the shared ContextMenu.
export function createSessionMenus() {
  let filterMenuOpen = $state(false);

  let filterMenuTriggerElement = $state(null);

  let filterMenuElement = $state(null);

  let filterMenuStyle = $state('visibility: hidden;');

  let filterMenuPlacement = $state('bottom');

  const SESSION_FILTER_MENU_WIDTH = 230;

  const toggleFilterMenu = async (triggerElement) => {
    if (filterMenuOpen) {
      closeFilterMenu();
      return;
    }

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
  return {
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
  };
}
