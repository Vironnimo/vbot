import { beforeEach, describe, expect, it } from 'vitest';
import { init, t } from '../../../lib/i18n.js';
import {
  accessPatch,
  agentSkillView,
  projectSkillPatch,
  setAutoAdd,
  skillAccessOf,
  skillAccessSummary,
  skillAccessView,
  skillDuplicateNotes,
  toggleSkill,
} from '../skillAccess.js';

const entry = (id, name, extra = {}) => ({
  id,
  name,
  description: `Purpose of ${name}`,
  origin: 'global',
  owner_id: null,
  project_id: null,
  shared: false,
  shared_with: [],
  disabled: false,
  status: 'available',
  missing: [],
  ...extra,
});
const row = (name, package_id, grant, available = true, own = false) => ({
  name,
  package_id,
  own,
  grant,
  available,
});
const ownRow = (name, package_id, grant = 'own') =>
  row(name, package_id, grant, true, true);

// A global `deploy` shadows the bundled one; Main owns `notes` and shares it
// with Reviewer, whose selection does not allow it.
const inventory = [
  entry('g-deploy', 'deploy'),
  entry('b-deploy', 'deploy', { origin: 'bundled' }),
  entry('b-teach', 'teach', { origin: 'bundled' }),
  entry('g-fetch', 'fetch', { status: 'unavailable', missing: ['env:TOKEN'] }),
  entry('p-lint', 'lint', { origin: 'project:Repo', project_id: 'repo' }),
  entry('own-notes', 'notes', {
    origin: 'agent',
    owner_id: 'main',
    shared: true,
    shared_with: ['reviewer'],
  }),
];
const main = {
  id: 'main',
  name: 'Main',
  root_project_id: 'repo',
  allowed_skills: ['*'],
  excluded_skills: ['teach'],
  skills: [
    ownRow('notes', 'own-notes'),
    row('lint', 'p-lint', 'project'),
    row('deploy', 'g-deploy', 'allowed'),
    row('teach', 'b-teach', 'excluded'),
    row('fetch', 'g-fetch', 'allowed', false),
  ],
};
const reviewer = {
  id: 'reviewer',
  name: 'Reviewer',
  root_project_id: null,
  allowed_skills: ['deploy', 'ghost'],
  excluded_skills: [],
  skills: [
    row('notes', 'own-notes', 'not_selected'),
    row('deploy', 'g-deploy', 'allowed'),
    row('teach', 'b-teach', 'not_selected'),
  ],
};
const repo = {
  project_id: 'repo',
  name: 'Repo',
  skills_project_disabled: ['old'],
  skills_global_enabled: ['deploy'],
  skills_bundled_enabled: [],
  skills: [
    { name: 'deploy', package_id: 'g-deploy', source: 'global', active: true },
    { name: 'lint', package_id: 'p-lint', source: 'project', active: true },
    { name: 'teach', package_id: 'b-teach', source: 'bundled', active: false },
  ],
};
const allowingReviewer = {
  ...reviewer,
  skills: [row('notes', 'own-notes', 'allowed')],
};
const mainOff = {
  ...main,
  excluded_skills: ['teach', 'notes'],
  skills: [ownRow('notes', 'own-notes', 'excluded')],
};
const agents = [main, reviewer];
const projects = [repo];
const context = { inventory, agents, projects };
const byId = (id) => inventory.find((item) => item.id === id);

beforeEach(() => init('en'));

describe('allowlist rules', () => {
  it.each([
    [
      'all: untick excludes',
      ['*'],
      ['a'],
      'b',
      false,
      false,
      ['*'],
      ['a', 'b'],
    ],
    ['all: tick releases', ['*'], ['a'], 'a', true, false, ['*'], []],
    [
      'selected: tick adds and releases',
      ['a'],
      ['b'],
      'b',
      true,
      false,
      ['a', 'b'],
      [],
    ],
    [
      'selected: untick keeps unknown',
      ['a', 'ghost'],
      [],
      'a',
      false,
      false,
      ['ghost'],
      [],
    ],
    [
      'own, all: untick excludes',
      ['*'],
      [],
      'own',
      false,
      true,
      ['*'],
      ['own'],
    ],
    [
      'own, selected: untick excludes',
      ['a'],
      [],
      'own',
      false,
      true,
      ['a'],
      ['own'],
    ],
    [
      'own, selected: tick only releases',
      ['a'],
      ['own'],
      'own',
      true,
      true,
      ['a'],
      [],
    ],
  ])(
    '%s',
    (_label, allowed, excluded, name, on, own, nextAllowed, nextExcluded) => {
      expect(toggleSkill({ allowed, excluded }, name, on, own)).toEqual({
        allowed: nextAllowed,
        excluded: nextExcluded,
      });
    },
  );

  it('switches auto-add without changing which listed Skills are granted', () => {
    const selected = { allowed: ['a', 'ghost'], excluded: ['old', 'mine'] };
    const all = setAutoAdd(selected, ['a', 'b'], true, ['mine']);
    expect(all).toEqual({ allowed: ['*'], excluded: ['b', 'old', 'mine'] });
    // Own Skills keep their exclusion in either mode.
    expect(setAutoAdd(all, ['a', 'b'], false, ['mine'])).toEqual({
      allowed: ['a'],
      excluded: ['mine'],
    });
    expect(setAutoAdd(all, ['a', 'b'], true, ['mine'])).toBe(all);
  });

  it('patches only the changed Agent fields', () => {
    const before = skillAccessOf(main);
    expect(accessPatch(before, before)).toEqual({});
    expect(accessPatch(before, toggleSkill(before, 'deploy', false))).toEqual({
      excluded_skills: ['teach', 'deploy'],
    });
  });

  it.each([
    ['project', 'lint', false, { skills_project_disabled: ['old', 'lint'] }],
    ['project', 'old', true, { skills_project_disabled: [] }],
    ['bundled', 'teach', true, { skills_bundled_enabled: ['teach'] }],
    ['global', 'deploy', false, { skills_global_enabled: [] }],
    ['agent', 'deploy', true, {}],
  ])('writes the %s pool list for %s active=%s', (source, name, on, patch) => {
    expect(projectSkillPatch(repo, source, [name], on)).toEqual(patch);
  });
});

describe('agentSkillView', () => {
  const summary = (view) =>
    view.groups.map((group) => [
      group.title,
      group.items.map((item) => [
        item.name,
        item.allowed,
        item.locked,
        item.state?.text ?? null,
      ]),
    ]);

  it('groups an Agent by why it gets each Skill and locks Project grants', () => {
    const view = agentSkillView(main, skillAccessOf(main), context);
    expect(summary(view)).toEqual([
      ['Own skills', [['notes', true, false, null]]],
      ['Via project Repo', [['lint', true, true, null]]],
      [
        'Global and extension skills',
        [
          ['deploy', true, false, null],
          ['fetch', true, false, 'env:TOKEN'],
        ],
      ],
      ['Bundled', [['teach', false, false, null]]],
    ]);
    expect(view).toMatchObject({
      governed: ['deploy', 'teach', 'fetch'],
      own: ['notes'],
      active: 4,
      total: 5,
      autoAdd: true,
    });
    expect(view.groups[1].items[0]).toMatchObject({
      lockedReason:
        'Granted by project Repo. Change it in that project’s skills.',
      lockedBy: 'Repo',
    });

    // Several missing requirements: the state names the first, the row's
    // tooltip lists them all.
    const fetchRow = agentSkillView(main, skillAccessOf(main), {
      ...context,
      inventory: inventory.map((item) =>
        item.id === 'g-fetch'
          ? { ...item, missing: ['env:TOKEN', 'bin:gh'] }
          : item,
      ),
    }).groups[2].items[1];
    expect(fetchRow.state.text).toBe(
      t('skills.access.missingMore', { first: 'env:TOKEN', count: 1 }),
    );
    expect(fetchRow.detailRows).toEqual([
      {
        label: t('skills.details.missing'),
        value: 'env:TOKEN\nbin:gh',
        mono: true,
      },
    ]);
  });

  it.each([
    [
      'an own Skill turned off stays in its group, unticked and uncounted',
      ownRow('notes', 'own-notes', 'excluded'),
      ['teach', 'notes'],
      ['notes', false, false, null],
      3,
    ],
    [
      'an own Skill the Project also grants is locked on',
      ownRow('lint', 'p-lint', 'project'),
      ['teach', 'lint'],
      ['lint', true, true, 'Via project Repo'],
      3,
    ],
  ])('%s', (_label, ownSkill, excluded, expected, active) => {
    const agent = {
      ...main,
      excluded_skills: excluded,
      skills: [
        ownSkill,
        ...main.skills.filter((r) => r.name !== ownSkill.name && !r.own),
      ],
    };
    const view = agentSkillView(agent, skillAccessOf(agent), context);
    expect(summary(view)[0]).toEqual(['Own skills', [expected]]);
    expect(view.active).toBe(active);
  });

  it('shows shared Skills by owner and keeps saved names it cannot see', () => {
    const view = agentSkillView(reviewer, skillAccessOf(reviewer), context);
    expect(summary(view)).toEqual([
      ['Shared with this Agent', [['notes', false, false, 'From Main']]],
      ['Global and extension skills', [['deploy', true, false, null]]],
      ['Bundled', [['teach', false, false, null]]],
      ['Saved but not found', [['ghost', true, false, null]]],
    ]);
    // Saved names grant nothing, so they do not count as active.
    expect(view).toMatchObject({ active: 1, total: 3, autoAdd: false });
  });

  it('lists the global pool over bundled copies for an Agent not created yet', () => {
    const view = agentSkillView(
      null,
      { allowed: ['*'], excluded: [] },
      { inventory },
    );
    expect(summary(view)).toEqual([
      [
        'Global and extension skills',
        [
          ['deploy', true, false, null],
          ['fetch', true, false, 'env:TOKEN'],
        ],
      ],
      ['Bundled', [['teach', true, false, null]]],
    ]);
    expect(view.groups[0].items[0].packageId).toBe('g-deploy');
  });
});

describe('skillAccessView', () => {
  const rows = (view) =>
    [...view.agents, ...view.projects].map((item) => [
      item.name,
      item.kind,
      item.allowed,
      item.state?.text ?? null,
    ]);

  it.each([
    [
      'a global Skill: Agent selections and Project activation',
      'g-deploy',
      [
        ['Main', 'grant', true, null],
        ['Reviewer', 'grant', true, null],
        ['Repo', 'project', true, null],
      ],
    ],
    [
      'a shadowed copy: locked on the copy that wins',
      'b-deploy',
      [
        ['Main', 'locked', false, 'Uses the global copy'],
        ['Reviewer', 'locked', false, 'Uses the global copy'],
        ['Repo', 'locked', false, 'Uses the global copy'],
      ],
    ],
    [
      'an excluded bundled Skill',
      'b-teach',
      [
        ['Main', 'grant', false, 'Excluded'],
        ['Reviewer', 'grant', false, null],
        ['Repo', 'project', false, null],
      ],
    ],
    [
      'a Project Skill: fixed for the root Agent, only in its Project',
      'p-lint',
      [
        ['Main', 'locked', true, 'Via project Repo'],
        ['Reviewer', 'locked', false, 'Only in project Repo'],
        ['Repo', 'project', true, null],
      ],
    ],
    [
      'a granted Skill whose requirements are missing',
      'g-fetch',
      [
        ['Main', 'grant', true, 'env:TOKEN'],
        ['Reviewer', 'locked', false, 'Not visible to this Agent'],
        ['Repo', 'locked', false, 'Not in this project'],
      ],
    ],
  ])('presents %s', (_label, id, expected) => {
    expect(rows(skillAccessView(byId(id), context))).toEqual(expected);
  });

  it('turns other Agents into sharing rows and offers Allow when blocked', () => {
    const view = skillAccessView(byId('own-notes'), context);
    expect(rows(view)).toEqual([
      ['Main', 'own', true, 'Owner'],
      ['Reviewer', 'share', true, 'Blocked by this Agent’s skill selection'],
    ]);
    const ownerOff = {
      ...main,
      skills: [ownRow('notes', 'own-notes', 'excluded')],
    };
    expect(
      skillAccessView(byId('own-notes'), { ...context, agents: [ownerOff] })
        .agents[0],
    ).toMatchObject({ kind: 'own', allowed: false, locked: false });
    expect(view.agents[1].action.ariaLabel).toBe('Allow notes for Reviewer');
    const unshared = { ...byId('own-notes'), shared_with: [] };
    expect(skillAccessView(unshared, context).agents[1]).toMatchObject({
      kind: 'share',
      allowed: false,
      state: null,
      action: null,
    });
  });

  it('locks every row of a Skill package turned off', () => {
    const off = { ...byId('g-deploy'), disabled: true, status: 'disabled' };
    const view = skillAccessView(off, context);
    expect(
      [...view.agents, ...view.projects].every((item) => item.locked),
    ).toBe(true);
  });
});

describe('library summaries', () => {
  it.each([
    ['g-deploy', {}, agents, '2 of 2 Agents'],
    ['b-deploy', {}, agents, '0 of 2 Agents'],
    ['g-deploy', {}, [main], '1 of 1 Agent'],
    ['g-deploy', {}, [], 'No Agents'],
    ['own-notes', {}, agents, 'Main + 0 shared (1 blocked)'],
    ['own-notes', {}, [main, allowingReviewer], 'Main + 1 shared'],
    ['own-notes', { shared_with: [] }, agents, 'Main only'],
    ['own-notes', { shared_with: [] }, [mainOff], 'Off for Main'],
    ['own-notes', {}, [mainOff, allowingReviewer], 'Off for Main, 1 shared'],
    ['p-lint', {}, agents, 'Active in Repo'],
    ['g-deploy', { disabled: true }, agents, 'Turned off'],
    ['g-deploy', { status: 'invalid' }, agents, 'Not loadable'],
  ])('summarizes %s %j', (id, extra, agentList, text) => {
    expect(
      skillAccessSummary({ ...byId(id), ...extra }, agentList, projects),
    ).toBe(text);
  });

  it('explains where another copy with the same name wins', () => {
    expect(skillDuplicateNotes(byId('b-deploy'), context)).toEqual([
      'Also exists as the global copy; that copy wins in project Repo and for Main, Reviewer.',
    ]);
    expect(skillDuplicateNotes(byId('g-deploy'), context)).toEqual([
      'Also exists as the bundled copy.',
    ]);
    expect(skillDuplicateNotes(byId('b-teach'), context)).toEqual([]);
  });
});
