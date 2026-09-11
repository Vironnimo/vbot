---
name: computer-use
description: "Operate desktop applications with the computer Tool, including visual editing, forms, dialogs, and verified saving."
---

# Computer Use

Use the computer Tool to complete the user's task in desktop applications. Actions execute by default; apply=false is a preview and sends no input. Screenshots arrive directly in the result.

## Choose the target and input

Start with windows and capture the chosen pid and window_id. Use apps and launch if the application is not open, then select its window. Window control initially uses background input so the user's pointer and active application can remain independent. A target keeps its chosen foreground setting for this Run; an explicit foreground value changes it. Background support depends on the application; test one small action and inspect its effect before doing more.

Desktop input uses the shared mouse and keyboard. foreground=true also uses foreground control. If the user wants to keep using their mouse, stay with background window control and report when the application requires foreground input. A screenshot does not activate a window. The user may be clicking or typing in another window while you work. This is normal concurrent activity, not a request to cancel the Run or stop the Tool. Do not routinely ask the user to stop using the computer. Unexpected focus, selection, or layout changes can come from the user or the application; do not assume a Tool defect. Inspect the returned outcome and current target state, then continue the task from that state. Do not undo unexplained changes that may be the user's work.

## Observe and act

The default observation is a screenshot. Use mode=som when you need named window elements, or mode=ax when pixels are unnecessary. query is one case-insensitive literal substring, not a regular expression or an OR expression. Omit it for an overview; limit bounds the element list. These options also work on input actions, letting you request the elements needed for the next step without another capture.

Use the returned view_id with coordinates measured in the displayed image. It identifies the target and foreground setting, so pid and window_id may be omitted for input, capture, wait, verify, and zoom. For example, use capture with the current view_id and mode=som to add named controls without changing the target or foreground setting. Zoom enlarges a rectangle from captured pixels; it does not change the application's zoom. Use the new image's view_id and coordinates when acting from a crop.

Use complete element refs exactly as returned for named controls; they identify their window and foreground setting. Never construct a ref from an earlier index or a similar token. Prefer coordinates for canvases and when element actions have no visible effect. After input or a new capture, continue from the returned observation; earlier views and element refs are invalid. Read-only zooms preserve the parent view and its element refs. Multiple zooms of one current view may be requested together; a capture followed by a zoom of the old view, or two separate inputs using the same refs, cannot. Wait for each new observation before using its refs, or use sequence for known coordinate steps.

## Work in short sequences

Use sequence for up to eight known steps on one target. Put view_id on the sequence to share it across coordinate steps. Only the first step may use an element ref. End the sequence when a menu, dialog, layout change, or uncertain application behavior requires a new observation.

For drawing, establish one reliable stroke or shape before planning detailed coordinates for the whole picture. Before each new kind of shape, confirm the selected drawing tool, color, fill, and whether the previous shape is still being edited. Test one shape before repeating it. Do not assume Escape or a click inside a shape commits it. Canvas edges may contain resize handles: use an application fill command for a full background, or start safely inside the canvas and inspect before repairing a thin border. Save a verified intermediate result before delicate adjustments.

## Recover and finish

Check applied, completed_steps, partial, and any error before continuing. When ok=false, any recovery observation and step counts are in artifacts. Dispatched input does not prove the application changed. On partial or uncertain results, inspect the fresh observation and continue only with the remaining work. Use wait with the current view_id for a later image, or verify for window and element conditions; do not repeat input merely because observation failed. If an action has no visible effect, check the returned foreground setting, selected control, and target position before changing coordinates or switching input methods. Treat an unverified explanation as a hypothesis, including in summaries; record the observed result separately.

When a dialog opens, use its returned target or find it through windows and capture it before entering text. Save through the application and verify that the dialog closed and the expected file or saved state exists before reporting success. An interrupted action may have had partial effects. Follow the user's latest instructions and capture again before further input. The computer Tool remains available. Use close when finished.

## Precision and keyboard-only applications

Windows display scaling is handled by the Tool. Measure coordinates in the returned image; do not multiply them by a monitor DPI scale. For a small menu row or control, zoom first and click using the crop's local coordinates and its view_id. The full-window coordinates belong to the parent view_id. A successful input returns a new full-target observation, so use that image's dimensions and reference next. After a window resize, application zoom, or change between foreground and background capture, remeasure small controls in the new image instead of reusing a coordinate map. An explicit resolution choice is retained for subsequent observations of that target.

Use type for focused text fields and key for individual shortcuts. In Windows applications such as Blender that ignore Unicode text input, focus the text field and use type with foreground=true and text_mode=keyboard. This mode sends real key events for characters on the active keyboard layout. In a viewport those characters can invoke commands, so do not treat a command string as harmless field text. For Blender numeric transforms, send the operator and axis with key, enter digits with keyboard mode, and confirm with key. Inspect the result before repeating input.

For click followed by type or key, put the view_id once on sequence. Stop and inspect when a menu or dialog changes the layout. A failed call is not evidence that the application lacks a capability: check the error and completed_steps before drawing conclusions or saving a workaround.
