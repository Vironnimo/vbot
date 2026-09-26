---
name: computer-use
description: "Operate desktop applications with the computer Tool, including visual editing, forms, dialogs, and verified saving."
---

# Computer Use

Use the computer Tool to complete the user's task in desktop applications. Input executes immediately, and screenshots arrive directly in the result.

## Choose the target and delivery

Start with windows (narrow it with app or pid) and capture the chosen window with its pid and window_id. If the application is not open, use apps and launch, then select its window from windows. Window input starts as background input, so the user's pointer and active application stay independent. foreground=true uses the real mouse and keyboard on the active window; the desktop always uses them. A target keeps its delivery setting for this Run until an explicit foreground value changes it. Background support depends on the application: test one small action and inspect its effect before doing more.

If the user wants to keep using their mouse, stay with background input and report when the application requires foreground input. The user may be clicking or typing in another window while you work. This is normal concurrent activity, not a request to cancel the Run or stop the Tool, so do not routinely ask the user to stop. Unexpected focus, selection, or layout changes can come from the user or the application; do not assume a Tool defect. Continue from the current state, and do not undo unexplained changes that may be the user's work.

Coordinates for foreground input need a screenshot captured with foreground=true. A screenshot never activates a window: if the window is not active, capture the desktop with {"action":"capture"}, click the window or its taskbar entry there, then capture the window with foreground=true. A background title-bar click is not a reliable way to activate a window.

## Screenshots and coordinates

Measure coordinates in the returned image, from its top-left; display scaling is handled, so do not multiply by a monitor scale. A call without pid, window_id or view_id continues with the latest screenshot and its target, and coordinates without view_id use their target's current screenshot. Every input replaces all earlier screenshots and element refs with the new screenshot it returns. If you captured several targets since the last input, input must say which one with view_id, or pid and window_id; a failure lists the current views.

The default screenshot is an image (mode vision). mode=som adds refs for named window elements; mode=ax returns only the element refs. query filters elements by one case-insensitive literal substring (not a regular expression) and implies som; limit bounds the list (default 200). These options also work on input actions, so the returned screenshot can include the elements for the next step. A filtered or incomplete element list does not prove a control is missing; try an overview or the visible label, then use pixels. resolution=original returns full-resolution screenshots for that target until changed back to auto.

Use complete element refs exactly as returned; never construct one from an earlier index. Prefer coordinates for canvases and when element actions have no visible effect.

For a small control, zoom first: {"action":"zoom","coordinate":[100,40],"to_coordinate":[300,120]} enlarges that rectangle of the current screenshot without changing the application. The crop has its own view_id and coordinates starting at [0,0]; act with the crop's view_id and coordinates measured in the crop, or with the parent view_id and parent coordinates. After a zoom, coordinates need an explicit view_id. After a window resize, application zoom, or delivery change, remeasure small controls in the new image.

## Keys and text

type enters text into the focused field; set_value replaces an element's text. key presses the key or combination in text: enter, escape, tab, backspace, delete, home, end, pageup, pagedown, up, down, left, right, space, f1 to f24, or combinations such as ctrl+s, alt+f4, ctrl+shift+t and ctrl+plus. With foreground=true, key with duration_ms (up to 2000) holds the key, and click, scroll or drag with modifiers such as ["ctrl"] or ["shift","alt"] holds those keys during the gesture.

If a Windows text field ignores typed Unicode, confirm its focus and use type with foreground=true and text_mode=keyboard. This sends physical key events on the active keyboard layout; outside a text field, those keys may invoke commands.

## Work in short sequences

Use sequence for up to eight known steps on one target, for example {"action":"sequence","steps":[{"action":"click","coordinate":[120,80]},{"action":"type","text":"Example"}]} after measuring the field in your screenshot. Steps share the sequence's screenshot; only the first step may use an element ref. The sequence stops at the first failure and captures once at the end. End it when a menu, dialog, layout change, or uncertain application behavior requires a new look.

Before repeating an operation, verify one instance and the application's current mode, selection, and active control. A completed gesture can leave an edit active, and a confirmation key can leave focus in the same field.

## Window operations and checks

- menu opens a menu path of a window: {"action":"menu","menu_path":["File","Save As..."]}.
- resize moves a window to screen position coordinate with size [width,height].
- verify waits up to timeout_ms (0 to 10000, default 5000) for conditions, then captures: {"action":"verify","expect":[{"element":{"selector":{"role":"Button","label_contains":"Save"},"exists":true}}]}. A condition is {"window":{"exists":false}} or an element selector with exists, enabled, selected or value_equals.
- wait pauses duration_ms (default 1000, up to 10000) and captures again, for an application that is still reacting.
- monitors lists displays; capture with monitor selects one display of the desktop.
- capture_after=false skips the screenshot after input. apply=false previews an input action without sending it.

## Recover and finish

Check applied, completed_steps, partial, and any error before continuing. A failure says whether input was sent and names the exact next call; send it as given. When input was sent but its new screenshot failed or was skipped, the result contains recovery: call computer with that object as the arguments to see the current state. Input that was sent does not prove the application changed, and a failed screenshot does not mean the input failed: inspect before continuing, and never repeat input only because its screenshot is missing. After a stopped sequence, do not repeat the completed steps.

If an action has no visible effect, check the delivery setting, the selected control, and the target position before changing coordinates or input methods. Treat an unverified explanation as a hypothesis; a failed call does not establish an application limitation.

When a dialog blocks a window, capturing that window shows the dialog and returns its window_id. Save through the application and verify that the dialog closed and the expected file or saved state exists before reporting success. An interrupted action may have had partial effects: follow the user's latest instructions and capture again before further input. Use close when finished.
