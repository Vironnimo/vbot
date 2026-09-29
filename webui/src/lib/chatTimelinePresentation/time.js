import { activeLocaleTag, t } from '$lib/i18n.js';
import { formatMoment } from '$lib/timeText.js';
import {
  formatDateTimeInApplicationZone,
  dateKeyInApplicationZone,
} from '$lib/dateTimePrefs.svelte.js';

export const formatTime = (timestamp) => {
  if (!timestamp) {
    return '';
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return formatDateTimeInApplicationZone(date, activeLocaleTag(), {
    hour: 'numeric',
    minute: '2-digit',
  });
};

export const formatDate = (timestamp) => {
  const dateKey = dateKeyForTimestamp(timestamp);
  if (isTodayDateKey(dateKey)) {
    return t('chat.today');
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return t('chat.today');
  }
  return formatDateTimeInApplicationZone(date, activeLocaleTag(), {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
};

export function dateKeyForTimestamp(timestamp) {
  if (!timestamp) {
    return todayDateKey();
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return todayDateKey();
  }
  return dateKeyForDate(date);
}

export function formatDurationMs(durationMs) {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return '';
  }
  const elapsedSeconds = durationMs / 1000;
  if (elapsedSeconds < 10) {
    return t('chat.durationSeconds', {
      seconds: elapsedSeconds.toFixed(1),
    });
  }
  if (elapsedSeconds < 60) {
    return t('chat.durationSeconds', {
      seconds: Math.round(elapsedSeconds),
    });
  }
  const totalSeconds = Math.round(elapsedSeconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return t('chat.durationHoursMinutes', {
      hours,
      minutes,
    });
  }
  return t('chat.durationMinutesSeconds', {
    minutes,
    seconds,
  });
}

export function timestampToMs(timestamp) {
  if (!timestamp) {
    return null;
  }
  const value = new Date(timestamp).getTime();
  return Number.isNaN(value) ? null : value;
}

export function elapsedSinceTimestamp(timestamp, nowMs) {
  const start = timestampToMs(timestamp);
  if (start === null || !Number.isFinite(nowMs)) {
    return null;
  }
  return Math.max(0, nowMs - start);
}

function dateKeyForDate(date) {
  return dateKeyInApplicationZone(date);
}

function todayDateKey() {
  return dateKeyForDate(new Date());
}

function isTodayDateKey(dateKey) {
  return dateKey === todayDateKey();
}

/**
 * Details rows for something that runs (a Tool call, a Sub-Agent Run, a
 * background process, a Run): when it started and, once settled, when it
 * finished, plus how long it ran or has been running. Unknown moments and
 * durations are left out, and so is a finish within the second it started.
 */
export function executionDetailRows({
  startedAt = '',
  finishedAt = '',
  durationMs = null,
  running = false,
  nowMs = Date.now(),
}) {
  const rows = [];
  const started = formatMoment(startedAt, { nowMs, seconds: true });
  if (started) {
    rows.push({ label: t('chat.details.started'), value: started });
  }
  const finished = running
    ? ''
    : formatMoment(finishedAt, { nowMs, seconds: true });
  if (finished && finished !== started) {
    rows.push({ label: t('chat.details.finished'), value: finished });
  }
  const duration = formatDurationMs(durationMs);
  if (duration) {
    rows.push({
      label: running
        ? t('chat.details.runningFor')
        : t('chat.details.duration'),
      value: duration,
    });
  }
  return rows;
}
