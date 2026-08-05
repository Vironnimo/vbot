import { expect } from "@playwright/test";

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function getAgentTab(container, agentName) {
  return container.getByRole("button", {
    name: new RegExp(`^${escapeRegExp(agentName)}:`),
  });
}

export async function ensureEmptyChat(chat) {
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  if (!(await sessionDrawer.isVisible())) {
    await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
  }
  await expect(sessionDrawer).toBeVisible();
  const sessionItems = sessionDrawer.getByRole("listitem");
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
  const emptyChat = chat.getByText("No messages yet", { exact: true }).first();
  const newSessionButton = chat.getByRole("button", {
    exact: true,
    name: "New session",
  });
  await expect(newSessionButton).toBeEnabled();
  const previousCount = await sessionItems.count();
  const reusesCurrentSession =
    (await selectedSession.isVisible()) && (await emptyChat.isVisible());

  await newSessionButton.click();
  await expect(sessionItems).toHaveCount(
    previousCount + (reusesCurrentSession ? 0 : 1),
  );
  await expect(emptyChat).toBeVisible();

  return chat;
}

export async function startIsolatedChat(page, { agentName = "" } = {}) {
  await page.goto("/#chat");

  const chat = page.getByRole("region", { name: "Chat" });
  if (agentName) {
    await getAgentTab(chat, agentName).click();
  }
  return ensureEmptyChat(chat);
}

export async function sendChatMessage(chat, content) {
  await chat.getByRole("textbox", { name: "Message" }).fill(content);
  await chat.getByRole("button", { name: "Send message" }).click();
}
