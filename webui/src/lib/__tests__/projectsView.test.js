import { describe, expect, it } from 'vitest';
import {
  PROJECT_SOURCE_FORMATS,
  PROJECT_THINKING_EFFORT_NO_DEFAULT,
  buildAddProjectPayload,
  buildDefaultAgentOptions,
  buildManageProjectPayload,
  buildRePointPayload,
  hasManageChanges,
  needsRePoint,
  normalizeDetectResult,
  normalizeProject,
  normalizeProjects,
  presentFormats,
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

  it('clears an emptied optional display_name to its id default', () => {
    const changes = buildManageProjectPayload(
      {
        display_name: '',
        default_agent: 'builder',
        default_model: 'openai/gpt-5.2',
        auto_load: ['AGENTS.md'],
      },
      project,
    );
    expect(changes).toEqual({ display_name: null });
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
