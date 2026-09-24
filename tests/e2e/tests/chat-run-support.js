import { expect } from "@playwright/test";

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A Project Team member's tab; its accessible name starts with the Agent name.
export function getAgentTab(container, agentName) {
  return container.getByRole("button", {
    name: new RegExp(`^${escapeRegExp(agentName)}:`),
  });
}

// The Chat header's personal Agent picker; its trigger shows the selected
// Agent's name.
export function getAgentPicker(chat) {
  return chat.getByRole("button", { name: /^Select agent/ });
}

export async function selectPersonalAgent(page, chat, agentName) {
  await getAgentPicker(chat).click();
  await page
    .getByRole("option", { name: new RegExp(`^${escapeRegExp(agentName)}:`) })
    .click();
  await expect(getAgentPicker(chat)).toContainText(agentName);
}

export async function ensureEmptyChat(chat) {
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  if (!(await sessionDrawer.isVisible())) {
    await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
  }
  await expect(sessionDrawer).toBeVisible();
  const emptySessions = sessionDrawer.getByText("No sessions yet", {
    exact: true,
  });
  const selectedSession = sessionDrawer.locator(
    "button.session-row__select--active",
  );
  await expect(async () => {
    expect(
      (await emptySessions.isVisible()) || (await selectedSession.isVisible()),
    ).toBe(true);
  }).toPass();
  const newSessionButton = chat.getByRole("button", {
    exact: true,
    name: "New session",
  });
  await expect(newSessionButton).toBeEnabled();

  // New session reuses a displayed empty Session and otherwise creates and
  // opens a fresh one; either way the button stays busy until that Session is
  // displayed and ready for input.
  await newSessionButton.click();
  await expect(newSessionButton).toBeEnabled();
  await expect(selectedSession).toHaveCount(1);
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
    await selectPersonalAgent(page, chat, agentName);
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

export async function sendChatMessage(chat, content) {
  await chat.getByRole("textbox", { name: "Message" }).fill(content);
  await chat.getByRole("button", { name: "Send message" }).click();
}
