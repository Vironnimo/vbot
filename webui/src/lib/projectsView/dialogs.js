import { t } from '../i18n.js';
import {
  emptyScanSkills,
  createProjectAddForm,
  buildAddProjectPayload,
  normalizeDetectResult,
  presentFormats,
  shouldSuggestClaudeMd,
  buildRePointPayload,
  normalizeProject,
} from './presentation.js';

const PROJECT_BUSY_CODE = 'project_busy';

const PROJECT_IN_USE_CODE = 'project_in_use';

// Internal add/remove/re-point workflows share the Project controller lifecycle.
export function createProjectDialogs({
  state,
  operations,
  detectDelayMs,
  isActive,
  errorText,
  selectProject,
  onProjectAdded,
  flushPendingProjects,
  loadProjects,
}) {
  let detectTimer = null;

  function clearDetect() {
    if (detectTimer !== null) {
      clearTimeout(detectTimer);
      detectTimer = null;
    }
  }

  function scheduleDetect(cwd, onResult) {
    clearDetect();
    const path = typeof cwd === 'string' ? cwd.trim() : '';
    if (!path) {
      onResult(null, path);
      return;
    }
    detectTimer = setTimeout(async () => {
      detectTimer = null;
      try {
        const result = await operations.detectProject(path);
        if (isActive()) {
          onResult(normalizeDetectResult(result), path);
        }
      } catch {
        if (isActive()) {
          onResult(null, path);
        }
      }
    }, detectDelayMs);
  }

  function openAdd() {
    state.addForm = createProjectAddForm();
    state.addError = '';
    state.addDetect = null;
    state.isAddOpen = true;
  }

  function closeAdd() {
    if (state.addingProject) {
      return;
    }
    state.isAddOpen = false;
    state.addError = '';
    clearDetect();
    flushPendingProjects();
  }

  function updateAddField(field, value) {
    state.addForm[field] = value;
    state.addError = '';
    if (field !== 'cwd') {
      return;
    }
    scheduleDetect(value, (result, detectedCwd) => {
      if (!state.isAddOpen || state.addForm.cwd.trim() !== detectedCwd) {
        return;
      }
      state.addDetect = result;
    });
  }

  async function submitAdd() {
    if (state.addForm.cwd.trim().length === 0) {
      state.addError = t('projects.add.missingCwd');
      return;
    }
    state.addingProject = true;
    state.addError = '';
    state.statusMessage = '';
    try {
      const formats = state.addDetect ? presentFormats(state.addDetect) : [];
      const payload = buildAddProjectPayload({
        cwd: state.addForm.cwd,
        display_name: state.addForm.display_name,
        source_format: formats.length > 1 ? state.addForm.source_format : '',
        auto_load:
          state.addDetect &&
          shouldSuggestClaudeMd(state.addDetect) &&
          state.addForm.include_claude_md
            ? ['CLAUDE.md']
            : [],
      });
      const result = await operations.addProject(payload);
      if (!isActive()) {
        return;
      }
      const project = normalizeProject(result?.project);
      state.statusMessage = t('projects.add.success');
      state.isAddOpen = false;
      state.addForm = createProjectAddForm();
      state.addDetect = null;
      await loadProjects();
      if (isActive()) {
        selectProject(project.project_id, result?.scan);
        onProjectAdded(project.project_id);
      }
    } catch (error) {
      if (isActive()) {
        state.addError = `${t('projects.add.error')} ${errorText(error)}`;
      }
    } finally {
      if (isActive()) {
        state.addingProject = false;
        flushPendingProjects();
      }
    }
  }

  function openRemove(project) {
    state.removeConfirmProject = project;
    state.copyRootedAgentIdentityFiles = false;
    state.removePermanently = false;
  }

  function cancelRemove() {
    state.removeConfirmProject = null;
    flushPendingProjects();
  }

  function removeErrorText(error) {
    if (error?.code === PROJECT_BUSY_CODE) {
      return t('projects.remove.busy');
    }
    if (error?.code === PROJECT_IN_USE_CODE) {
      return t('projects.remove.inUse');
    }
    return `${t('projects.remove.error')} ${errorText(error)}`;
  }

  async function confirmRemove() {
    const project = state.removeConfirmProject;
    state.removeConfirmProject = null;
    if (!project) {
      flushPendingProjects();
      return;
    }
    state.removingProjectId = project.project_id;
    state.statusMessage = '';
    state.listError = '';
    state.editError = '';
    try {
      const result = await operations.removeProject(project.project_id, {
        copyRootedAgentIdentityFiles: state.copyRootedAgentIdentityFiles,
        permanent: state.removePermanently,
      });
      if (!isActive()) {
        return;
      }
      if (state.selectedProjectId === project.project_id) {
        state.selectedProjectId = '';
        state.activeTeam = [];
        state.activeReport = null;
        state.activeScanSkills = emptyScanSkills();
      }
      const affectedCount = Array.isArray(result?.affected_agent_ids)
        ? result.affected_agent_ids.length
        : 0;
      const copyState = state.copyRootedAgentIdentityFiles
        ? t('projects.remove.filesCopied')
        : t('projects.remove.filesNotCopied');
      const removed = !state.removePermanently
        ? t('projects.remove.archived')
        : result?.purge_pending
          ? t('archive.deletePending')
          : t('projects.remove.deletedPermanently');
      const reset =
        affectedCount === 0
          ? ''
          : affectedCount === 1
            ? t('projects.remove.resetOneAgent', { copyState })
            : t('projects.remove.resetManyAgents', {
                count: affectedCount,
                copyState,
              });
      state.statusMessage = [removed, reset].filter(Boolean).join(' ');
      await loadProjects();
    } catch (error) {
      if (!isActive()) {
        return;
      }
      const message = removeErrorText(error);
      if (state.selectedProjectId === project.project_id) {
        state.editError = message;
      } else {
        state.listError = message;
      }
    } finally {
      if (isActive()) {
        state.removingProjectId = '';
        flushPendingProjects();
      }
    }
  }

  function openRePoint(project) {
    state.rePointProject = project;
    state.rePointCwd = '';
    state.rePointError = '';
  }

  function closeRePoint() {
    if (state.rePointing) {
      return;
    }
    state.rePointProject = null;
    state.rePointError = '';
    flushPendingProjects();
  }

  async function submitRePoint() {
    if (!state.rePointProject) {
      return;
    }
    if (state.rePointCwd.trim().length === 0) {
      state.rePointError = t('projects.rePoint.missingCwd');
      return;
    }
    state.rePointing = true;
    state.rePointError = '';
    state.statusMessage = '';
    try {
      const projectId = state.rePointProject.project_id;
      const result = await operations.setProject(
        projectId,
        buildRePointPayload(state.rePointCwd),
      );
      if (!isActive()) {
        return;
      }
      state.statusMessage = t('projects.rePoint.success');
      state.rePointProject = null;
      await loadProjects();
      if (isActive() && state.selectedProjectId === projectId) {
        selectProject(projectId, result?.scan);
      }
    } catch (error) {
      if (isActive()) {
        state.rePointError = `${t('projects.rePoint.error')} ${errorText(error)}`;
      }
    } finally {
      if (isActive()) {
        state.rePointing = false;
        flushPendingProjects();
      }
    }
  }

  return {
    clearDetect,
    openAdd,
    closeAdd,
    updateAddField,
    submitAdd,
    openRemove,
    cancelRemove,
    confirmRemove,
    openRePoint,
    closeRePoint,
    submitRePoint,
  };
}
