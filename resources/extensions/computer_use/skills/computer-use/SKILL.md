---
name: computer-use
description: "Operate desktop apps on the vBot server's Windows desktop with computer, computer_batch and computer_apps: find apps, look, click, type and verify."
---

# Computer Use

computer moves the user's real mouse and keyboard on the desktop of the computer the vBot server runs on. It is slow and visible to the user, so use it only when nothing better fits:

- Prefer a shell, files, an app's command line or an API when they can do the job.
- For web pages, prefer the playwright-cli Skill if you have it; it is faster and more reliable than clicking through a browser.

## Workflow

1. computer_apps `{"action":"list"}` shows the displays, the running apps and whether the user requires approval per app; add `"query"` to search installed apps.
2. Only if list says the user approves each app: computer_apps `{"action":"request","apps":["Notepad"],"reason":"..."}` asks the user and waits for the answer. Name every app the task needs in one request. If the user declines, do not ask again unless they tell you to.
3. computer_apps `{"action":"open","app":"Notepad"}` brings the app to the front, starting it if needed, and returns a screenshot.
4. Act with computer, one action per call, or computer_batch for steps you can predict, such as click a field, type, press enter.
5. Check the screenshot each result returns before the next step.

## Screenshots and coordinates

- Coordinates are [x, y] in the latest screenshot, measured from its top-left corner. Take a new screenshot when the screen may have changed.
- Gray boxes are apps the user has not approved, when the user requires approval per app. Request the app if you need it; input into a gray area is refused.
- Use zoom with a region to read small text or check a small control before clicking it.
- Set action_summary on input actions; the user sees it.

## Keys and text

- type enters text into the focused field; click the field first.
- key presses keys or chords: `enter`, `escape`, `tab`, `ctrl+s`, `ctrl+shift+t`, `alt+f4`. Separate several chords with spaces to press them in order.
- On clicks, scrolls and drags, text names modifier keys to hold, such as `shift` or `ctrl`.

## Safety

- Never type passwords, keys or other secrets. Ask the user to enter them.
- Ask the user before actions that are hard to undo: deleting, sending, buying, publishing, closing without saving, or changing system settings.
- The user can stop you with the Stop button or by pressing Esc twice. After a stop, do not continue unless the user asks you to.
- If something unexpected appears, such as a dialog, a login prompt or a warning, take a screenshot and decide from what you see; ask the user when unsure.

## Finish

Save through the app and check the result: the dialog closed, the title shows no unsaved mark, or the file exists. Report what you did and what you verified.
