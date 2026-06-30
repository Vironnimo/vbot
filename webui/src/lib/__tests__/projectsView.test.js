import { describe, expect, it } from 'vitest';

import {
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  PROJECT_THINKING_EFFORT_NO_DEFAULT,
  buildAddProjectPayload,
  buildDefaultAgentOptions,
  buildManageProjectPayload,
  buildRePointPayload,
  buildSkillToggleSections,
  buildToolToggleList,
  hasManageChanges,
  needsRePoint,
  normalizeProject,
  normalizeProjects,
  normalizeScanReport,
  normalizeScanSkills,
  projectTeam,
  setListMembership,
} from '../projectsView.js';

describe('buildAddProjectPayload', () => {
  it('builds a payload with only cwd when optionals are blank', () => {
    expect(
      buildAddProjectPayload({
        cwd: '  C:/repos/demo  ',
        display_name: '',
        default_agent: '   ',
        default_model: '',
        auto_load: [],
      }),
    ).toEqual({ cwd: 'C:/repos/demo' });
  });

  it('includes optional pointers and auto-load when provided', () => {
    expect(
      buildAddProjectPayload({
        cwd: 'C:/repos/demo',
        display_name: 'Demo',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md', '  README.md  ', ''],
      }),
    ).toEqual({
      cwd: 'C:/repos/demo',
      display_name: 'Demo',
      default_agent: 'builder',
      default_model: 'openai/gpt-5.2',
      auto_load: ['AGENTS.md', 'README.md'],
    });
  });

  it('includes the default knobs when set, and 0 / "" count as real values', () => {
    expect(
      buildAddProjectPayload({
        cwd: 'C:/repos/demo',
        default_temperature: '0',
        default_thinking_effort: '',
      }),
    ).toEqual({
      cwd: 'C:/repos/demo',
      default_temperature: 0,
      default_thinking_effort: '',
    });
  });

  it('omits the default knobs when blank / the no-default sentinel', () => {
    expect(
      buildAddProjectPayload({
        cwd: 'C:/repos/demo',
        default_temperature: '',
        default_thinking_effort: PROJECT_THINKING_EFFORT_NO_DEFAULT,
      }),
    ).toEqual({ cwd: 'C:/repos/demo' });
  });
});

describe('buildManageProjectPayload', () => {
  const project = {
    display_name: 'Demo',
    default_agent: 'builder',
    default_model: 'openai/gpt-5.2',
    auto_load: ['AGENTS.md'],
  };

  it('returns an empty change set when nothing changed', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: 'Demo',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
      project,
    );
    expect(changes).toEqual({});
    expect(hasManageChanges(changes)).toBe(false);
  });

  it('emits only the fields that actually changed (sparse)', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: 'Renamed',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md', 'README.md'],
      },
      project,
    );
    expect(changes).toEqual({
      display_name: 'Renamed',
      auto_load: ['AGENTS.md', 'README.md'],
    });
    expect(hasManageChanges(changes)).toBe(true);
  });

  it('clears a default pointer to null when emptied', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: 'Demo',
        default_agent: '',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
      project,
    );
    // null clears the pointer (backend maps None → ""); a sent "" would be
    // rejected as invalid_request.
    expect(changes).toEqual({ default_agent: null });
    expect(hasManageChanges(changes)).toBe(true);
  });

  it('sends a changed pointer as a trimmed string', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: 'Demo',
        default_agent: '  planner  ',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
      project,
    );
    expect(changes).toEqual({ default_agent: 'planner' });
  });

  it('treats an emptied display_name as no change (it is required)', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: '',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
      project,
    );
    expect(changes).toEqual({});
  });
});

describe('buildManageProjectPayload default knobs', () => {
  const baseProject = {
    display_name: 'Demo',
    default_agent: 'builder',
    default_model: 'openai/gpt-5.2',
    default_temperature: 0.5,
    default_thinking_effort: 'high',
    auto_load: ['AGENTS.md'],
  };

  function form(overrides) {
    return {
      display_name: 'Demo',
      default_agent: 'builder',
      default_model: 'openai/gpt-5.2',
      default_temperature: '0.5',
      default_thinking_effort: 'high',
      auto_load: ['AGENTS.md'],
      ...overrides,
    };
  }

  it('emits no knob changes when they match the stored values', () => {
    expect(buildManageProjectPayload(form(), baseProject)).toEqual({});
  });

  it('emits a changed temperature as a number', () => {
    expect(
      buildManageProjectPayload(
        form({ default_temperature: '0.2' }),
        baseProject,
      ),
    ).toEqual({ default_temperature: 0.2 });
  });

  it('clears temperature to null when the box is emptied', () => {
    expect(
      buildManageProjectPayload(form({ default_temperature: '' }), baseProject),
    ).toEqual({ default_temperature: null });
  });

  it('treats 0 as a real temperature change versus a stored null', () => {
    const project = { ...baseProject, default_temperature: null };
    expect(
      buildManageProjectPayload(form({ default_temperature: '0' }), project),
    ).toEqual({ default_temperature: 0 });
  });

  it('clears thinking effort to null via the no-default sentinel', () => {
    expect(
      buildManageProjectPayload(
        form({ default_thinking_effort: PROJECT_THINKING_EFFORT_NO_DEFAULT }),
        baseProject,
      ),
    ).toEqual({ default_thinking_effort: null });
  });

  it('sends "" to force the provider default', () => {
    expect(
      buildManageProjectPayload(
        form({ default_thinking_effort: '' }),
        baseProject,
      ),
    ).toEqual({ default_thinking_effort: '' });
  });

  it('sends a changed effort level', () => {
    expect(
      buildManageProjectPayload(
        form({ default_thinking_effort: 'low' }),
        baseProject,
      ),
    ).toEqual({ default_thinking_effort: 'low' });
  });
});

describe('buildDefaultAgentOptions', () => {
  it('leads with the empty option and lists the scanned team', () => {
    const options = buildDefaultAgentOptions({
      team: [
        { agent_id: 'builder', display_name: 'Builder' },
        { agent_id: 'planner', display_name: 'planner' },
      ],
      currentValue: 'builder',
      emptyLabel: 'No project default',
    });

    expect(options).toEqual([
      { value: '', label: 'No project default' },
      { value: 'builder', label: 'Builder', secondaryLabel: 'builder' },
      { value: 'planner', label: 'planner', secondaryLabel: '' },
    ]);
  });

  it('keeps a stored agent that is no longer in the team as a trailing option', () => {
    const options = buildDefaultAgentOptions({
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
      currentValue: 'ghost',
      emptyLabel: '—',
      unavailableLabel: (agentId) => `${agentId} (gone)`,
    });

    expect(options).toEqual([
      { value: '', label: '—' },
      { value: 'builder', label: 'Builder', secondaryLabel: 'builder' },
      { value: 'ghost', label: 'ghost (gone)' },
    ]);
  });

  it('does not duplicate a stored agent that is already in the team', () => {
    const options = buildDefaultAgentOptions({
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
      currentValue: 'builder',
      emptyLabel: '—',
    });

    expect(options.filter((option) => option.value === 'builder')).toHaveLength(
      1,
    );
  });
});

describe('needsRePoint / buildRePointPayload', () => {
  it('only treats an explicit cwd_exists false as needing re-point', () => {
    expect(needsRePoint({ cwd_exists: false })).toBe(true);
    expect(needsRePoint({ cwd_exists: true })).toBe(false);
    expect(needsRePoint({})).toBe(false);
    expect(needsRePoint(null)).toBe(false);
  });

  it('builds a trimmed cwd-only re-point payload', () => {
    expect(buildRePointPayload('  C:/repos/moved  ')).toEqual({
      cwd: 'C:/repos/moved',
    });
  });
});

describe('normalizeProject / normalizeProjects', () => {
  it('normalizes a project into a stable display shape', () => {
    expect(
      normalizeProject({
        project_id: 'demo',
        display_name: 'Demo',
        cwd: 'C:/repos/demo',
        cwd_exists: true,
        default_agent: 'builder',
        default_model: '',
        default_temperature: 0.4,
        default_thinking_effort: 'high',
        auto_load: ['AGENTS.md', '  '],
        created_at: '2026-06-18T00:00:00Z',
        updated_at: '2026-06-18T01:00:00Z',
      }),
    ).toEqual({
      project_id: 'demo',
      display_name: 'Demo',
      cwd: 'C:/repos/demo',
      cwd_exists: true,
      default_agent: 'builder',
      default_model: '',
      default_temperature: 0.4,
      default_thinking_effort: 'high',
      auto_load: ['AGENTS.md'],
      allowed_tools: [],
      skills_bundled_enabled: [],
      skills_global_enabled: [],
      skills_project_disabled: [],
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T01:00:00Z',
    });
  });

  it('defaults the knobs to null and preserves a "" provider-default effort', () => {
    const noDefaults = normalizeProject({ project_id: 'demo' });
    expect(noDefaults.default_temperature).toBeNull();
    expect(noDefaults.default_thinking_effort).toBeNull();

    // 0 is a real temperature, "" is the explicit provider-default effort — both
    // are preserved (not coerced to null).
    const explicit = normalizeProject({
      project_id: 'demo',
      default_temperature: 0,
      default_thinking_effort: '',
    });
    expect(explicit.default_temperature).toBe(0);
    expect(explicit.default_thinking_effort).toBe('');
  });

  it('coerces a missing cwd_exists to false and tolerates a non-list', () => {
    const project = normalizeProject({ project_id: 'demo' });
    expect(project.cwd_exists).toBe(false);
    expect(project.auto_load).toEqual([]);
    expect(normalizeProjects(undefined)).toEqual([]);
    expect(normalizeProjects([{ project_id: 'a' }]).length).toBe(1);
  });
});

describe('projectTeam', () => {
  it('projects the scan team into a display-ready list', () => {
    expect(
      projectTeam({
        team: [
          {
            agent_id: 'builder',
            display_name: 'Builder',
            description: 'Builds things',
            model: 'openai/gpt-5.2',
            temperature: 0.2,
            thinking_effort: 'high',
            source_format: 'opencode',
            source_path: '.opencode/agents/builder.md',
            model_override: 'openai/gpt-mini',
          },
          { agent_id: 'planner' },
        ],
      }),
    ).toEqual([
      {
        agent_id: 'builder',
        display_name: 'Builder',
        description: 'Builds things',
        model: 'openai/gpt-5.2',
        temperature: 0.2,
        thinking_effort: 'high',
        source_format: 'opencode',
        source_path: '.opencode/agents/builder.md',
        denied_tools: [],
        // The per-agent override is carried through for the team-row badge.
        model_override: 'openai/gpt-mini',
      },
      {
        agent_id: 'planner',
        display_name: 'planner',
        description: '',
        model: '',
        temperature: null,
        thinking_effort: null,
        source_format: '',
        source_path: '',
        denied_tools: [],
        // No override → null (an agent without a pinned model shows no badge).
        model_override: null,
      },
    ]);
  });

  it('returns an empty list for a missing team', () => {
    expect(projectTeam({})).toEqual([]);
    expect(projectTeam(undefined)).toEqual([]);
  });
});

describe('normalizeScanReport', () => {
  it('treats an empty/clean report as the normal, healthy case', () => {
    const clean = normalizeScanReport({ clean: true, findings: [] });
    expect(clean.clean).toBe(true);
    expect(clean.findingCount).toBe(0);
    expect(clean.groups).toEqual([]);

    const missing = normalizeScanReport(undefined);
    expect(missing.clean).toBe(true);
    expect(missing.groups).toEqual([]);
  });

  it('groups findings by type in the stable display order', () => {
    const report = normalizeScanReport({
      clean: false,
      findings: [
        {
          type: FINDING_TYPE_ORPHAN,
          detail: 'orphan pointer',
          agent_id: 'ghost',
        },
        {
          type: FINDING_TYPE_SLUG_COLLISION,
          detail: 'two on one id',
          agent_id: 'dup',
          source_path: 'a.md',
        },
        { type: FINDING_TYPE_BAD_MODEL, detail: 'bad model', agent_id: 'b' },
        {
          type: FINDING_TYPE_UNSLUGIFIABLE_NAME,
          detail: 'no slug',
          agent_id: '',
        },
        {
          type: FINDING_TYPE_SLUG_COLLISION,
          detail: 'another collision',
          agent_id: 'dup2',
        },
      ],
    });

    expect(report.clean).toBe(false);
    expect(report.findingCount).toBe(5);
    expect(report.groups.map((group) => group.type)).toEqual([
      FINDING_TYPE_SLUG_COLLISION,
      FINDING_TYPE_UNSLUGIFIABLE_NAME,
      FINDING_TYPE_BAD_MODEL,
      FINDING_TYPE_ORPHAN,
    ]);
    expect(report.groups[0].findings).toHaveLength(2);
  });

  it('falls back to the finding count when the clean flag is absent', () => {
    const report = normalizeScanReport({
      findings: [{ type: FINDING_TYPE_BAD_MODEL, detail: 'x' }],
    });
    expect(report.clean).toBe(false);
  });
});

describe('buildToolToggleList', () => {
  it('marks catalog tools enabled when in the whitelist and drops memory', () => {
    const rows = buildToolToggleList({
      catalog: [{ name: 'read' }, { name: 'edit' }, { name: 'memory' }],
      allowedTools: ['read'],
    });

    expect(rows).toEqual([
      { name: 'edit', enabled: false },
      { name: 'read', enabled: true },
    ]);
  });

  it('accepts a catalog of bare names and sorts the rows', () => {
    const rows = buildToolToggleList({
      catalog: ['grep', 'bash'],
      allowedTools: ['bash'],
    });

    expect(rows.map((row) => row.name)).toEqual(['bash', 'grep']);
  });
});

describe('buildSkillToggleSections', () => {
  it('defaults project skills on (off when disabled) and bundled/global off (on when enabled)', () => {
    const sections = buildSkillToggleSections({
      projectSkills: ['refactoring', 'debugging'],
      bundledSkills: ['pdf', 'xlsx'],
      globalSkills: ['deploy', 'audit'],
      skillsBundledEnabled: ['pdf'],
      skillsGlobalEnabled: ['deploy'],
      skillsProjectDisabled: ['debugging'],
    });

    expect(sections.project).toEqual([
      { name: 'refactoring', enabled: true },
      { name: 'debugging', enabled: false },
    ]);
    expect(sections.bundled).toEqual([
      { name: 'pdf', enabled: true },
      { name: 'xlsx', enabled: false },
    ]);
    expect(sections.global).toEqual([
      { name: 'deploy', enabled: true },
      { name: 'audit', enabled: false },
    ]);
  });

  it('drops a bundled or global skill shadowed by a project skill of the same name', () => {
    const sections = buildSkillToggleSections({
      projectSkills: ['glossary'],
      bundledSkills: ['glossary', 'pdf'],
      globalSkills: ['glossary', 'deploy'],
    });

    expect(sections.bundled.map((row) => row.name)).toEqual(['pdf']);
    expect(sections.global.map((row) => row.name)).toEqual(['deploy']);
  });
});

describe('setListMembership', () => {
  it('adds, removes, and is a no-op when already in the desired state', () => {
    expect(setListMembership(['read'], 'edit', true)).toEqual(['read', 'edit']);
    expect(setListMembership(['read', 'edit'], 'edit', false)).toEqual([
      'read',
    ]);
    expect(setListMembership(['read'], 'read', true)).toEqual(['read']);
    expect(setListMembership(['read'], 'edit', false)).toEqual(['read']);
  });
});

describe('normalizeScanSkills', () => {
  it('extracts the project, bundled, and global skill pools', () => {
    expect(
      normalizeScanSkills({
        skills: { project: ['a', ' '], bundled: ['b'], global: ['c'] },
      }),
    ).toEqual({ project: ['a'], bundled: ['b'], global: ['c'] });
    expect(normalizeScanSkills(undefined)).toEqual({
      project: [],
      bundled: [],
      global: [],
    });
  });
});

describe('buildManageProjectPayload whitelist fields', () => {
  const project = {
    display_name: 'Demo',
    allowed_tools: ['read', 'edit'],
    skills_bundled_enabled: [],
    skills_project_disabled: [],
  };

  it('sends a whitelist field only when its set changed (order-insensitive)', () => {
    // Same set, different order → no change.
    const unchanged = buildManageProjectPayload(
      { display_name: 'Demo', allowed_tools: ['edit', 'read'] },
      project,
    );
    expect(unchanged.allowed_tools).toBeUndefined();

    // A real membership change is sent.
    const changed = buildManageProjectPayload(
      { display_name: 'Demo', allowed_tools: ['read'] },
      project,
    );
    expect(changed).toEqual({ allowed_tools: ['read'] });
  });

  it('sends an empty allowed_tools as a real "every tool off" change', () => {
    const changes = buildManageProjectPayload(
      { display_name: 'Demo', allowed_tools: [] },
      project,
    );
    expect(changes).toEqual({ allowed_tools: [] });
  });

  it('diffs the skill rule fields', () => {
    // The form always carries every whitelist field (seeded from the project), so
    // allowed_tools matches and only the skill fields differ here.
    const changes = buildManageProjectPayload(
      {
        display_name: 'Demo',
        allowed_tools: ['read', 'edit'],
        skills_bundled_enabled: ['pdf'],
        skills_project_disabled: ['debugging'],
      },
      project,
    );
    expect(changes).toEqual({
      skills_bundled_enabled: ['pdf'],
      skills_project_disabled: ['debugging'],
    });
  });
});
