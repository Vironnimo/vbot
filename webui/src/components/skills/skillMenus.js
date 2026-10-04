// Context menus of the Skills manager's rows. Each builder returns the
// `{ label, items }` part of a ContextMenu value (components/ui/ContextMenu.svelte);
// SkillsView (and, for Agent rows, the Agent editor) adds the anchor and
// supplies the actions:
//
//   open(entry), edit(entry), copyName(name), setDisabled(entry, disabled),
//   remove(entry), restore(item), purge(item)
//
// `entry` is a skill.inventory package, `item` one of its `archived`
// packages. Agent and Project rows pass the package they list (null for a
// saved name without one) and a `toggle(on)` that applies the row's own rule.
import { t } from '$lib/i18n.js';

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

function everywhereItem(entry, actions) {
  return entry.disabled
    ? {
        id: 'turn-on-everywhere',
        label: t('skills.menu.turnOnEverywhere'),
        group: 'everywhere',
        onSelect: () => actions.setDisabled(entry, false),
      }
    : {
        id: 'turn-off-everywhere',
        label: t('skills.detail.turnOff'),
        group: 'everywhere',
        onSelect: () => actions.setDisabled(entry, true),
      };
}

/** A library row: Open, Edit, Copy name | Turn off/on everywhere | Delete. */
export function libraryRowMenu(entry, actions) {
  const items = packageItems(entry, actions, t('skills.menu.open'));
  if (entry.editable_scope)
    items.push({
      id: 'edit',
      label: t('skills.editInstructions'),
      group: 'package',
      onSelect: () => actions.edit(entry),
    });
  items.push(copyItem(entry.name, actions), everywhereItem(entry, actions));
  if (entry.editable_scope)
    items.push({
      id: 'delete',
      label: t('skills.menu.delete'),
      danger: true,
      group: 'delete',
      onSelect: () => actions.remove(entry),
    });
  return { label: t('skills.menu.label', { name: entry.name }), items };
}

/**
 * A row of an Agent's skill selection: turn it on or off for the Agent (not
 * for a Project grant), Open skill, Edit instructions (the Agent's own
 * Skills), Copy name | Turn off/on everywhere | Delete... (the Agent's own
 * Skills). Shared and global Skills offer no Edit or Delete here: from an
 * Agent's perspective they would change another owner's or every Agent's
 * Skill.
 */
export function agentRowMenu(item, { agentName, entry, toggle }, actions) {
  const own = Boolean(item.own && entry?.editable_scope);
  const items = [
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
    ...packageItems(entry, actions, t('skills.menu.openSkill')),
  ];
  if (own)
    items.push({
      id: 'edit',
      label: t('skills.editInstructions'),
      group: 'package',
      onSelect: () => actions.edit(entry),
    });
  items.push(copyItem(item.name, actions));
  if (entry) items.push(everywhereItem(entry, actions));
  if (own)
    items.push({
      id: 'delete',
      label: t('skills.menu.delete'),
      danger: true,
      group: 'delete',
      onSelect: () => actions.remove(entry),
    });
  return { label: t('skills.menu.label', { name: item.name }), items };
}

/** A row of a Project's skill selection: (de)activate, Open skill, Copy name. */
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
      ...packageItems(entry, actions, t('skills.menu.openSkill')),
      copyItem(item.name, actions),
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
