// The page bridge uses semantic color names, independent of the app's CSS.
export const EXTENSION_THEME_TOKENS = Object.freeze({
  background: '--bg',
  surface: '--surface',
  elevatedSurface: '--surface-2',
  border: '--border',
  text: '--text-hi',
  mutedText: '--text-med',
  accent: '--accent',
});

export function applyExtensionTheme(
  theme = {},
  element = document.documentElement,
) {
  const values = theme ?? {};
  for (const [name, token] of Object.entries(EXTENSION_THEME_TOKENS)) {
    if (typeof values[name] === 'string')
      element.style.setProperty(token, values[name]);
    else element.style.removeProperty(token);
  }
  if (values.mode === 'light' || values.mode === 'dark')
    element.style.colorScheme = values.mode;
  else element.style.removeProperty('color-scheme');
}
