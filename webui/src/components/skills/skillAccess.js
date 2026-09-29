import { t } from '$lib/i18n.js';

import {
  agentDisplayName,
  skillCopyLabel,
  skillDiagnosticLines,
  skillSourceDetail,
} from './skillsView.js';

// Skill access as the manager edits it. The server owns precedence and the
// effective grants (skill.inventory projects them per Agent and Project); the
// browser only computes allowlist set changes and presents the projection.

export const ALL_SKILLS = '*';

const GRANTED = new Set(['own', 'project', 'allowed']);

function names(value) {
  return Array.isArray(value)
    ? value.filter((name) => typeof name === 'string' && name)
    : [];
}

function unique(list) {
  return [...new Set(list)];
}

function sameList(left, right) {
  return (
    left.length === right.length &&
    left.every((name, index) => name === right[index])
  );
}

// ── Agent allowlists ────────────────────────────────────────────────────────

/** The allowlist pair of an Agent record, projection entry or form draft. */
export function skillAccessOf(record) {
  return {
    allowed: names(record?.allowed_skills),
    excluded: names(record?.excluded_skills),
  };
}

/** True when Skills added later are allowed automatically ("*"). */
export function isAllMode(access) {
  return access.allowed.includes(ALL_SKILLS);
}

/**
 * Only a Project grant is fixed: the Project decides it. Own Skills and every
 * other Skill follow the Agent's allowlist pair.
 */
export function isAllowlistGoverned(grant) {
  return grant !== 'project';
}

/**
 * Whether the allowlist pair grants a Skill name. An own Skill is on unless
 * excluded, in either mode; any other name also needs the wildcard or a list
 * entry.
 */
export function isDraftGranted(access, name, own = false) {
  if (access.excluded.includes(name)) return false;
  return own || isAllMode(access) || access.allowed.includes(name);
}

/**
 * Turns Skill names on or off. Own Skills (`own`) and every name in "all"
 * mode are turned off by excluding and on by releasing the exclusion. With a
 * fixed selection other names are added to or removed from the list (and
 * released from the exclusions so the grant takes effect). Unknown names
 * already saved are kept.
 */
export function toggleSkills(access, skillNames, on, own = false) {
  const targets = new Set(skillNames);
  if (own || isAllMode(access)) {
    return {
      allowed: [...access.allowed],
      excluded: on
        ? access.excluded.filter((name) => !targets.has(name))
        : unique([...access.excluded, ...skillNames]),
    };
  }
  if (on) {
    return {
      allowed: unique([...access.allowed, ...skillNames]),
      excluded: access.excluded.filter((name) => !targets.has(name)),
    };
  }
  return {
    allowed: access.allowed.filter((name) => !targets.has(name)),
    excluded: [...access.excluded],
  };
}

export function toggleSkill(access, name, on, own = false) {
  return toggleSkills(access, [name], on, own);
}

/**
 * Switches between "all" (new Skills are added automatically) and a fixed
 * selection without changing which of the governed names are granted now.
 * `governedNames` are the listed non-own Skills; exclusions of own Skills
 * (`ownNames`) carry over unchanged.
 */
export function setAutoAdd(access, governedNames, on, ownNames = []) {
  if (on === isAllMode(access)) return access;
  const governed = new Set(governedNames);
  if (on) {
    return {
      allowed: [ALL_SKILLS],
      excluded: unique([
        ...governedNames.filter((name) => !isDraftGranted(access, name)),
        ...access.excluded.filter((name) => !governed.has(name)),
      ]),
    };
  }
  const own = new Set(ownNames);
  return {
    allowed: unique([
      ...governedNames.filter((name) => isDraftGranted(access, name)),
      ...access.allowed.filter(
        (name) =>
          name !== ALL_SKILLS &&
          !governed.has(name) &&
          !own.has(name) &&
          !access.excluded.includes(name),
      ),
    ]),
    excluded: access.excluded.filter((name) => own.has(name)),
  };
}

/** The Agent update fields that differ between two allowlist pairs. */
export function accessPatch(before, after) {
  const patch = {};
  if (!sameList(before.allowed, after.allowed))
    patch.allowed_skills = after.allowed;
  if (!sameList(before.excluded, after.excluded))
    patch.excluded_skills = after.excluded;
  return patch;
}

// ── Project lists ───────────────────────────────────────────────────────────

const PROJECT_FIELDS = {
  project: 'skills_project_disabled',
  bundled: 'skills_bundled_enabled',
  global: 'skills_global_enabled',
};

/**
 * The single Project field that activates or deactivates Skills of one pool:
 * Project Skills are active unless disabled; bundled and global ones only when
 * enabled.
 */
export function projectSkillPatch(project, source, skillNames, active) {
  const field = PROJECT_FIELDS[source];
  if (!field) return {};
  const current = names(project?.[field]);
  const listed = source === 'project' ? !active : active;
  const targets = new Set(skillNames);
  return {
    [field]: listed
      ? unique([...current, ...skillNames])
      : current.filter((name) => !targets.has(name)),
  };
}

// ── Projection lookups ──────────────────────────────────────────────────────

function entryIndex(inventory) {
  return new Map(inventory.map((entry) => [entry.id, entry]));
}

function projectName(projectId, projects) {
  return (
    projects.find((project) => project.project_id === projectId)?.name ||
    projectId ||
    ''
  );
}

// The complete list behind a "first +N more" state, for the row's tooltip.
function missingRows(entry) {
  const missing = names(entry?.missing);
  return missing.length > 1
    ? [
        {
          label: t('skills.details.missing'),
          value: missing.join('\n'),
          mono: true,
        },
      ]
    : [];
}

function missingText(entry) {
  const missing = names(entry?.missing);
  if (!missing.length) return t('skills.access.requirementsMissing');
  return missing.length > 1
    ? t('skills.access.missingMore', {
        first: missing[0],
        count: missing.length - 1,
      })
    : missing[0];
}

/** Rows a new Agent would see: the global pool shadows bundled Skills. */
function draftAgentRows(inventory) {
  const rows = new Map();
  for (const origin of ['global', 'bundled']) {
    for (const entry of inventory) {
      if (
        entry.origin !== origin ||
        entry.disabled ||
        entry.status === 'invalid' ||
        rows.has(entry.name)
      )
        continue;
      rows.set(entry.name, {
        name: entry.name,
        package_id: entry.id,
        grant: 'not_selected',
        available: entry.status === 'available',
      });
    }
  }
  return [...rows.values()].sort((left, right) =>
    left.name.localeCompare(right.name),
  );
}

// ── Agent perspective ───────────────────────────────────────────────────────

const AGENT_GROUPS = ['own', 'project', 'shared', 'global', 'bundled'];

function agentGroupOf(row, entry, agentId) {
  if (row.own) return 'own';
  if (row.grant === 'project') return 'project';
  if (entry?.owner_id && entry.owner_id !== agentId) return 'shared';
  if (entry?.origin === 'bundled') return 'bundled';
  return 'global';
}

function agentGroupTitle(group, rootProject) {
  switch (group) {
    case 'own':
      return t('skills.panel.ownGroup');
    case 'project':
      return t('skills.panel.projectGroup', { name: rootProject });
    case 'shared':
      return t('skills.panel.sharedGroup');
    case 'bundled':
      return t('skills.panel.bundledGroup');
    case 'saved':
      return t('skills.panel.savedGroup');
    default:
      return t('skills.panel.globalGroup');
  }
}

function agentGroupAllLabel(group, rootProject) {
  return t('skills.panel.allInGroup', {
    group: agentGroupTitle(group, rootProject),
  });
}

/**
 * The Skills one Agent can see, grouped by why it gets them, with the checked
 * state taken from the (possibly unsaved) allowlist pair. `agent` is the
 * inventory projection entry, or null for an Agent that does not exist yet.
 * Saved names the Agent cannot currently see stay listed so they are never
 * dropped silently. A Project grant is locked: `lockedReason` explains it,
 * `lockedBy` names the Project.
 */
export function agentSkillView(
  agent,
  access,
  {
    inventory = [],
    projects = [],
    agents = [],
    agentId = agent?.id ?? '',
  } = {},
) {
  const byId = entryIndex(inventory);
  const rows = agent ? (agent.skills ?? []) : draftAgentRows(inventory);
  const rootProject = projectName(agent?.root_project_id, projects);
  const groups = new Map(AGENT_GROUPS.map((group) => [group, []]));
  const governed = [];
  const own = [];
  for (const row of rows) {
    const entry = byId.get(row.package_id);
    const group = agentGroupOf(row, entry, agentId);
    const locked = !isAllowlistGoverned(row.grant);
    if (row.own) own.push(row.name);
    else if (!locked) governed.push(row.name);
    const allowed = locked || isDraftGranted(access, row.name, row.own);
    let state = null;
    if (!row.available) state = { text: missingText(entry), tone: 'warn' };
    else if (group === 'shared')
      state = {
        text: t('skills.panel.sharedBy', {
          name: agentDisplayName(entry.owner_id, agents),
        }),
      };
    // An own Skill the Project also grants: say why it cannot be turned off.
    else if (locked && group === 'own')
      state = { text: t('skills.access.viaProject', { name: rootProject }) };
    groups.get(group).push({
      key: row.name,
      name: row.name,
      packageId: row.package_id,
      own: Boolean(row.own),
      allowed,
      locked,
      lockedReason: locked
        ? t('skills.panel.lockedByProject', { name: rootProject })
        : '',
      lockedBy: locked ? rootProject : '',
      detail: entry?.description || '',
      detailRows: row.available ? [] : missingRows(entry),
      state,
    });
  }
  const known = new Set(rows.map((row) => row.name));
  const saved = isAllMode(access)
    ? []
    : access.allowed
        .filter((name) => !known.has(name))
        .map((name) => ({
          key: name,
          name,
          packageId: null,
          own: false,
          allowed: true,
          locked: false,
          lockedReason: '',
          lockedBy: '',
          detail: t('skills.panel.savedDetail'),
          state: null,
        }));
  const result = [...groups]
    .filter(([, items]) => items.length)
    .map(([id, items]) => ({
      id,
      title: agentGroupTitle(id, rootProject),
      allLabel: agentGroupAllLabel(id, rootProject),
      items,
    }));
  if (saved.length)
    result.push({
      id: 'saved',
      title: agentGroupTitle('saved'),
      allLabel: agentGroupAllLabel('saved'),
      items: saved,
    });
  // Saved names the Agent cannot see are listed but grant nothing.
  const listed = result
    .filter((group) => group.id !== 'saved')
    .flatMap((group) => group.items);
  return {
    groups: result,
    governed,
    own,
    active: listed.filter((item) => item.allowed).length,
    total: listed.length,
    autoAdd: isAllMode(access),
  };
}

// ── Project perspective ─────────────────────────────────────────────────────

const PROJECT_GROUPS = ['project', 'bundled', 'global'];

function projectGroupTitle(source) {
  if (source === 'project') return t('skills.panel.projectPoolGroup');
  if (source === 'bundled') return t('skills.panel.bundledGroup');
  return t('skills.panel.globalGroup');
}

/**
 * Project Skill pools as selection groups. `sections` maps each pool
 * (project, bundled, global) to rows `{name, description, active, packageId}`.
 */
export function projectSkillView(sections) {
  const groups = PROJECT_GROUPS.map((source) => ({
    id: source,
    title: projectGroupTitle(source),
    allLabel: t('skills.panel.allInGroup', {
      group: projectGroupTitle(source),
    }),
    items: (sections?.[source] ?? []).map((row) => ({
      key: row.name,
      name: row.name,
      packageId: row.packageId ?? null,
      allowed: Boolean(row.active),
      locked: false,
      lockedReason: '',
      detail: row.description || '',
      state: null,
    })),
  })).filter((group) => group.items.length);
  const listed = groups.flatMap((group) => group.items);
  return {
    groups,
    active: listed.filter((item) => item.allowed).length,
    total: listed.length,
  };
}

/** A Project projection as the pool sections of `projectSkillView`. */
export function projectionSkillSections(project, inventory) {
  const byId = entryIndex(inventory);
  const sections = { project: [], bundled: [], global: [] };
  for (const row of project?.skills ?? []) {
    sections[row.source]?.push({
      name: row.name,
      description: byId.get(row.package_id)?.description || '',
      active: row.active,
      packageId: row.package_id,
    });
  }
  return sections;
}

// ── Skill perspective ───────────────────────────────────────────────────────

function shadowState(row, { byId, agents, projects }) {
  const winner = byId.get(row.package_id);
  return {
    text: winner
      ? t('skills.access.usesOther', {
          copy: skillCopyLabel(winner, agents, projects),
        })
      : t('skills.access.usesAnother'),
  };
}

function agentAccessRow(entry, agent, context) {
  const { projects } = context;
  const label = agent.name || agent.id;
  const row = (agent.skills ?? []).find((item) => item.name === entry.name);
  const base = {
    key: agent.id,
    name: label,
    agentId: agent.id,
    allowed: false,
    locked: true,
    kind: 'locked',
    state: null,
    action: null,
  };
  const isOwner = entry.owner_id && entry.owner_id === agent.id;
  if (entry.disabled)
    return isOwner
      ? { ...base, state: { text: t('skills.access.owner') } }
      : base;
  if (entry.owner_id && !isOwner) {
    const shared = names(entry.shared_with).includes(agent.id);
    const effective = Boolean(
      row && row.package_id === entry.id && GRANTED.has(row.grant),
    );
    const shareRow = {
      ...base,
      allowed: shared,
      counted: shared && effective,
      locked: false,
      kind: 'share',
    };
    if (!shared) return shareRow;
    if (!row)
      return {
        ...shareRow,
        state: { text: t('skills.access.notVisible'), tone: 'warn' },
      };
    if (row.package_id !== entry.id)
      return {
        ...shareRow,
        state: { ...shadowState(row, context), tone: 'warn' },
      };
    if (!GRANTED.has(row.grant))
      return {
        ...shareRow,
        state: { text: t('skills.access.blocked'), tone: 'warn' },
        action: {
          label: t('skills.access.allow'),
          ariaLabel: t('skills.access.allowFor', {
            name: label,
            skill: entry.name,
          }),
        },
      };
    if (!row.available)
      return { ...shareRow, state: { text: missingText(entry), tone: 'warn' } };
    return shareRow;
  }
  if (!row) {
    let text = t('skills.access.notVisible');
    if (entry.status === 'invalid') text = t('skills.access.notLoadable');
    else if (entry.project_id)
      text = t('skills.access.projectOnly', {
        name: projectName(entry.project_id, projects),
      });
    return { ...base, state: { text } };
  }
  if (row.package_id !== entry.id)
    return { ...base, state: shadowState(row, context) };
  const granted = GRANTED.has(row.grant);
  const rootProject = projectName(agent.root_project_id, projects);
  const locked = !isAllowlistGoverned(row.grant);
  let state = null;
  if (granted && !row.available)
    state = { text: missingText(entry), tone: 'warn' };
  else if (locked)
    state = { text: t('skills.access.viaProject', { name: rootProject }) };
  else if (row.own) state = { text: t('skills.access.owner') };
  else if (row.grant === 'excluded')
    state = { text: t('skills.access.excluded') };
  let kind = 'grant';
  if (locked) kind = 'locked';
  else if (row.own) kind = 'own';
  return {
    ...base,
    allowed: granted,
    locked,
    lockedReason: locked
      ? t('skills.panel.lockedByProject', { name: rootProject })
      : '',
    kind,
    state,
  };
}

function projectAccessRow(entry, project, context) {
  const row = (project.skills ?? []).find((item) => item.name === entry.name);
  const base = {
    key: project.project_id,
    name: project.name || project.project_id,
    projectId: project.project_id,
    allowed: false,
    locked: true,
    kind: 'locked',
    source: row?.source ?? null,
    state: null,
    action: null,
  };
  if (entry.disabled) return base;
  if (!row)
    return {
      ...base,
      state: {
        text:
          entry.status === 'invalid'
            ? t('skills.access.notLoadable')
            : t('skills.access.notInProject'),
      },
    };
  if (row.package_id !== entry.id)
    return { ...base, state: shadowState(row, context) };
  return { ...base, allowed: row.active, locked: false, kind: 'project' };
}

/**
 * Who gets one Skill package: a row per Identity Agent and, for packages a
 * Project can activate, per Project. Rows of a private package's other Agents
 * are sharing controls.
 */
export function skillAccessView(
  entry,
  { agents = [], projects = [], inventory = [] } = {},
) {
  const byId = entryIndex(inventory);
  const context = { byId, projects, agents };
  const agentRows = agents.map((agent) =>
    agentAccessRow(entry, agent, context),
  );
  const projectRows = entry.owner_id
    ? []
    : projects
        .filter(
          (project) =>
            !entry.project_id || project.project_id === entry.project_id,
        )
        .map((project) => projectAccessRow(entry, project, context));
  return { agents: agentRows, projects: projectRows };
}

/** One quiet line saying who gets a Skill package, for the library list. */
export function skillAccessSummary(entry, agents = [], projects = []) {
  if (entry.disabled) return t('skills.summary.off');
  if (entry.status === 'invalid') return t('skills.summary.invalid');
  if (entry.owner_id) {
    const owner = agentDisplayName(entry.owner_id, agents);
    const shared = names(entry.shared_with).length;
    // Receivers whose own Skill selection lets them use the shared package.
    const count = agents.filter(
      (agent) =>
        agent.id !== entry.owner_id &&
        (agent.skills ?? []).some(
          (row) => row.package_id === entry.id && GRANTED.has(row.grant),
        ),
    ).length;
    // The owner has its own Skill unless it turned it off (an exclusion).
    const ownerRow = agents
      .find((agent) => agent.id === entry.owner_id)
      ?.skills?.find((row) => row.package_id === entry.id);
    const ownerOn = !ownerRow || GRANTED.has(ownerRow.grant);
    if (!shared)
      return ownerOn
        ? t('skills.summary.ownerOnly', { name: owner })
        : t('skills.summary.ownerOff', { name: owner });
    const blocked = shared - count;
    if (ownerOn)
      return blocked > 0
        ? t('skills.summary.ownerSharedBlocked', {
            name: owner,
            count,
            blocked,
          })
        : t('skills.summary.ownerShared', { name: owner, count });
    return blocked > 0
      ? t('skills.summary.ownerOffSharedBlocked', {
          name: owner,
          count,
          blocked,
        })
      : t('skills.summary.ownerOffShared', { name: owner, count });
  }
  if (entry.project_id) {
    const project = projects.find(
      (item) => item.project_id === entry.project_id,
    );
    const name = project?.name || entry.origin?.slice(8) || entry.project_id;
    const row = project?.skills?.find(
      (item) => item.name === entry.name && item.package_id === entry.id,
    );
    return row && !row.active
      ? t('skills.summary.projectOff', { name })
      : t('skills.summary.projectActive', { name });
  }
  if (!agents.length) return t('skills.summary.noAgents');
  const count = agents.filter((agent) =>
    (agent.skills ?? []).some(
      (row) => row.package_id === entry.id && GRANTED.has(row.grant),
    ),
  ).length;
  return agents.length === 1
    ? t('skills.summary.agentsOfOne', { count })
    : t('skills.summary.agents', { count, total: agents.length });
}

/**
 * Explains other packages with the same name and where they win over this
 * one. Empty when the name is unique.
 */
export function skillDuplicateNotes(
  entry,
  { inventory = [], agents = [], projects = [] } = {},
) {
  const usesPackage = (projection, id) =>
    (projection.skills ?? []).some(
      (row) => row.name === entry.name && row.package_id === id,
    );
  return inventory
    .filter((other) => other.name === entry.name && other.id !== entry.id)
    .map((other) => {
      const copy = skillCopyLabel(other, agents, projects);
      const inProjects = projects
        .filter((project) => usesPackage(project, other.id))
        .map((project) =>
          t('skills.detail.inProject', {
            name: project.name || project.project_id,
          }),
        )
        .join(', ');
      const forAgents = agents
        .filter((agent) => usesPackage(agent, other.id))
        .map((agent) => agent.name || agent.id)
        .join(', ');
      if (inProjects && forAgents)
        return t('skills.detail.duplicateWinsBoth', {
          copy,
          projects: inProjects,
          agents: forAgents,
        });
      if (inProjects)
        return t('skills.detail.duplicateWinsIn', {
          copy,
          projects: inProjects,
        });
      if (forAgents)
        return t('skills.detail.duplicateWinsFor', {
          copy,
          agents: forAgents,
        });
      return t('skills.detail.duplicate', { copy });
    });
}

/**
 * Details card of a Library row: the description leads (never shown inline),
 * then where the package lives, who gets it, and its requirement notes.
 */
export function skillRowDetails(entry, agents = [], projects = []) {
  return {
    title: entry.name,
    text: entry.description || '',
    rows: [
      {
        label: t('skills.details.source'),
        value: skillSourceDetail(entry, agents, projects),
      },
      {
        label: t('skills.details.access'),
        value: skillAccessSummary(entry, agents, projects),
      },
      {
        label: t('skills.details.notes'),
        value: skillDiagnosticLines(entry).join('\n'),
        tone: 'warning',
      },
    ],
    placement: 'right',
    alignTo: '.skills-row-name',
  };
}
