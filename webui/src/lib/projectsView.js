// Pure view helpers for the Projects tab. Business and normalization logic
// lives here so the Svelte component stays a thin display/input/orchestration
// layer (see webui.md → Conventions). Every export is unit-tested in
// __tests__/projectsView.test.js.
//
// The shapes mirror the verified backend contract (server/rpc/project_methods):
//   project: { project_id, display_name, cwd, cwd_exists, default_agent,
//              default_model, auto_load[], created_at, updated_at }
//   scan:    { team: [member…], report: { clean, findings: [finding…] } }
//   finding: { type, detail, agent_id, source_path }

// The scan report's `finding.type` discriminants (server scan_report.py).
export const FINDING_TYPE_SLUG_COLLISION = 'slug_collision';
export const FINDING_TYPE_UNSLUGIFIABLE_NAME = 'unslugifiable_name';
export const FINDING_TYPE_BAD_MODEL = 'bad_model';
export const FINDING_TYPE_ORPHAN = 'orphan';

// Stable display order for grouped findings, so the report always lists the
// same finding kinds in the same order regardless of server ordering.
export const FINDING_TYPES = Object.freeze([
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
]);

// The mutable fields a manage form can change through project.set. cwd is
// handled by the dedicated re-point path, so it is not part of the generic
// manage diff here.
const MANAGE_FIELDS = Object.freeze([
  'display_name',
  'default_agent',
  'default_model',
]);

// Build the project.add payload from the add-form values. cwd is required (the
// thin api wrapper enforces it too); the optional pointers are only included
// when the user actually typed something, matching the backend's
// "non-empty string" rule for these params.
export function buildAddProjectPayload(formValues) {
  const payload = {
    cwd: asText(formValues?.cwd).trim(),
  };

  const displayName = optionalText(formValues?.display_name);
  if (displayName !== null) {
    payload.display_name = displayName;
  }

  const defaultAgent = optionalText(formValues?.default_agent);
  if (defaultAgent !== null) {
    payload.default_agent = defaultAgent;
  }

  const defaultModel = optionalText(formValues?.default_model);
  if (defaultModel !== null) {
    payload.default_model = defaultModel;
  }

  const autoLoad = normalizeAutoLoad(formValues?.auto_load);
  if (autoLoad.length > 0) {
    payload.auto_load = autoLoad;
  }

  return payload;
}

// Build the sparse project.set changes for a manage form: only fields whose
// value actually differs from the current project, and at least one (callers
// must guard with `hasManageChanges` before sending). project.set rejects an
// empty change set, so this never produces one silently — an unchanged form
// yields `{}` and the caller short-circuits.
//
// auto_load is compared as an ordered list; display_name / default_agent /
// default_model compare as trimmed strings. A pointer field
// (default_agent/default_model) cleared to empty is sent as `null` — the
// backend's `_optional_string` rejects a sent empty string with
// `invalid_request`, and only maps JSON `null` (None) to "" to clear the
// pointer (fall through the model chain). A non-empty pointer is sent as the
// trimmed string. display_name cannot be cleared (it is a required non-empty
// field) so an empty display_name is treated as no change.
export function buildManageProjectPayload(formValues, project) {
  const changes = {};

  for (const field of MANAGE_FIELDS) {
    const next = asText(formValues?.[field]).trim();
    const current = asText(project?.[field]).trim();
    if (next === current) {
      continue;
    }
    if (field === 'display_name' && next === '') {
      // display_name is required non-empty; an empty box is not a clear.
      continue;
    }
    // A cleared pointer must be sent as null (the backend maps None → "" to
    // clear it); a sent empty string would be rejected as invalid_request.
    changes[field] = next === '' ? null : next;
  }

  const nextAutoLoad = normalizeAutoLoad(formValues?.auto_load);
  const currentAutoLoad = normalizeAutoLoad(project?.auto_load);
  if (!sameStringList(nextAutoLoad, currentAutoLoad)) {
    changes.auto_load = nextAutoLoad;
  }

  return changes;
}

// Whether a manage payload carries at least one change (project.set needs ≥1).
export function hasManageChanges(changes) {
  return isPlainObject(changes) && Object.keys(changes).length > 0;
}

// A project's cwd no longer resolves to a directory → offer Re-Point. The flag
// is server-computed (`cwd_exists`); only an explicit `false` triggers it, so a
// missing/undefined flag never forces the re-point UI.
export function needsRePoint(project) {
  return project?.cwd_exists === false;
}

// The change set for a Re-Point: project.set with the new cwd only. The caller
// passes the project_id separately to setProject, so this is just `{ cwd }`.
export function buildRePointPayload(cwd) {
  return { cwd: asText(cwd).trim() };
}

// Normalize one project record from the backend into a stable display shape.
export function normalizeProject(project) {
  return {
    project_id: asText(project?.project_id),
    display_name: asText(project?.display_name),
    cwd: asText(project?.cwd),
    cwd_exists: project?.cwd_exists === true,
    default_agent: asText(project?.default_agent),
    default_model: asText(project?.default_model),
    auto_load: normalizeAutoLoad(project?.auto_load),
    created_at: optionalText(project?.created_at),
    updated_at: optionalText(project?.updated_at),
  };
}

export function normalizeProjects(projects) {
  const raw = Array.isArray(projects) ? projects : [];
  return raw.map((project) => normalizeProject(project));
}

// Project the scan's team into a stable, display-ready list. The repo is the
// source of truth (no copy drift) — this only shapes what the view renders.
export function projectTeam(scan) {
  const raw = Array.isArray(scan?.team) ? scan.team : [];
  return raw.map((member) => ({
    agent_id: asText(member?.agent_id),
    display_name: asText(member?.display_name) || asText(member?.agent_id),
    description: asText(member?.description),
    model: asText(member?.model),
    temperature:
      typeof member?.temperature === 'number' ? member.temperature : null,
    source_format: asText(member?.source_format),
    source_path: asText(member?.source_path),
  }));
}

// Normalize the scan report into a render-ready shape: the `clean` flag plus
// findings grouped by type in a stable order. An empty / clean report is the
// normal case (a bare or empty repo), NOT an error — `clean` is true and
// `groups` is empty, and callers must treat that as a healthy project.
export function normalizeScanReport(report) {
  const rawFindings = Array.isArray(report?.findings) ? report.findings : [];
  const findings = rawFindings.map((finding) => ({
    type: asText(finding?.type),
    detail: asText(finding?.detail),
    agent_id: asText(finding?.agent_id),
    source_path: optionalText(finding?.source_path),
  }));

  const groups = FINDING_TYPES.map((type) => ({
    type,
    findings: findings.filter((finding) => finding.type === type),
  })).filter((group) => group.findings.length > 0);

  // The server's `clean` flag is authoritative; fall back to "no findings" only
  // when it is absent so a malformed payload still renders sensibly.
  const clean =
    typeof report?.clean === 'boolean' ? report.clean : findings.length === 0;

  return {
    clean,
    findingCount: findings.length,
    findings,
    groups,
  };
}

function normalizeAutoLoad(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => asText(item).trim())
    .filter((item) => item.length > 0);
}

function sameStringList(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((item, index) => item === right[index]);
}

function optionalText(value) {
  const normalized = asText(value).trim();
  return normalized ? normalized : null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
