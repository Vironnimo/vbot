// Small app-wide reactive store for pure display preferences from the
// appearance settings section. Unlike recall/skills, chat width has no runtime
// reload hook — it only drives a CSS attribute, so a tiny reactive singleton is
// enough: App seeds it from `settings.get` on bootstrap and passes the value
// down to ChatView; the Appearance panel updates it on save so the open chat
// reflows live without a reload.

import { CHAT_WIDTH_OPTIONS, DEFAULT_CHAT_WIDTH } from './settingsView.js';

export const appearancePrefs = $state({ chatWidth: DEFAULT_CHAT_WIDTH });

export function setChatWidth(value) {
  appearancePrefs.chatWidth = CHAT_WIDTH_OPTIONS.includes(value)
    ? value
    : DEFAULT_CHAT_WIDTH;
}
