import { mount } from 'svelte';
import '../../../../webui/src/styles/app.css';
import SwarmPage from './SwarmPage.svelte';

mount(SwarmPage, { target: document.getElementById('app') });
