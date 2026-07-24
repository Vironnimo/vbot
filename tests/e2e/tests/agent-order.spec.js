import { expect, test } from "@playwright/test";

async function rpc(request, method, params = {}) {
  const response = await request.post("/api/rpc", {
    data: { method, params },
  });
  const payload = await response.json();
  if (!response.ok() || payload?.ok !== true) {
    throw new Error(`RPC ${method} failed: ${JSON.stringify(payload)}`);
  }
  return payload.result;
}

test("Agent order persists after keyboard reordering and a reload", async ({
  page,
  request,
}) => {
  const createdAgentIds = [];
  try {
    await rpc(request, "agent.create", {
      id: "e2e-order-one",
      name: "E2E Order One",
    });
    createdAgentIds.push("e2e-order-one");
    await rpc(request, "agent.create", {
      id: "e2e-order-two",
      name: "E2E Order Two",
    });
    createdAgentIds.push("e2e-order-two");

    await page.goto("/#agents");
    const agents = page.getByRole("region", { name: "Agents" });
    const agentList = agents.getByRole("complementary", { name: "Agents" });
    const names = agentList.locator(".agent-item-name");

    await expect(names).toHaveText(["Main", "E2E Order One", "E2E Order Two"]);
    await agentList
      .getByRole("button", {
        name: "Reorder E2E Order Two (use arrow keys)",
      })
      .press("ArrowUp");

    await expect(names).toHaveText(["Main", "E2E Order Two", "E2E Order One"]);
    await expect(agentList.getByRole("status")).toContainText(
      "Moved E2E Order Two to position 2 of 3",
    );

    await page.reload();
    await expect(names).toHaveText(["Main", "E2E Order Two", "E2E Order One"]);
  } finally {
    for (const agentId of createdAgentIds.reverse()) {
      await rpc(request, "agent.delete", { id: agentId });
    }
  }
});
