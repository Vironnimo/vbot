import { expect, test } from "@playwright/test";

test("Connected clients update when another browser tab opens and closes", async ({
  context,
  page,
}) => {
  await page.goto("/#settings");
  const serverInfo = page.getByRole("region", { name: "Server info" });
  const clientRows = serverInfo.locator(".s-client-row");

  await expect(clientRows).toHaveCount(1);
  await expect(
    serverInfo.getByText("This window", { exact: true }),
  ).toHaveCount(1);

  const secondPage = await context.newPage();
  await secondPage.goto("/#settings");
  const secondServerInfo = secondPage.getByRole("region", {
    name: "Server info",
  });

  await expect(clientRows).toHaveCount(2);
  await expect(secondServerInfo.locator(".s-client-row")).toHaveCount(2);
  await expect(
    secondServerInfo.getByText("This window", { exact: true }),
  ).toHaveCount(1);

  await secondPage.close();
  await expect(clientRows).toHaveCount(1);
  await expect(
    serverInfo.getByText("This window", { exact: true }),
  ).toHaveCount(1);
});
