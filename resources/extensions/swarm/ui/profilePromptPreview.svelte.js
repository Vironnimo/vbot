import { t, tOr } from '../../../../webui/src/lib/i18n.js';
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
    const request = ++previewRequest;
    const submitted = `${previewFormation}:${context.snapshot()}`;
    previewBusy = true;
    previewError = '';
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
    'tool:project': () =>
      t('swarm.profile.promptBlock.project', 'Project Tool guidance'),
    'tool:subagent': () =>
      t('swarm.profile.promptBlock.subagent', 'Subagent Tool guidance'),
    'tool:bash': () => t('swarm.profile.promptBlock.bash', 'Bash environment'),
  };
  const blockTitle = (id) =>
    tOr(`systemPrompt.blockTitle.${id}`, toolBlockTitles[id]?.() ?? id);

  return {
    blockTitle,
    get preview() {
      return preview;
    },
    set preview(value) {
      preview = value;
    },
    get previewBusy() {
      return previewBusy;
    },
    set previewBusy(value) {
      previewBusy = value;
    },
    get previewError() {
      return previewError;
    },
    set previewError(value) {
      previewError = value;
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
    get inspectPrompt() {
      return inspectPrompt;
    },
  };
}
