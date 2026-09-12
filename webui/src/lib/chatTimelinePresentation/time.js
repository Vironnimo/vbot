import { activeLocaleTag, t } from '$lib/i18n.js';
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
    return t('chat.today', 'Today');
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return t('chat.today', 'Today');
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

export function formatDurationMs(
  durationMs,
  i18nKey = 'chat.runDurationSeconds',
) {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return '';
  }
  const elapsedSeconds = durationMs / 1000;
  if (elapsedSeconds < 10) {
    return t(i18nKey, '{seconds}s', {
      seconds: elapsedSeconds.toFixed(1),
    });
  }
  if (elapsedSeconds < 60) {
    return t(i18nKey, '{seconds}s', {
      seconds: Math.round(elapsedSeconds),
    });
  }
  const totalSeconds = Math.round(elapsedSeconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return t('chat.durationHoursMinutes', '{hours}h {minutes}m', {
      hours,
      minutes,
    });
  }
  return t('chat.durationMinutesSeconds', '{minutes}m {seconds}s', {
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
