import { expect } from "@playwright/test";

export async function startIsolatedChat(page, { agentName = "" } = {}) {
  await page.goto("/#chat");

  const chat = page.getByRole("region", { name: "Chat" });
  if (agentName) {
    await chat.getByRole("button", { exact: true, name: agentName }).click();
  }
  await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  const sessionItems = sessionDrawer.getByRole("listitem");
  const emptySessions = sessionDrawer.getByText("No sessions yet", {
    exact: true,
  });
  const currentSession = sessionDrawer.getByText("Current", { exact: true });
  await expect(async () => {
    expect(
      (await emptySessions.isVisible()) || (await currentSession.isVisible()),
    ).toBe(true);
  }).toPass();
  const previousCount = await sessionItems.count();

  await chat.getByRole("button", { exact: true, name: "New session" }).click();
  await expect(sessionItems).toHaveCount(previousCount + 1);
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();

  return chat;
}

export async function sendChatMessage(chat, content) {
  await chat.getByRole("textbox", { name: "Message" }).fill(content);
  await chat.getByRole("button", { name: "Send message" }).click();
}
