// A local Model that vBot runs itself and installs on request: its download
// facts before installation, and what its installation job reports, as one
// translated line, an optional download progress and the one action that
// helps next.

import { activeLocaleTag, t, tOr } from '../i18n.js';
import { textOrEmpty } from './values.js';

const BYTES_PER_MEGABYTE = 1_000_000;
const BYTES_PER_GIGABYTE = 1_000_000_000;

function finiteOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// A download size in megabytes or gigabytes: "347 MB", "1.2 GB".
export function formatDownloadSize(bytes) {
  const value = finiteOrNull(bytes);
  if (value === null || value < 0) {
    return '';
  }
  const [unit, size] =
    value >= BYTES_PER_GIGABYTE
      ? ['gigabyte', value / BYTES_PER_GIGABYTE]
      : ['megabyte', value / BYTES_PER_MEGABYTE];
  return new Intl.NumberFormat(activeLocaleTag(), {
    style: 'unit',
    unit,
    unitDisplay: 'short',
    maximumFractionDigits: size < 10 ? 1 : 0,
  }).format(size);
}

// "347 MB download · Apache-2.0 license" from a local target's metadata;
// either part is left out when unknown.
export function describeLocalModelDownload(metadata) {
  const size = formatDownloadSize(metadata?.download_bytes);
  const license = textOrEmpty(metadata?.license);
  return [
    size ? t('settings.localModel.downloadSize', { size }) : '',
    license ? t('settings.localModel.license', { license }) : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

// "1.2 GB of 4.1 GB" with its percentage from an installation's byte
// progress; null when the status reports none.
export function describeDownloadProgress(progress) {
  const completed = finiteOrNull(progress?.completed);
  const total = finiteOrNull(progress?.total);
  if (completed === null || total === null || total <= 0) {
    return null;
  }
  const done = Math.min(Math.max(completed, 0), total);
  return {
    percent: Math.floor((done / total) * 100),
    text: t('settings.localModel.progress', {
      completed: formatDownloadSize(done),
      total: formatDownloadSize(total),
    }),
  };
}

function installingMessage(status, progress) {
  if (status?.phase === 'downloading' && progress) {
    return t('settings.localModel.downloadingModel');
  }
  return tOr(
    `settings.localModel.phase.${textOrEmpty(status?.phase)}`,
    t('settings.localModel.phase.installing'),
  );
}

/**
 * The install row of one local Model.
 *
 * @param {{ state: string, status: object | null, error: string }} job - The
 *   installation job: its state (`checking` before the first status), the
 *   last status and a client-side error code (`connection`, ...).
 * @returns {{ tone: 'neutral' | 'warn', message: string,
 *   progress: { percent: number, text: string } | null,
 *   action: 'install' | 'retry' | 'check' | 'installing' | null }}
 */
export function describeLocalModelSetup(job) {
  const status = job?.status ?? null;
  if (job?.error) {
    return {
      tone: 'warn',
      message: tOr(
        `settings.localModel.error.${job.error}`,
        t('settings.localModel.error.connection'),
      ),
      progress: null,
      action: 'check',
    };
  }
  switch (job?.state) {
    case 'installing': {
      const progress = describeDownloadProgress(status?.progress);
      return {
        tone: 'neutral',
        message: installingMessage(status, progress),
        progress,
        action: 'installing',
      };
    }
    case 'failed':
      return {
        tone: 'warn',
        message: tOr(
          `settings.localModel.error.${textOrEmpty(status?.error)}`,
          t('settings.localModel.error.install_failed'),
        ),
        progress: null,
        action: 'retry',
      };
    case 'missing':
      return {
        tone: 'neutral',
        message: t('settings.localModel.state.missing'),
        progress: null,
        action: 'install',
      };
    case 'ready':
      return {
        tone: 'neutral',
        message: t('settings.localModel.state.ready'),
        progress: null,
        action: null,
      };
    case 'restart_required':
      return {
        tone: 'neutral',
        message: t('settings.localModel.state.restart_required'),
        progress: null,
        action: null,
      };
    default:
      return {
        tone: 'neutral',
        message: t('settings.localModel.state.checking'),
        progress: null,
        action: null,
      };
  }
}
