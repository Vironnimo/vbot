// What the server is working on in the background: downloads and
// installations of local Models, WhatsApp support installations and large
// Conversation search indexing passes. The server publishes the list
// (`activity_status`); Settings > General shows it at the top while it has
// entries. This file turns each entry into a row that says what happens.

import { t, tOr } from '../i18n.js';
import { describeDownloadProgress } from './localModels.js';
import { countText, etaText, indexErrorText } from './recall.js';
import { textOrEmpty } from './values.js';

// The Settings section that manages each task's local Models.
const SECTION_BY_TASK = {
  speech_to_text: 'speech_models',
  text_to_speech: 'speech_models',
  text_embedding: 'recall',
};

function itemProgress(entry) {
  const total = Number(entry.progress?.total);
  if (!Number.isFinite(total) || total <= 0) return null;
  const completed = Math.min(
    Math.max(Number(entry.progress.completed) || 0, 0),
    total,
  );
  const eta = etaText('indexing', entry.eta_seconds);
  const text = t('settings.recall.status.progress', {
    indexed: countText(completed),
    total: countText(total),
  });
  return {
    percent: Math.floor((completed / total) * 100),
    text: eta ? `${text} · ${eta}` : text,
  };
}

function describeModelInstall(entry) {
  const model = entry.label;
  if (entry.state === 'running') {
    const progress =
      entry.phase === 'downloading'
        ? describeDownloadProgress(entry.progress)
        : null;
    if (progress) {
      return {
        title: t('settings.activity.downloading', { model }),
        detail: '',
        progress,
      };
    }
    return {
      title: t('settings.activity.installing', { model }),
      detail: tOr(
        `settings.localModel.phase.${textOrEmpty(entry.phase)}`,
        t('settings.localModel.phase.installing'),
      ),
    };
  }
  if (entry.state === 'failed') {
    return {
      title: t('settings.activity.installFailed', { model }),
      detail: tOr(
        `settings.localModel.error.${textOrEmpty(entry.error)}`,
        t('settings.localModel.error.install_failed'),
      ),
    };
  }
  return {
    title: t('settings.activity.installed', { model }),
    detail:
      entry.state === 'action_required'
        ? t('settings.activity.restartToUse')
        : '',
  };
}

function describeWhatsAppSetup(entry) {
  const channel = entry.label;
  if (entry.state === 'running') {
    return {
      title: t('settings.activity.whatsappInstalling', { channel }),
      detail: '',
    };
  }
  if (entry.state === 'failed') {
    return {
      title: t('settings.activity.whatsappFailed', { channel }),
      detail:
        textOrEmpty(entry.message) || t('settings.activity.whatsappRetry'),
    };
  }
  return {
    title: t('settings.activity.whatsappInstalled', { channel }),
    detail: '',
  };
}

function describeRecallIndex(entry) {
  if (entry.state === 'running') {
    return {
      title: t('settings.activity.indexing'),
      detail:
        entry.phase === 'retrying' ? t('settings.activity.indexRetrying') : '',
      progress: itemProgress(entry),
    };
  }
  if (entry.state === 'failed') {
    const reason = indexErrorText({ code: entry.error });
    // The generic reason only repeats the title.
    const generic = t('settings.recall.indexError.generic');
    return {
      title: t('settings.activity.indexFailed'),
      detail: reason === generic ? '' : reason,
    };
  }
  return { title: t('settings.activity.indexed'), detail: '' };
}

function sectionFor(entry) {
  if (entry.kind === 'whatsapp_setup') return 'channels';
  if (entry.kind === 'recall_index') return 'recall';
  return SECTION_BY_TASK[entry.task_type] ?? '';
}

/**
 * One row: `{ id, title, detail, warn, progress, section, cancelTarget,
 * dismissible }`. `title` says what happens ("Downloading Parakeet"),
 * `detail` adds the phase or problem ('' when the title says it all),
 * `progress` is `{ percent, text }` or null, `section` the Settings section
 * that manages the work ('' when none), and `cancelTarget` the local target a
 * running installation is cancelled through ('' when it cannot be cancelled).
 */
export function describeBackgroundActivity(entry) {
  const running = entry.state === 'running';
  const describe =
    entry.kind === 'whatsapp_setup'
      ? describeWhatsAppSetup
      : entry.kind === 'recall_index'
        ? describeRecallIndex
        : describeModelInstall;
  const { title, detail, progress = null } = describe(entry);
  return {
    id: entry.id,
    title,
    detail,
    warn: entry.state === 'failed' || entry.state === 'action_required',
    progress: running ? progress : null,
    section: sectionFor(entry),
    cancelTarget:
      running && entry.kind === 'local_model_install'
        ? textOrEmpty(entry.target)
        : '',
    dismissible: !running,
  };
}
