/**
 * True while an input method editor (IME) owns the keystroke, including the
 * Enter that confirms a candidate. Chromium and Firefox mark such keydowns with
 * `isComposing`; Safari ends the composition first and reports the confirming
 * keydown only through the legacy keyCode 229. Text controls must leave these
 * keys to the IME instead of treating them as submit/cancel shortcuts.
 */
export function isImeComposing(event) {
  return Boolean(event?.isComposing) || event?.keyCode === 229;
}
