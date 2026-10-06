import { expect } from "@playwright/test";

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// The Chat header's Agent picker includes Identity Agents and Project Teams;
// its trigger shows the selected Agent's name.
export function getAgentPicker(chat) {
  return chat.getByRole("button", { name: /^Select agent/ });
}

export async function selectChatAgent(
  page,
  chat,
  agentName,
  { projectName = "" } = {},
) {
  const picker = getAgentPicker(chat);
  await picker.click();
  const role =
    (await picker.getAttribute("aria-haspopup")) === "tree"
      ? "treeitem"
      : "option";
  if (projectName) {
    const group = page.getByRole("treeitem", {
      name: new RegExp(`^${escapeRegExp(projectName)}(?:\\s|:|$)`),
    });
    await expect(group).toBeVisible();
    if ((await group.getAttribute("aria-expanded")) === "false") {
      await group.click();
    }
  }
  const name = projectName ? `${agentName} · ${projectName}` : agentName;
  await page
    .getByRole(role, { name: new RegExp(`^${escapeRegExp(name)}:`) })
    .click();
  await expect(picker).toContainText(name);
}

export async function ensureEmptyChat(chat) {
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  if (!(await sessionDrawer.isVisible())) {
    await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
  }
  await expect(sessionDrawer).toBeVisible();
  const selectedSession = sessionDrawer.locator(
    "button.session-row__select--active",
  );
  const newSessionButton = chat.getByRole("button", {
    exact: true,
    name: "New session",
  });
  await expect(newSessionButton).toBeEnabled();

  // New session opens an unsaved draft without creating a Session; the
  // Session exists only once the first message is sent. A displayed draft is
  // reused.
  await newSessionButton.click();
  await expect(selectedSession).toHaveCount(0);
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();

  // The Sessions panel floats above the chat surface and intercepts pointer
  // events on the timeline underneath, so it never stays open beyond this
  // helper. Specs that need it open it explicitly.
  if (await sessionDrawer.isVisible()) {
    await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
    await expect(sessionDrawer).toBeHidden();
  }

  return chat;
}

export async function startIsolatedChat(page, { agentName = "" } = {}) {
  await page.goto("/#chat");

  const chat = page.getByRole("region", { name: "Chat" });
  if (agentName) {
    await selectChatAgent(page, chat, agentName);
  }
  return ensureEmptyChat(chat);
}

// A Session row under the pointer opens a hoverable title tooltip above
// itself, covering the row above. Earlier clicks leave the pointer wherever a
// menu, dialog, or header button was, so park it on the tooltip-free brand and
// wait out the close delay: a click retried against a covering tooltip scrolls
// the list, and that scroll closes a row menu the click has just opened.
export async function parkPointer(page) {
  await page.mouse.move(0, 0);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
}

// The message timeline of a Chat area. Assertions about Assistant output and
// error messages scope to it: the Chat region also holds a visually hidden
// status region that repeats each finished answer and error for screen
// readers, so an unscoped text match can resolve to both.
export function chatTimeline(chat) {
  return chat.locator(".messages");
}

export async function sendChatMessage(chat, content) {
  await chat.getByRole("textbox", { name: "Message" }).fill(content);
  await chat.getByRole("button", { name: "Send message" }).click();
}
