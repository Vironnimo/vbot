<script>
  import { t } from '$lib/i18n.js';
  import {
    formatServerHost,
    getDataDirectoryValue,
  } from '$lib/settingsView.js';

  let { settings = null } = $props();

  let serverHostValue = $derived(
    formatServerHost(settings?.general?.server, t),
  );
  let dataDirectoryValue = $derived(getDataDirectoryValue(settings, t));
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.serverHost', 'Server host')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.serverHostDescription',
        'Address and port the vBot server listens on.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--input">
    <div class="s-value-box">{serverHostValue}</div>
  </div>
</div>
<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.dataDirectory', 'Data directory')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.dataDirectoryDescription',
        'Root path for agents, sessions, and workspace files.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--input">
    <div class="s-value-box">{dataDirectoryValue}</div>
  </div>
</div>
