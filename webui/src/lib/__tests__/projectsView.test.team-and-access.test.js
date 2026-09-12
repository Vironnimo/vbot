import { describe, expect, it } from 'vitest';
import {
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNAVAILABLE_TOOL,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  buildManageProjectPayload,
  buildSkillToggleSections,
  buildToolToggleList,
  memberFieldIsOverridden,
  normalizeOverrideTemperature,
  normalizeScanReport,
  normalizeScanSkills,
  projectAgentTargetSummary,
  projectTeam,
  seedTeamOverrideDraft,
  setListMembership,
} from '../projectsView.js';

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
            tools: { subagent: { allowed_agents: ['builder'] } },
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
        tools: { subagent: { allowed_agents: ['builder'] } },
        // The per-agent override object (subset of the three fields), or null.
        overrides: { model: 'openai/gpt-mini' },
        // Provenance-aware resolved values per run field.
        effective: {
          model: { value: 'openai/gpt-mini', source: 'override' },
          temperature: { value: 0.2, source: 'agent' },
          thinking_effort: { value: 'high', source: 'agent' },
          tool_access: { value: { mode: 'all' }, source: null },
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
        tools: {},
        // No override → null; effective defaults to a stable null-per-field map.
        overrides: null,
        effective: {
          model: { value: null, source: null },
          temperature: { value: null, source: null },
          thinking_effort: { value: null, source: null },
          tool_access: { value: { mode: 'all' }, source: null },
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

  it('summarizes repository-owned Project Agent targets against the Team', () => {
    const team = [{ agent_id: 'builder' }, { agent_id: 'reviewer' }];

    expect(
      projectAgentTargetSummary(
        {
          agent_id: 'builder',
          tools: { subagent: { allowed_agents: [] } },
        },
        team,
      ),
    ).toEqual({ mode: 'self', agents: [] });
    expect(
      projectAgentTargetSummary(
        {
          agent_id: 'builder',
          tools: { subagent: { allowed_agents: ['reviewer'] } },
        },
        team,
      ),
    ).toEqual({ mode: 'all', agents: ['reviewer'] });
    expect(
      projectAgentTargetSummary(
        {
          agent_id: 'builder',
          tools: { subagent: { allowed_agents: ['reviewer'] } },
        },
        [...team, { agent_id: 'tester' }],
      ),
    ).toEqual({ mode: 'limited', agents: ['reviewer'] });
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
    expect(memberFieldIsOverridden(member, 'tool_access')).toBe(false);
    expect(
      memberFieldIsOverridden(
        { ...member, overrides: { tool_access: { mode: 'none' } } },
        'tool_access',
      ),
    ).toBe(true);
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
      compaction_policy: null,
      tool_access: { mode: 'all' },
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
      compaction_policy: null,
      tool_access: { mode: 'all' },
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
        {
          type: FINDING_TYPE_UNAVAILABLE_TOOL,
          detail: 'extension tool is unavailable',
        },
      ],
    });

    expect(report.clean).toBe(false);
    expect(report.findingCount).toBe(6);
    expect(report.groups.map((group) => group.type)).toEqual([
      FINDING_TYPE_SLUG_COLLISION,
      FINDING_TYPE_UNSLUGIFIABLE_NAME,
      FINDING_TYPE_BAD_MODEL,
      FINDING_TYPE_ORPHAN,
      FINDING_TYPE_UNAVAILABLE_TOOL,
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
  it('uses server-owned Project configurability metadata instead of tool names', () => {
    const rows = buildToolToggleList({
      catalog: [
        { name: 'read' },
        { name: 'edit' },
        { name: 'memory', project_configurable: false },
        { name: 'skill' },
        { name: 'server_policy_tool', project_configurable: false },
      ],
      allowedTools: ['read', 'skill'],
    });

    // The browser knows only the metadata contract: even an arbitrary future policy
    // tool is excluded without adding its name here. Each row carries the readiness
    // fields (defaulting to ready) so a not-ready tool renders the shared notice.
    expect(rows).toEqual([
      {
        name: 'edit',
        family: null,
        family_label: null,
        description: '',
        enabled: false,
        ready: true,
        readiness_hint: null,
        extension: null,
      },
      {
        name: 'read',
        family: null,
        family_label: null,
        description: '',
        enabled: true,
        ready: true,
        readiness_hint: null,
        extension: null,
      },
      {
        name: 'skill',
        family: null,
        family_label: null,
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
        family: null,
        family_label: null,
        description: 'Talk to Home Assistant.',
        enabled: false,
        ready: false,
        readiness_hint: 'Set the Home Assistant token first.',
        extension: 'homeassistant',
      },
    ]);
  });

  it('keeps a persisted tool missing from the catalog visible and removable', () => {
    const rows = buildToolToggleList({
      catalog: [{ name: 'read' }],
      allowedTools: ['read', 'disabled_extension_tool'],
    });

    expect(rows).toEqual([
      {
        name: 'disabled_extension_tool',
        family: null,
        family_label: null,
        description: '',
        enabled: true,
        ready: false,
        readiness_hint: null,
        extension: null,
        registered: false,
      },
      {
        name: 'read',
        family: null,
        family_label: null,
        description: '',
        enabled: true,
        ready: true,
        readiness_hint: null,
        extension: null,
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

  it('preserves an Extension family label for grouped project Tools', () => {
    const rows = buildToolToggleList({
      catalog: [
        {
          name: 'ha_get_state',
          family: 'extension:homeassistant:home_assistant',
          family_label: 'Home Assistant',
        },
      ],
    });

    expect(rows[0]).toMatchObject({
      family: 'extension:homeassistant:home_assistant',
      family_label: 'Home Assistant',
    });
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
