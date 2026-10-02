// Background activity: the long-running server work Settings > General lists
// in one place. The server publishes the list (`activity_status`); this file
// turns each entry into what a row shows and which actions it offers.

import { t, tOr } from '../i18n.js';
import { describeDownloadProgress } from './localModels.js';
import { countText, etaText, indexErrorText } from './recall.js';
import { textOrEmpty } from './values.js';

// Per task of a local Model installation: its row title and the Settings
// section that manages it.
const LOCAL_MODEL_TASKS = {
  speech_to_text: {
    title: (model) => t('settings.activity.task.speech_to_text', { model }),
    section: 'speech_models',
  },
  text_to_speech: {
    title: (model) => t('settings.activity.task.text_to_speech', { model }),
    section: 'speech_models',
  },
  text_embedding: {
    title: (model) => t('settings.activity.task.text_embedding', { model }),
    section: 'recall',
  },
};

function sectionFor(entry) {
  if (entry.kind === 'whatsapp_setup') return 'channels';
  if (entry.kind === 'recall_index') return 'recall';
  return LOCAL_MODEL_TASKS[entry.task_type]?.section ?? '';
}

function titleFor(entry) {
  if (entry.kind === 'whatsapp_setup') {
    return t('settings.activity.whatsapp', { channel: entry.label });
  }
  if (entry.kind === 'recall_index') return t('settings.activity.recallIndex');
  return LOCAL_MODEL_TASKS[entry.task_type]?.title(entry.label) ?? entry.label;
}

function progressFor(entry) {
  const progress = entry.progress;
  if (!progress) return null;
  if (progress.unit === 'bytes') return describeDownloadProgress(progress);
  const total = Number(progress.total);
  if (!Number.isFinite(total) || total <= 0) return null;
  const completed = Math.min(
    Math.max(Number(progress.completed) || 0, 0),
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

function runningText(entry) {
  if (entry.kind === 'whatsapp_setup') {
    return t('settings.channels.whatsapp.installing');
  }
  if (entry.kind === 'recall_index') {
    return entry.phase === 'retrying'
      ? t('settings.activity.retrying')
      : t('settings.activity.indexing');
  }
  if (entry.phase === 'downloading' && entry.progress) {
    return t('settings.activity.downloadingModel');
  }
  return tOr(
    `settings.localModel.phase.${textOrEmpty(entry.phase)}`,
    t('settings.localModel.phase.installing'),
  );
}

function failureText(entry) {
  if (entry.kind === 'recall_index') {
    return indexErrorText({ code: entry.error });
  }
  if (entry.kind === 'whatsapp_setup') {
    return textOrEmpty(entry.message) || t('settings.activity.whatsappFailed');
  }
  return tOr(
    `settings.localModel.error.${textOrEmpty(entry.error)}`,
    t('settings.localModel.error.install_failed'),
  );
}

function statusText(entry) {
  if (entry.state === 'running') return runningText(entry);
  if (entry.state === 'failed') return failureText(entry);
  if (entry.state === 'action_required') {
    return t('settings.localModel.state.restart_required');
  }
  return t('settings.activity.completed');
}

/**
 * What one background activity row shows: `{ id, title, status, warn,
 * progress, section, cancelTarget, dismissible }`. `progress` is
 * `{ percent, text }` or null, `section` the Settings section that manages
 * the work ('' when none), and `cancelTarget` the local target a running
 * installation is cancelled through ('' when it cannot be cancelled here).
 */
export function describeBackgroundActivity(entry) {
  const running = entry.state === 'running';
  return {
    id: entry.id,
    title: titleFor(entry),
    status: statusText(entry),
    warn: entry.state === 'failed' || entry.state === 'action_required',
    progress: running ? progressFor(entry) : null,
    section: sectionFor(entry),
    cancelTarget:
      running && entry.kind === 'local_model_install'
        ? textOrEmpty(entry.target)
        : '',
    dismissible: !running,
  };
}
