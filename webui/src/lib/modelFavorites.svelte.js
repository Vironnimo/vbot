// The user's favorite Models, shared by every Model picker of this window.
//
// A favorite names a Model id, so all Connection rows of a Model share it. The
// store runs on what its page configures: the app uses the RPC API, an Extension
// page its page client; an unconfigured page has no favorites and no stars.
//
import { t } from './i18n.js';

// `stars` follow every change at once. `listed` decides which Models a picker
// puts first and keeps visible: it waits while a picker is open, so a click on a
// star never moves the rows under the pointer.

let transport = $state.raw(null);
let stars = $state.raw([]);
let listed = $state.raw([]);
let orderHolds = 0;
let readRevision = 0;
let savesRunning = 0;

function settle() {
  if (orderHolds === 0) listed = stars;
}

function apply(models) {
  stars = Array.isArray(models) ? models : [];
  settle();
}

// `load()` answers `{models}`, `save(model, favorite)` the same; `onError(message)`
// tells the user that a change failed. `null` clears the store.
export function configureModelFavorites(next) {
  transport = next;
  readRevision += 1;
  apply([]);
}

export async function loadModelFavorites() {
  if (!transport) return;
  const revision = ++readRevision;
  try {
    const result = await transport.load();
    if (revision === readRevision && savesRunning === 0) apply(result?.models);
  } catch {
    // An older server has no favorites; a failed read keeps the last answer.
  }
}

async function toggle(model) {
  const favorite = !stars.includes(model);
  stars = favorite
    ? [...stars, model]
    : stars.filter((entry) => entry !== model);
  settle();
  readRevision += 1;
  savesRunning += 1;
  try {
    const result = await transport.save(model, favorite);
    savesRunning -= 1;
    if (savesRunning === 0) apply(result?.models);
  } catch {
    savesRunning -= 1;
    transport.onError?.(t('dropdown.favoriteFailed'));
    await loadModelFavorites();
  }
}

// Option fields of a Model. `active` draws the star, `listed` is what the
// order and the suitability filter go by.
export function modelFavoriteFields(modelId) {
  if (!transport || typeof modelId !== 'string' || !modelId) return {};
  return {
    favorite: {
      active: stars.includes(modelId),
      listed: listed.includes(modelId),
      toggle: () => toggle(modelId),
    },
  };
}

export function favoritesFirst(options) {
  return [
    ...options.filter((option) => option.favorite?.listed),
    ...options.filter((option) => !option.favorite?.listed),
  ];
}

// While a picker shows its rows; the returned function ends the hold.
export function holdModelFavoriteOrder() {
  orderHolds += 1;
  let held = true;
  return () => {
    if (!held) return;
    held = false;
    orderHolds -= 1;
    settle();
  };
}
