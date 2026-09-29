import { beforeEach, describe, expect, it } from 'vitest';
import { init } from '../../../lib/i18n.js';
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
const row = (name, package_id, grant, available = true) => ({
  name,
  package_id,
  grant,
  available,
});

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
    row('notes', 'own-notes', 'own'),
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
const agents = [main, reviewer];
const projects = [repo];
const context = { inventory, agents, projects };
const byId = (id) => inventory.find((item) => item.id === id);

beforeEach(() => init('en'));

describe('allowlist rules', () => {
  it.each([
    ['all: untick excludes', ['*'], ['a'], 'b', false, ['*'], ['a', 'b']],
    ['all: tick releases', ['*'], ['a'], 'a', true, ['*'], []],
    [
      'selected: tick adds and releases',
      ['a'],
      ['b'],
      'b',
      true,
      ['a', 'b'],
      [],
    ],
    [
      'selected: untick keeps unknown',
      ['a', 'ghost'],
      [],
      'a',
      false,
      ['ghost'],
      [],
    ],
  ])('%s', (_label, allowed, excluded, name, on, nextAllowed, nextExcluded) => {
    expect(toggleSkill({ allowed, excluded }, name, on)).toEqual({
      allowed: nextAllowed,
      excluded: nextExcluded,
    });
  });

  it('switches auto-add without changing which listed Skills are granted', () => {
    const selected = { allowed: ['a', 'ghost'], excluded: ['old'] };
    const all = setAutoAdd(selected, ['a', 'b'], true);
    expect(all).toEqual({ allowed: ['*'], excluded: ['b', 'old'] });
    expect(setAutoAdd(all, ['a', 'b'], false)).toEqual({
      allowed: ['a'],
      excluded: [],
    });
    expect(setAutoAdd(all, ['a', 'b'], true)).toBe(all);
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

  it('groups an Agent by why it gets each Skill and locks fixed grants', () => {
    const view = agentSkillView(main, skillAccessOf(main), context);
    expect(summary(view)).toEqual([
      ['Own skills', [['notes', true, true, null]]],
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
      active: 4,
      total: 5,
      autoAdd: true,
    });
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
      ['Main', 'locked', true, 'Owner'],
      ['Reviewer', 'share', true, 'Blocked by this Agent’s skill selection'],
    ]);
    expect(view.agents[1].action.ariaLabel).toBe('Allow notes for Reviewer');
    const unshared = { ...byId('own-notes'), shared_with: [] };
    expect(skillAccessView(unshared, context).agents[1]).toMatchObject({
      kind: 'share',
      allowed: false,
      state: null,
      action: null,
    });
  });

  it('locks every row of a Skill turned off everywhere', () => {
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
    ['p-lint', {}, agents, 'Active in Repo'],
    ['g-deploy', { disabled: true }, agents, 'Off everywhere'],
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
