---
name: computer-use
description: "Operate desktop applications with the computer Tool, including visual editing, forms, dialogs, and verified saving."
---

# Computer Use

Use the computer Tool to complete the user's task in desktop applications. Actions execute by default; apply=false is a preview and sends no input. Screenshots arrive directly in the result.

## Choose the target and input

Start with windows and capture the chosen pid and window_id. Use apps and launch if the application is not open, then select its window. Window control uses background input by default so the user's pointer and active application can remain independent. Background support depends on the application; inspect the result after the first action.

Desktop input uses the shared mouse and keyboard. foreground=true also uses foreground control. If the user wants to keep using their mouse, stay with background window control and report when the application requires foreground input. A screenshot does not activate a window.

## Observe and act

The default observation is a screenshot. Use mode=som when you need named window elements, or mode=ax when pixels are unnecessary. query and limit focus the element list. These options also work on input actions, letting you request the elements needed for the next step without another capture.

Use the returned view_id with coordinates measured in the displayed image. It identifies the target, so pid and window_id may be omitted for coordinate input and zoom. Zoom enlarges a rectangle from captured pixels; it does not change the application's zoom. Use the new image's view_id and coordinates when acting from a crop.

Use element refs from the latest element observation for named controls. Prefer coordinates for canvases and when element actions have no visible effect. After input, continue from the returned observation; earlier views and element refs are invalid.

## Work in short sequences

Use sequence for up to eight known steps on one target. Put view_id on the sequence to share it across coordinate steps. Only the first step may use an element ref. End the sequence when a menu, dialog, layout change, or uncertain application behavior requires a new observation.

For drawing, try one stroke or shape and inspect it before repeating. Check which tool and color are selected and whether a new shape is still being edited. Do not assume Escape commits a shape or that another drag starts a new one.

## Recover and finish

Check applied, completed_steps, partial, and any error before continuing. When ok=false, any recovery observation and step counts are in artifacts. Dispatched input does not prove the application changed. On partial or uncertain results, inspect the fresh observation and continue only with the remaining work. Use wait for a later image or verify for window and element conditions; do not repeat input merely because observation failed.

When a dialog opens, use its returned target or find it through windows and capture it before entering text. Save through the application and verify that the dialog closed and the expected file or saved state exists before reporting success. An interrupted action may have had partial effects. Follow the user's latest instructions and capture again before further input. The computer Tool remains available. Use close when finished.
