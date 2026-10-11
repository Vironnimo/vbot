import { t, tOr } from '../../../../webui/src/lib/i18n.js';

// The Swarm editor's prompt preview for one formation row: it requests the
// combined System Prompt of the current draft (`profiles.preview`), keeps
// only the newest request's answer and tells whether the shown preview still
// matches the draft. `context.ready()` resolves whether the draft can be
// previewed; when it cannot, the editor shows the problems and nothing is
// requested.
export function createProfilePromptPreview(context) {
  let preview = $state(null);
  let previewSnapshot = $state('');
  let previewBusy = $state(false);
  let previewError = $state('');
  let previewFormation = $state(0);
  let previewRequest = 0;

  const previewCurrent = $derived(
    preview && previewSnapshot === `${previewFormation}:${context.snapshot()}`,
  );

  async function inspectPrompt() {
    previewError = '';
    if (!(await context.ready())) return;
    const request = ++previewRequest;
    const submitted = `${previewFormation}:${context.snapshot()}`;
    previewBusy = true;
    try {
      const result = await context.bridgeClient.operation('profiles.preview', {
        profile: context.profilePayload(),
        formation_index: Number(previewFormation),
      });
      if (request !== previewRequest) return;
      preview = result.preview;
      previewSnapshot = submitted;
    } catch (cause) {
      if (request === previewRequest) previewError = cause.message;
    } finally {
      if (request === previewRequest) previewBusy = false;
    }
  }
  // Tool guidance blocks the core catalog has no title for.
  const toolBlockTitles = {
    'tool:project': () => t('swarm.profile.promptBlock.project'),
    'tool:subagent': () => t('swarm.profile.promptBlock.subagent'),
    'tool:bash': () => t('swarm.profile.promptBlock.bash'),
  };
  const blockTitle = (id) =>
    tOr(`systemPrompt.blockTitle.${id}`, toolBlockTitles[id]?.() ?? id);

  return {
    blockTitle,
    inspectPrompt,
    get preview() {
      return preview;
    },
    get previewBusy() {
      return previewBusy;
    },
    get previewError() {
      return previewError;
    },
    get previewFormation() {
      return previewFormation;
    },
    set previewFormation(value) {
      previewFormation = value;
    },
    get previewCurrent() {
      return previewCurrent;
    },
  };
}
