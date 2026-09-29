// The WebUI build identity. Every production build carries one id, derived
// from its hashed output files: `index.html` names it in a meta tag (the
// build a loaded page runs) and `build.json` beside it (the build the server
// serves now). A page whose id differs from the served one runs outdated code,
// typically after an update restarted the server under an open page.
//
// `scripts/webui-build-identity.mjs` writes both at build time, so this module
// stays free of imports and runs under Node as well.

export const WEBUI_BUILD_META_NAME = 'vbot-webui-build';

export const WEBUI_BUILD_FILE = 'build.json';

// The id of the build this page runs, or null without one (the Vite dev
// server and tests have no build).
export function loadedWebuiBuild(doc = globalThis.document) {
  const content = doc
    ?.querySelector?.(`meta[name="${WEBUI_BUILD_META_NAME}"]`)
    ?.getAttribute('content');
  return typeof content === 'string' && content.length > 0 ? content : null;
}

// The build id a `build.json` payload names, or null for anything else.
export function servedWebuiBuild(payload) {
  const id = payload?.build_id;
  return typeof id === 'string' && id.length > 0 ? id : null;
}

// Whether the server now serves another build than the one this page runs.
// Without both ids nothing is known, so the page counts as current.
export async function isWebuiOutdated(
  getServedBuild,
  loaded = loadedWebuiBuild(),
) {
  if (loaded === null) return false;
  const served = await getServedBuild();
  return served !== null && served !== loaded;
}
