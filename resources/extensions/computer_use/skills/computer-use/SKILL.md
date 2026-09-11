---
name: computer-use
description: "Operate desktop applications with the computer Tool, including visual editing, forms, dialogs, and verified saving."
---

# Computer Use

Use the computer Tool to complete the user's task in desktop applications. Actions execute by default; apply=false is a preview and sends no input. Screenshots arrive directly in the result.

## Choose the target and input

Start with windows and capture the chosen pid and window_id. Use apps and launch if the application is not open, then select its window. Window control initially uses background input so the user's pointer and active application can remain independent. A target keeps its chosen foreground setting for this Run; an explicit foreground value changes it. Background support depends on the application; test one small action and inspect its effect before doing more.

Desktop input uses the shared mouse and keyboard. foreground=true also uses foreground control. If the user wants to keep using their mouse, stay with background window control and report when the application requires foreground input. A screenshot does not activate a window. The user may be clicking or typing in another window while you work. This is normal concurrent activity, not a request to cancel the Run or stop the Tool. Do not routinely ask the user to stop using the computer. Unexpected focus, selection, or layout changes can come from the user or the application; do not assume a Tool defect. Inspect the returned outcome and current target state, then continue the task from that state. Do not undo unexplained changes that may be the user's work.

To switch to foreground control, capture the window with foreground=true before coordinate input. If it is not foreground, capture the desktop with {"action":"capture"}, use that image to select the intended window or its taskbar entry, then capture the window with its pid, window_id and foreground=true. A background title-bar click is not a reliable activation method. Keep a working delivery setting until observed behavior requires a change; a new Run needs a fresh capture.

## Observe and act

The default observation is a screenshot. Use mode=som when you need named window elements, or mode=ax when pixels are unnecessary. query is one case-insensitive literal substring, not a regular expression or an OR expression. Omit it for an overview; limit bounds the element list. These options also work on input actions, letting you request the elements needed for the next step without another capture.

A filtered or incomplete element list does not establish that a control is unavailable. Try an unfiltered overview or a query using its visible label, then use pixels if needed.

Use the returned view_id with coordinates measured in the displayed image. It identifies the target and foreground setting, so pid and window_id may be omitted for input, capture, wait, verify, and zoom. For example, use capture with the current view_id and mode=som to add named controls without changing the target or foreground setting. Zoom enlarges a rectangle from captured pixels; it does not change the application's zoom. Use the new image's view_id and coordinates when acting from a crop.

Use complete element refs exactly as returned for named controls; they identify their window and foreground setting. Never construct a ref from an earlier index or a similar token. Prefer coordinates for canvases and when element actions have no visible effect. After input or a new capture, continue from the returned observation; earlier views and element refs are invalid. Read-only zooms preserve the parent view and its element refs. Multiple zooms of one current view may be requested together; a capture followed by a zoom of the old view, or two separate inputs using the same refs, cannot. Wait for each new observation before using its refs, or use sequence for known coordinate steps.

## Work in short sequences

Use sequence for up to eight known steps on one target. Put view_id on the sequence to share it across coordinate steps. Only the first step may use an element ref. End the sequence when a menu, dialog, layout change, or uncertain application behavior requires a new observation.

Before repeating an operation, verify one instance and the application's current mode, selection, and active control. A completed gesture can leave an edit active; a confirmation key can leave focus in the same field. Resolve that state before the next operation.

Example: with a text field observed at [120,80], replace VIEW with the current view_id and use {"action":"sequence","view_id":"VIEW","steps":[{"action":"click","coordinate":[120,80]},{"action":"type","text":"Example"}]}. Coordinates are illustrative; measure them in your image. The result supplies the view for the next call.

## Recover and finish

Check applied, completed_steps, partial, and any error before continuing. When ok=false, recovery details are in artifacts. A recovery object contains arguments for a read-only computer call: pass that object directly to the Tool to inspect the next state. target and foreground identify the requested target and delivery setting; they do not prove the window still exists or is active. Recovery may start with a desktop capture or windows discovery. After selecting a window on the desktop, capture that window with foreground=true before window-coordinate input.

After input or a failed capture, a missing new observation means the previous view_id is no longer usable. Validation errors before either operation preserve the current observation. This also applies after capture_after=false. Use recovery or capture with explicit target fields. A retained observation is labeled as such and is not a fresh screenshot. Dispatched input does not prove the application changed; inspect before continuing with the remaining work and never replay input merely because its observation failed. With a current view, use wait for a later image or verify for window and element conditions.

If an action has no visible effect, check the returned foreground setting, selected control, and target position before changing coordinates or switching input methods. Treat an unverified explanation as a hypothesis; a failed call or incomplete observation does not establish an application limitation.

When a dialog opens, use its returned target or find it through windows and capture it before entering text. Save through the application and verify that the dialog closed and the expected file or saved state exists before reporting success. An interrupted action may have had partial effects. Follow the user's latest instructions and capture again before further input. The computer Tool remains available. Use close when finished.

## Precision and keyboard-only applications

Windows display scaling is handled by the Tool. Measure coordinates in the returned image; do not multiply them by a monitor DPI scale. For a small menu row or control, zoom first and click using the crop's local coordinates and its view_id. The full-window coordinates belong to the parent view_id. A successful input returns a new full-target observation, so use that image's dimensions and reference next. After a window resize, application zoom, or change between foreground and background capture, remeasure small controls in the new image instead of reusing a coordinate map. An explicit resolution choice is retained for subsequent observations of that target.

Example: zoom with {"action":"zoom","view_id":"VIEW","coordinate":[100,40],"to_coordinate":[300,120]}. Replace VIEW with the current reference and measure the rectangle in that image. If the desired control appears at [30,20] in the returned crop, click [30,20] using the crop's view_id, not the parent's coordinates.

Use type for focused text fields and key for individual shortcuts. If a Windows text field ignores Unicode input, confirm its focus and use type with foreground=true and text_mode=keyboard. This sends characters as physical key events on the active keyboard layout. Outside a text field, those keys may invoke commands. Inspect the result before repeating input.
