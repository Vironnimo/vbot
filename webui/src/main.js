import { mount } from 'svelte';
import App from './App.svelte';
import AccessGate from './components/access/AccessGate.svelte';

// The access gate mounts the app once the server lets this page in.
mount(AccessGate, { target: document.getElementById('app'), props: { App } });
