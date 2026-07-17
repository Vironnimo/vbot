import { expect, test } from "@playwright/test";

test("a new Session becomes current and appears in the Session list", async ({
  page,
}) => {
  await page.goto("/#chat");

  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("button", { exact: true, name: "Sessions" }).click();

  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  const sessionList = sessionDrawer.getByRole("list");
  const emptySessions = sessionDrawer.getByText("No sessions yet", {
    exact: true,
  });
  const currentSession = sessionDrawer.getByText("Current", { exact: true });
  await expect(async () => {
    expect(
      (await emptySessions.isVisible()) || (await currentSession.isVisible()),
    ).toBe(true);
  }).toPass();
  const previousCount = await sessionList.getByRole("listitem").count();

  await chat.getByRole("button", { exact: true, name: "New session" }).click();

  await expect(sessionList.getByRole("listitem")).toHaveCount(
    previousCount + 1,
  );
  await expect(sessionList.getByText("Current", { exact: true })).toBeVisible();
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();
});
