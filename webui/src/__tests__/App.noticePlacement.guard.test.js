import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

// User requirement (2026-11-04): nothing may appear above the active view.
// Every new app-wide message used to land there because it was the only open
// place in the layout. App-wide states belong in the toast stack
// (`app/notices.svelte.js`); what must be confirmed is a Dialog.
const appSource = readFileSync(
  fileURLToPath(new URL('../App.svelte', import.meta.url)),
  'utf8',
);

describe('App notice placement', () => {
  it('renders the active view first inside the AppShell', () => {
    const shellContent = appSource
      .slice(appSource.indexOf('<AppShell'))
      .replace(/^<AppShell[\s\S]*?\n>\n/, '')
      .replace(/^\s*\{#snippet sidebarFooter\(\)\}[\s\S]*?\{\/snippet\}\n/, '');

    expect(shellContent.trimStart()).toMatch(
      /^\{#key serverRecoveryGeneration\}/,
    );
  });

  it('does not use Banners at app level', () => {
    expect(appSource).not.toMatch(/<Banner\b/);
  });
});
