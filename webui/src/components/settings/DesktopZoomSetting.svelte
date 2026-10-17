<script>
  // The Desktop window zoom, a row of the Appearance section in the Desktop
  // app. It belongs to this Desktop, not to the server, and applies at once
  // instead of autosaving; the app-level owner (app/desktopZoom.svelte.js)
  // keeps it in step with the zoom shortcuts.
  import Dropdown from '../Dropdown.svelte';
  import { t } from '$lib/i18n.js';

  let { desktopZoom } = $props();

  let zoom = $derived(desktopZoom.zoom);
  let options = $derived(
    (zoom?.levels ?? []).map((percent) => ({
      value: percent,
      label:
        percent === zoom.default
          ? t('settings.appearance.zoom.defaultLevel', { percent })
          : t('settings.appearance.zoom.level', { percent }),
    })),
  );
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.appearance.zoom.label')}
    </div>
    <div class="s-row-desc">
      {t('settings.appearance.zoom.description')}
    </div>
  </div>
  <div class="s-row-control s-row-control--appearance">
    <Dropdown
      id="settings-appearance-zoom"
      value={zoom?.percent ?? null}
      {options}
      disabled={!zoom}
      ariaLabel={t('settings.appearance.zoom.label')}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={(percent) => desktopZoom.set(percent)}
    />
  </div>
</div>
