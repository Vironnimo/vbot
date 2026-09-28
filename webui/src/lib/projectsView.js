// Public Projects surface. Internal modules own editor reconciliation,
// dialog workflows and pure form/team/scan projections.
export { createProjectsController } from './projectsView/controller.js';
export {
  createProjectsState,
  PROJECT_SOURCE_FORMATS,
  PROJECT_THINKING_EFFORT_NO_DEFAULT,
  PROJECT_THINKING_EFFORT_OPTIONS,
  buildToolToggleList,
  buildSkillToggleSections,
  hasManageChanges,
  buildDefaultAgentOptions,
  presentFormats,
  shouldSuggestClaudeMd,
  needsRePoint,
  projectTeam,
  projectAgentTargetSummary,
  memberFieldIsOverridden,
  normalizeScanReport,
} from './projectsView/presentation.js';
