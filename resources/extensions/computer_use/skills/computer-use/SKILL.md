---
name: computer-use
description: "Operate desktop apps on the vBot server's Windows desktop with computer, computer_batch and computer_apps: find apps, look, click, type and verify."
---

# Computer Use

computer moves the user's real mouse and keyboard on the desktop of the computer the vBot server runs on. It is slow and visible to the user, so use it only when nothing better fits:

- Prefer a shell, files, an app's command line or an API when they can do the job.
- For web pages, prefer the playwright-cli Skill if you have it; it is faster and more reliable than clicking through a browser.

## Workflow

1. If computer_apps is available, `{"action":"list"}` shows displays, running apps and whether the user requires approval per app; add `"query"` to search installed apps. Otherwise take a screenshot of the whole monitor with computer `{"action":"screenshot","view":"display"}`.
2. Only when approval per app is required: computer_apps `{"action":"request","apps":["Notepad"],"reason":"..."}` asks the user and waits for the answer. Name every app the task needs in one request. If computer_apps is unavailable, ask the user to enable it before operating an unapproved app. If the user declines, do not ask again unless they tell you to.
3. If computer_apps is available, `{"action":"open","app":"Notepad"}` brings the app to the front, starting it if needed, and returns a screenshot. Otherwise click the app in the taskbar or Start menu of a display screenshot.
4. Act with computer, one action per call. If computer_batch is available, use it for predictable steps, such as click a field, type, press enter. The steps of a batch return no screenshots in between, so batch only steps that do not depend on what an earlier step shows. A batch that sent input ends with one screenshot; to check fine details, end the batch with a zoom of the area you worked on.
5. Check the screenshot each result returns before the next step. A successful input call means the input was sent; verify that the intended control, color, text or drawing actually changed before building on it. When a call fails after it sent input, its result shows the screen as the input left it.
6. When done, use computer_apps `{"action":"release"}` if available. Otherwise finish your reply, which ends control.

## Screenshots and coordinates

- A coordinate is the pixel where you see the target in the latest screenshot, counted from its top-left corner. Never convert it to screen pixels, add a crop origin or calculate with image sizes: the Tool does that.
- Pass screenshot_id to click in an earlier screenshot or in a zoom image, as long as its window has not moved since.
- A screenshot shows the foreground window. Use `{"action":"screenshot","view":"display"}` to see the whole monitor, including the taskbar; add display to choose a monitor. `{"action":"screenshot","view":"window"}` returns to the window. Later screenshots keep the choice.
- zoom is a magnifier: `region: [x0, y0, x1, y1]` in the selected image, with x1 and y1 excluded, returns a fresh, enlarged image with its own screenshot_id. Use it to read small text, and to click exactly on a small target such as a canvas edge, a thin line or a handle: pass the zoom's screenshot_id with the position where you see the target in the zoom image.
- To draw, select with a lasso or follow a curve, send one left_click_drag with path: the left button stays down from the first point to the last, and the pointer moves straight from point to point, so more points make a rounder curve. duration sets how long the movement takes.
- For an exact drag whose ends do not fit into one zoom, zoom around each end, then send left_mouse_down with the first zoom's screenshot_id and left_mouse_up with the second's, in one computer_batch if you have it.
- Read positions and colors from the images. Do not analyse screenshot files with scripts.
- Screenshots do not show the mouse pointer, but they show what it causes: hover highlights, tooltips, or an app's own brush or cursor outline. Before the screenshot that checks your work, move the pointer to an empty spot with mouse_move, for example as the last input step of a batch.
- Gray boxes are apps the user has not approved, when the user requires approval per app. Request the app if you need it; input into a gray area is refused.

## Keys and text

- type and key go to the foreground window, wherever the pointer is. Click the field first. If another app came to the front since your latest screenshot, they are refused; click into the window you want, then type.
- key presses keys or chords: `enter`, `escape`, `tab`, `ctrl+s`, `ctrl+shift+t`, `alt+f4`. Separate several chords with spaces to press them in order.
- On clicks, scrolls and drags, text names modifier keys to hold, such as `shift` or `ctrl`.

## Safety

- Never type passwords, keys or other secrets. Ask the user to enter them.
- Ask the user before actions that are hard to undo: deleting, sending, buying, publishing, closing without saving, or changing system settings.
- While you control the computer, a frame around the screens shows it, and the user can stop you at any time with the Stop button or by pressing Esc twice. After a stop, the computer stays off until the user's next message: tell the user what you did and what is left.
- If something unexpected appears, such as a dialog, a login prompt or a warning, take a screenshot and decide from what you see; ask the user when unsure.

## Finish

Save through the app and check the result: the dialog closed, the title shows no unsaved mark, or the file exists. Release the computer if computer_apps is available, then report what you did and what you verified.
