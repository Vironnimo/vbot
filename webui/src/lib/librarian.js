// The built-in Librarian as the WebUI shows it: the hidden Agent that runs
// Skill maintenance, why it can be unavailable, and how a pass went. The
// server owns the Librarian (`librarian.overview`, `librarian.status`); these
// helpers only present it.
import { t } from './i18n.js';

// The reserved id of the Librarian Agent. It is not in the Agent list, so a
// view that shows one of its Sessions names it itself.
export const LIBRARIAN_AGENT_ID = 'librarian';

export function librarianName() {
  return t('librarian.name');
}

// Why the Librarian is unavailable (`problem` of `librarian.overview`,
// `librarian_problem` of `librarian.status`), with the fix.
export function librarianProblemText(problem) {
  switch (problem) {
    case 'agent_id_taken':
      return t('librarian.problem.agentIdTaken');
    case 'invalid_config':
      return t('librarian.problem.invalidConfig');
    case 'missing':
      return t('librarian.problem.missing');
    default:
      return t('librarian.problem.unknown');
  }
}

export function librarianTriggerText(pass) {
  return pass?.trigger === 'manual'
    ? t('skills.librarian.triggerManual')
    : t('skills.librarian.triggerSchedule');
}

// How the merge step of a pass ended, with what it changed.
export function librarianMergeText(pass) {
  const merged = pass?.merged ?? 0;
  const changed = pass?.changed ?? 0;
  const created = pass?.created ?? 0;
  switch (pass?.consolidation) {
    case 'ran':
      return t('skills.librarian.mergeRan', { merged, changed, created });
    case 'failed':
      return t('skills.librarian.mergeFailed', { merged, changed, created });
    case 'unchanged':
      return t('skills.librarian.mergeUnchanged');
    case 'too_few':
      return t('skills.librarian.mergeTooFew');
    default:
      return t('skills.librarian.mergeOff');
  }
}
