import { describe, expect, it } from 'vitest';

import {
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  buildAddProjectPayload,
  buildManageProjectPayload,
  buildRePointPayload,
  hasManageChanges,
  needsRePoint,
  normalizeProject,
  normalizeProjects,
  normalizeScanReport,
  projectTeam,
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
      auto_load: ['AGENTS.md'],
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T01:00:00Z',
    });
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
            source_format: 'opencode',
            source_path: '.opencode/agents/builder.md',
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
        source_format: 'opencode',
        source_path: '.opencode/agents/builder.md',
      },
      {
        agent_id: 'planner',
        display_name: 'planner',
        description: '',
        model: '',
        temperature: null,
        source_format: '',
        source_path: '',
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
