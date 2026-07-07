import { describe, expect, it } from 'vitest';

import {
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  PROJECT_SOURCE_FORMATS,
  PROJECT_THINKING_EFFORT_NO_DEFAULT,
  buildAddProjectPayload,
  buildDefaultAgentOptions,
  buildManageProjectPayload,
  buildRePointPayload,
  buildSkillToggleSections,
  buildToolToggleList,
  hasManageChanges,
  memberFieldIsOverridden,
  needsRePoint,
  normalizeDetectResult,
  normalizeOverrideTemperature,
  normalizeProject,
  normalizeProjects,
  normalizeScanReport,
  normalizeScanSkills,
  presentFormats,
  projectTeam,
  seedTeamOverrideDraft,
  setListMembership,
  shouldSuggestClaudeMd,
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

  it('includes a known source format and omits blank/unknown ones', () => {
    expect(
      buildAddProjectPayload({ cwd: 'C:/repos/demo', source_format: 'claude' }),
    ).toEqual({ cwd: 'C:/repos/demo', source_format: 'claude' });
    // Absent/blank → the server auto-detects; unknown values are never sent.
    expect(
      buildAddProjectPayload({ cwd: 'C:/repos/demo', source_format: '' }),
    ).toEqual({ cwd: 'C:/repos/demo' });
    expect(
      buildAddProjectPayload({ cwd: 'C:/repos/demo', source_format: 'cursor' }),
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

describe('buildManageProjectPayload source format', () => {
  const project = { display_name: 'Demo', source_format: 'opencode' };

  it('emits a changed source format', () => {
    expect(
      buildManageProjectPayload(
        { display_name: 'Demo', source_format: 'claude' },
        project,
      ),
    ).toEqual({ source_format: 'claude' });
  });

  it('treats an unchanged or empty source format as no change', () => {
    expect(
      buildManageProjectPayload(
        { display_name: 'Demo', source_format: 'opencode' },
        project,
      ),
    ).toEqual({});
    // source_format is required non-empty on the backend — never a clear.
    expect(
      buildManageProjectPayload(
        { display_name: 'Demo', source_format: '' },
        project,
      ),
    ).toEqual({});
  });
});

describe('normalizeDetectResult / presentFormats / shouldSuggestClaudeMd', () => {
  it('normalizes counts, presence, and context files', () => {
    const detect = normalizeDetectResult({
      cwd_exists: true,
      formats: {
        opencode: { agents: 2, skills: 0 },
        claude: { agents: 0, skills: 3 },
      },
      context_files: { agents_md: true, claude_md: 'CLAUDE.md' },
    });

    expect(detect.cwd_exists).toBe(true);
    expect(detect.formats.opencode).toEqual({
      agents: 2,
      skills: 0,
      present: true,
    });
    // Skills alone make a format present (≥1 agent OR ≥1 skill).
    expect(detect.formats.claude.present).toBe(true);
    expect(detect.agents_md).toBe(true);
    expect(detect.claude_md).toBe('CLAUDE.md');
    expect(presentFormats(detect)).toEqual(['opencode', 'claude']);
  });

  it('degrades a missing/foreign response to nothing found', () => {
    const detect = normalizeDetectResult(null);

    expect(detect.cwd_exists).toBe(false);
    for (const key of PROJECT_SOURCE_FORMATS) {
      expect(detect.formats[key]).toEqual({
        agents: 0,
        skills: 0,
        present: false,
      });
    }
    expect(detect.claude_md).toBe(null);
    expect(presentFormats(detect)).toEqual([]);
  });

  it('suggests CLAUDE.md only when found and no AGENTS.md exists', () => {
    expect(
      shouldSuggestClaudeMd({ agents_md: false, claude_md: 'CLAUDE.md' }),
    ).toBe(true);
    expect(
      shouldSuggestClaudeMd({ agents_md: true, claude_md: 'CLAUDE.md' }),
    ).toBe(false);
    expect(shouldSuggestClaudeMd({ agents_md: false, claude_md: null })).toBe(
      false,
    );
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
      source_format: 'opencode',
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

  it('keeps a known source format and defaults an absent/unknown one', () => {
    expect(
      normalizeProject({ project_id: 'demo', source_format: 'claude' })
        .source_format,
    ).toBe('claude');
    expect(normalizeProject({ project_id: 'demo' }).source_format).toBe(
      'opencode',
    );
    expect(
      normalizeProject({ project_id: 'demo', source_format: 'cursor' })
        .source_format,
    ).toBe('opencode');
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
  it('projects the scan team into a display-ready list with overrides + effective', () => {
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
            denied_tools: ['bash'],
            overrides: { model: 'openai/gpt-mini' },
            effective: {
              model: { value: 'openai/gpt-mini', source: 'override' },
              temperature: { value: 0.2, source: 'agent' },
              thinking_effort: { value: 'high', source: 'agent' },
            },
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
        denied_tools: ['bash'],
        // The per-agent override object (subset of the three fields), or null.
        overrides: { model: 'openai/gpt-mini' },
        // Provenance-aware resolved values per run field.
        effective: {
          model: { value: 'openai/gpt-mini', source: 'override' },
          temperature: { value: 0.2, source: 'agent' },
          thinking_effort: { value: 'high', source: 'agent' },
        },
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
        // No override → null; effective defaults to a stable null-per-field map.
        overrides: null,
        effective: {
          model: { value: null, source: null },
          temperature: { value: null, source: null },
          thinking_effort: { value: null, source: null },
        },
      },
    ]);
  });

  it('drops unknown override fields and treats an empty override object as null', () => {
    const [member] = projectTeam({
      team: [{ agent_id: 'a', overrides: { unknown: 'x' } }],
    });
    expect(member.overrides).toBeNull();

    const [withTemp] = projectTeam({
      team: [{ agent_id: 'a', overrides: { temperature: 0.5, unknown: 'x' } }],
    });
    expect(withTemp.overrides).toEqual({ temperature: 0.5 });
  });

  it('returns an empty list for a missing team', () => {
    expect(projectTeam({})).toEqual([]);
    expect(projectTeam(undefined)).toEqual([]);
  });
});

describe('memberFieldIsOverridden', () => {
  it('is true only when the effective source for the field is "override"', () => {
    const member = {
      effective: {
        model: { value: 'x', source: 'override' },
        temperature: { value: 0.2, source: 'agent' },
        thinking_effort: { value: null, source: null },
      },
    };
    expect(memberFieldIsOverridden(member, 'model')).toBe(true);
    expect(memberFieldIsOverridden(member, 'temperature')).toBe(false);
    expect(memberFieldIsOverridden(member, 'thinking_effort')).toBe(false);
    expect(memberFieldIsOverridden(undefined, 'model')).toBe(false);
  });
});

describe('seedTeamOverrideDraft', () => {
  it('seeds from the overridden values when present', () => {
    const draft = seedTeamOverrideDraft({
      overrides: {
        model: 'openai/gpt-mini',
        temperature: 0.3,
        thinking_effort: 'low',
      },
      effective: {
        model: { value: 'openai/gpt-mini', source: 'override' },
        temperature: { value: 0.3, source: 'override' },
        thinking_effort: { value: 'low', source: 'override' },
      },
    });
    expect(draft).toEqual({
      model: 'openai/gpt-mini',
      temperature: '0.3',
      thinking_effort: 'low',
    });
  });

  it('falls back to the effective values as a starting suggestion', () => {
    const draft = seedTeamOverrideDraft({
      overrides: null,
      effective: {
        model: { value: 'openai/gpt-5.2', source: 'agent' },
        temperature: { value: null, source: null },
        thinking_effort: { value: 'high', source: 'project_default' },
      },
    });
    expect(draft).toEqual({
      model: 'openai/gpt-5.2',
      temperature: '',
      thinking_effort: 'high',
    });
  });
});

describe('normalizeOverrideTemperature', () => {
  it('parses comma-decimals and returns null for an empty/invalid box', () => {
    expect(normalizeOverrideTemperature('0,7')).toBe(0.7);
    expect(normalizeOverrideTemperature('0')).toBe(0);
    expect(normalizeOverrideTemperature('')).toBeNull();
    expect(normalizeOverrideTemperature('abc')).toBeNull();
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
  it('marks catalog tools enabled when in the whitelist and drops memory and skill_manage', () => {
    const rows = buildToolToggleList({
      catalog: [
        { name: 'read' },
        { name: 'edit' },
        { name: 'memory' },
        { name: 'skill' },
        { name: 'skill_manage' },
      ],
      allowedTools: ['read', 'skill'],
    });

    // memory (runtime-derived) and skill_manage (identity-only) are excluded; skill
    // stays an ordinary, toggleable project tool. Each row carries the readiness
    // fields (defaulting to ready) so a not-ready tool renders the shared notice.
    expect(rows).toEqual([
      {
        name: 'edit',
        description: '',
        enabled: false,
        ready: true,
        readiness_hint: null,
        extension: null,
      },
      {
        name: 'read',
        description: '',
        enabled: true,
        ready: true,
        readiness_hint: null,
        extension: null,
      },
      {
        name: 'skill',
        description: '',
        enabled: true,
        ready: true,
        readiness_hint: null,
        extension: null,
      },
    ]);
  });

  it('carries a not-ready tool s readiness fields through to its row', () => {
    const rows = buildToolToggleList({
      catalog: [
        {
          name: 'home_assistant',
          description: 'Talk to Home Assistant.',
          ready: false,
          readiness_hint: 'Set the Home Assistant token first.',
          extension: 'homeassistant',
        },
      ],
      allowedTools: [],
    });

    expect(rows).toEqual([
      {
        name: 'home_assistant',
        description: 'Talk to Home Assistant.',
        enabled: false,
        ready: false,
        readiness_hint: 'Set the Home Assistant token first.',
        extension: 'homeassistant',
      },
    ]);
  });

  it('accepts a catalog of bare names and sorts the rows', () => {
    const rows = buildToolToggleList({
      catalog: ['grep', 'bash'],
      allowedTools: ['bash'],
    });

    expect(rows.map((row) => row.name)).toEqual(['bash', 'grep']);
    // A bare-name entry has no readiness metadata, so it defaults to ready.
    expect(rows[0]).toMatchObject({ ready: true, readiness_hint: null });
  });
});

describe('buildSkillToggleSections', () => {
  it('defaults project skills on (off when disabled) and bundled/global off (on when enabled)', () => {
    const sections = buildSkillToggleSections({
      projectSkills: [
        { name: 'refactoring', description: 'Refactor.' },
        { name: 'debugging', description: 'Debug.' },
      ],
      bundledSkills: [
        { name: 'pdf', description: 'PDFs.' },
        { name: 'xlsx', description: 'Sheets.' },
      ],
      globalSkills: [
        { name: 'deploy', description: 'Deploy.' },
        { name: 'audit', description: 'Audit.' },
      ],
      skillsBundledEnabled: ['pdf'],
      skillsGlobalEnabled: ['deploy'],
      skillsProjectDisabled: ['debugging'],
    });

    // Each entry carries the skill's description through for the chip hover card.
    expect(sections.project).toEqual([
      { name: 'refactoring', description: 'Refactor.', enabled: true },
      { name: 'debugging', description: 'Debug.', enabled: false },
    ]);
    expect(sections.bundled).toEqual([
      { name: 'pdf', description: 'PDFs.', enabled: true },
      { name: 'xlsx', description: 'Sheets.', enabled: false },
    ]);
    expect(sections.global).toEqual([
      { name: 'deploy', description: 'Deploy.', enabled: true },
      { name: 'audit', description: 'Audit.', enabled: false },
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
  it('extracts the project, bundled, and global skill pools with descriptions', () => {
    expect(
      normalizeScanSkills({
        skills: {
          project: [{ name: 'a', description: 'A skill.' }, ' '],
          bundled: ['b'],
          global: [{ name: 'c', description: '' }],
        },
      }),
    ).toEqual({
      project: [{ name: 'a', description: 'A skill.' }],
      bundled: [{ name: 'b', description: '' }],
      global: [{ name: 'c', description: '' }],
    });
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
