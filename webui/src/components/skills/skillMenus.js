// Context menus of the Skills manager's rows. Each builder returns the
// `{ label, items }` part of a ContextMenu value (components/ui/ContextMenu.svelte);
// SkillsView (and, for their rows, the Agent and Project editors) adds the
// anchor and supplies the actions:
//
//   open(entry), edit(entry), copyName(name), setDisabled(entry, disabled),
//   remove(entry), restore(item), purge(item)
//
// `entry` is a skill.inventory package, `item` one of its `archived`
// packages. Agent and Project rows pass the package they list (null for a
// saved name without one) and a `toggle(on)` that applies the row's own rule.
import { t } from '$lib/i18n.js';
import { skillReadOnlyHint } from './skillsView.js';

function packageItems(entry, actions, openLabel) {
  const items = [];
  if (entry)
    items.push({
      id: 'open',
      label: openLabel,
      group: 'package',
      onSelect: () => actions.open(entry),
    });
  return items;
}

function copyItem(name, actions) {
  return {
    id: 'copy-name',
    label: t('skills.menu.copyName'),
    group: 'package',
    onSelect: () => actions.copyName(name),
  };
}

// The package's own off switch, for every Agent and Project.
function switchItem(entry, actions) {
  return entry.disabled
    ? {
        id: 'turn-on',
        label: t('skills.menu.turnOn'),
        group: 'switch',
        onSelect: () => actions.setDisabled(entry, false),
      }
    : {
        id: 'turn-off',
        label: t('skills.detail.turnOff'),
        group: 'switch',
        onSelect: () => actions.setDisabled(entry, true),
      };
}

// Edit and Delete are offered for every package wherever it is listed, so a
// Skill can be managed where it is seen. A package that cannot be edited
// (bundled, from an Extension or skill folder, in a Project repository, or
// unloadable) keeps both items disabled with the reason as their hint.
function editItem(entry, actions) {
  return {
    id: 'edit',
    label: t('skills.editInstructions'),
    group: 'package',
    disabled: !entry.editable_scope,
    hint: entry.editable_scope ? undefined : skillReadOnlyHint(entry),
    onSelect: () => actions.edit(entry),
  };
}

function deleteItem(entry, actions) {
  return {
    id: 'delete',
    label: t('skills.menu.delete'),
    danger: true,
    group: 'delete',
    disabled: !entry.editable_scope,
    hint: entry.editable_scope ? undefined : skillReadOnlyHint(entry),
    onSelect: () => actions.remove(entry),
  };
}

// The package part shared by every row with a package: Open, Edit, Copy
// name | Turn this skill off/on | Delete...
function packageMenuItems(entry, name, actions, openLabel) {
  if (!entry) return [copyItem(name, actions)];
  return [
    ...packageItems(entry, actions, openLabel),
    editItem(entry, actions),
    copyItem(name, actions),
    switchItem(entry, actions),
    deleteItem(entry, actions),
  ];
}

/** A library row: Open, Edit, Copy name | Turn this skill off/on | Delete. */
export function libraryRowMenu(entry, actions) {
  return {
    label: t('skills.menu.label', { name: entry.name }),
    items: packageMenuItems(entry, entry.name, actions, t('skills.menu.open')),
  };
}

/**
 * A row of an Agent's skill selection: turn it on or off for the Agent (not
 * for a Project grant), then the package items. Edit and Delete act on the
 * package itself, also for a global or shared Skill; the delete confirmation
 * says who loses it.
 */
export function agentRowMenu(item, { agentName, entry, toggle }, actions) {
  return {
    label: t('skills.menu.label', { name: item.name }),
    items: [
      {
        id: 'toggle',
        label: item.allowed
          ? t('skills.menu.turnOffFor', { name: agentName })
          : t('skills.menu.turnOnFor', { name: agentName }),
        group: 'package',
        disabled: item.locked,
        hint: item.locked
          ? t('skills.menu.managedIn', { name: item.lockedBy })
          : undefined,
        onSelect: () => toggle(!item.allowed),
      },
      ...packageMenuItems(
        entry,
        item.name,
        actions,
        t('skills.menu.openSkill'),
      ),
    ],
  };
}

/** A row of a Project's skill selection: (de)activate, then the package items. */
export function projectRowMenu(item, { projectName, entry, toggle }, actions) {
  return {
    label: t('skills.menu.label', { name: item.name }),
    items: [
      {
        id: 'toggle',
        label: item.allowed
          ? t('skills.menu.deactivateIn', { name: projectName })
          : t('skills.menu.activateIn', { name: projectName }),
        group: 'package',
        onSelect: () => toggle(!item.allowed),
      },
      ...packageMenuItems(
        entry,
        item.name,
        actions,
        t('skills.menu.openSkill'),
      ),
    ],
  };
}

/** An archived row: Restore, Copy name | Delete permanently... */
export function archivedRowMenu(item, actions) {
  return {
    label: t('skills.menu.label', { name: item.name }),
    items: [
      {
        id: 'restore',
        label: t('skills.archived.restore'),
        group: 'package',
        onSelect: () => actions.restore(item),
      },
      copyItem(item.name, actions),
      {
        id: 'purge',
        label: t('skills.archived.purge'),
        danger: true,
        group: 'delete',
        onSelect: () => actions.purge(item),
      },
    ],
  };
}
