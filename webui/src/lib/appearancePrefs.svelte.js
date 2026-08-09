// Small app-wide reactive store for pure display preferences from the
// appearance settings section. Unlike recall/skills, these values have no
// runtime reload hook — they only drive Chat presentation, so a tiny reactive
// singleton is enough. App seeds it from `settings.get`; Appearance updates it
// on save so the open Chat changes without a reload.

import {
  CHAT_WIDTH_OPTIONS,
  CHAT_WORKING_MODE_OPTIONS,
  DEFAULT_CHAT_WIDTH,
  DEFAULT_CHAT_WORKING_MODE,
} from './settingsView.js';

export const appearancePrefs = $state({
  chatWidth: DEFAULT_CHAT_WIDTH,
  chatWorkingMode: DEFAULT_CHAT_WORKING_MODE,
});

export function setChatWidth(value) {
  appearancePrefs.chatWidth = CHAT_WIDTH_OPTIONS.includes(value)
    ? value
    : DEFAULT_CHAT_WIDTH;
}

export function setChatWorkingMode(value) {
  appearancePrefs.chatWorkingMode = CHAT_WORKING_MODE_OPTIONS.includes(value)
    ? value
    : DEFAULT_CHAT_WORKING_MODE;
}
