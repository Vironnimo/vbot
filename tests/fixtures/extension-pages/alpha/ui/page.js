import { createExtensionPageClient } from '$lib/extensionPageClient.js';

const bridge = createExtensionPageClient();
const root = document.documentElement;
const main = document.querySelector('main');

root.dataset.page = 'alpha';

bridge.onContext((context) => {
  root.dataset.context = 'received';
  root.dataset.locale = context.locale;
  root.dataset.timezone = context.timezone;
  root.dataset.theme = JSON.stringify(context.theme);
  main.textContent = `Alpha fixture (${context.locale}/${context.timezone})`;
  if (root.dataset.operationStarted) return;
  root.dataset.operationStarted = 'true';
  void bridge
    .operation('probe', { fixture: 'alpha' })
    .then((result) => {
      root.dataset.operation = result.fixture;
    })
    .catch((error) => {
      root.dataset.operationError = error.message;
    });
});

bridge.onInvalidation((event) => {
  root.dataset.invalidated = event.reason ?? 'revision';
});

window.addEventListener('beforeunload', () => {
  root.dataset.unloaded = 'true';
  bridge.dispose();
});
