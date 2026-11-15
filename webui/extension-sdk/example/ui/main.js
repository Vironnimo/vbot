import '@vbot/extension-sdk/styles';
import { mount } from 'svelte';
import { registerCatalog } from '@vbot/extension-sdk/lib/i18n.js';
import Page from './Page.svelte';

registerCatalog({
  'sdkExample.title': 'Extension example',
  'sdkExample.description': 'This page uses the public vBot Extension SDK.',
  'sdkExample.notify': 'Show notification',
  'sdkExample.notification': 'The Extension is connected to vBot.',
});

mount(Page, { target: document.getElementById('app') });
