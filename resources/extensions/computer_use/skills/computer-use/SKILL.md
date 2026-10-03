---
name: computer-use
description: "Operate desktop apps on the vBot server's Windows desktop with computer, computer_batch and computer_apps: find apps, look, click, type and verify."
---

# Computer Use

computer moves the user's real mouse and keyboard on the desktop of the computer the vBot server runs on. It is slow and visible to the user, so use it only when nothing better fits:

- Prefer a shell, files, an app's command line or an API when they can do the job.
- For web pages, prefer the playwright-cli Skill if you have it; it is faster and more reliable than clicking through a browser.

## Workflow

1. If computer_apps is available, `{"action":"list"}` shows displays, running apps and whether the user requires approval per app; add `"query"` to search installed apps. Otherwise start with computer `{"action":"screenshot"}` and use the visible desktop.
2. Only when approval per app is required: computer_apps `{"action":"request","apps":["Notepad"],"reason":"..."}` asks the user and waits for the answer. Name every app the task needs in one request. If computer_apps is unavailable, ask the user to enable it before operating an unapproved app. If the user declines, do not ask again unless they tell you to.
3. If computer_apps is available, `{"action":"open","app":"Notepad"}` brings the app to the front, starting it if needed, and returns a screenshot. Otherwise use the visible taskbar or Start menu, then take a screenshot.
4. Act with computer, one action per call. If computer_batch is available, use it for predictable steps, such as click a field, type, press enter. Each batch uses images returned before that call; images produced inside it are for inspection afterwards.
5. Check the screenshot each result returns before the next step. A successful input call means the input was sent; verify that the intended control, color, text or drawing actually changed before building on it.
6. When done, use computer_apps `{"action":"release"}` if available. Otherwise finish your reply, which ends control.

## Screenshots and coordinates

- Measure coordinates in the image identified by screenshot_id, from its top-left corner. This applies to both screenshots and zoom images. Do not convert coordinates to desktop pixels; the tool performs that conversion. Omit screenshot_id to use the latest returned image.
- A screenshot normally shows the foreground window. To see the whole desktop, use `{"action":"screenshot","view":"display"}`; add display to select a monitor. Use `{"action":"screenshot","view":"window"}` to return to window capture. This choice applies to later input screenshots too.
- Take a new screenshot when the screen may have changed. Image references belong to this Session; if a reference is unavailable or its window or display geometry changed, take a new screenshot and use its reference.
- Gray boxes are apps the user has not approved, when the user requires approval per app. Request the app if you need it; input into a gray area is refused.
- Use zoom with `region: [x0, y0, x1, y1]` in the selected image to inspect a small control; the bottom-right corner is excluded. The result has a new screenshot_id. Click using coordinates measured directly in that zoom image, without adding the crop origin or undoing its scale. If several zoom images were returned, name the one you measured with screenshot_id.
- Set action_summary on input actions; the user sees it.

## Keys and text

- type enters text into the focused field; click the field first.
- key presses keys or chords: `enter`, `escape`, `tab`, `ctrl+s`, `ctrl+shift+t`, `alt+f4`. Separate several chords with spaces to press them in order.
- On clicks, scrolls and drags, text names modifier keys to hold, such as `shift` or `ctrl`.

## Safety

- Never type passwords, keys or other secrets. Ask the user to enter them.
- Ask the user before actions that are hard to undo: deleting, sending, buying, publishing, closing without saving, or changing system settings.
- While you control the computer, a frame around the screens shows it, and the user can stop you at any time with the Stop button or by pressing Esc twice. After a stop, the computer stays off until the user's next message: tell the user what you did and what is left.
- If something unexpected appears, such as a dialog, a login prompt or a warning, take a screenshot and decide from what you see; ask the user when unsure.

## Finish

Save through the app and check the result: the dialog closed, the title shows no unsaved mark, or the file exists. Release the computer if computer_apps is available, then report what you did and what you verified.
