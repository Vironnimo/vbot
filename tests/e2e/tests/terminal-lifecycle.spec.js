import { spawnSync } from "node:child_process";
import { userInfo } from "node:os";

import { expect, test } from "@playwright/test";

import { environment } from "../environment.js";

const DEFAULT_SHELL_MARKER = "VBOT-DEFAULT-SHELL-READY";

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

async function cleanupTerminals(request) {
  const result = await rpc(request, "terminal.list");
  await Promise.all(
    result.terminals
      .filter((terminal) => !["exited", "error"].includes(terminal.state))
      .map((terminal) =>
        rpc(request, "terminal.kill", { terminal_id: terminal.terminal_id }),
      ),
  );
  const retained = await rpc(request, "terminal.list");
  await Promise.all(
    retained.terminals.map((terminal) =>
      rpc(request, "terminal.forget", { terminal_id: terminal.terminal_id }),
    ),
  );
}

async function openTerminals(page) {
  await page.goto("/#terminals");
  await expect(page.getByLabel("Connected")).toBeVisible();
  await expect(
    page.getByRole("main").getByRole("region", { name: "Terminals" }),
  ).toBeVisible();
}

async function startTerminal(page, { command, arguments: args = [] } = {}) {
  await page.getByRole("button", { name: "New terminal" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New terminal" });
  await expect(dialog).toBeVisible();
  if (command) {
    await dialog.locator("#terminal-start-command").fill(command);
  }
  if (args.length > 0) {
    await dialog.locator("#terminal-start-arguments").fill(args.join("\n"));
  }
  await dialog.getByRole("button", { name: "Start terminal" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(
    page.locator(".terminals-view__terminal-host .xterm-rows"),
  ).toBeVisible();
}

function terminalOutput(page) {
  return page.locator(".terminals-view__terminal-host .xterm-rows");
}

async function sendToSelectedTerminal(page, text) {
  await page
    .getByRole("group", { name: "Live terminal. Click to take control." })
    .click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

async function stopSelectedTerminal(page) {
  await page.getByRole("button", { name: "Stop terminal" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Stop this Terminal Session?",
  });
  await dialog.getByRole("button", { name: "Stop terminal" }).click();
}

function pythonHarness(label, backgroundDelaySeconds = null) {
  const setTitle = `lambda value:(sys.stdout.write('\\x1b]0;'+value+'\\x07'),sys.stdout.flush())`;
  const background =
    backgroundDelaySeconds === null
      ? ""
      : `threading.Timer(${backgroundDelaySeconds},lambda:(set_title('E2E-${label} active'),print('BACKGROUND-${label}',flush=True))).start();`;
  return `import sys,threading;set_title=${setTitle};set_title('E2E-${label}');print('READY-${label}',flush=True);${background}[print('${label}:'+line.rstrip(),flush=True) for line in sys.stdin]`;
}

function windowsCommandExists(command) {
  return (
    spawnSync("where.exe", [command], {
      encoding: "utf8",
      windowsHide: true,
    }).status === 0
  );
}

function expectedDefaultShell() {
  if (process.platform !== "win32") {
    return process.env.SHELL || userInfo().shell || "/bin/sh";
  }
  if (windowsCommandExists("pwsh.exe")) {
    return "pwsh.exe";
  }
  if (windowsCommandExists("powershell.exe")) {
    return "powershell.exe";
  }
  return process.env.COMSPEC || "cmd.exe";
}

function expectedDefaultShellTitle(command) {
  const executable = command.split(/[\\/]/).pop().toLowerCase();
  if (executable === "pwsh.exe" || executable === "pwsh") {
    return "PowerShell";
  }
  if (executable === "powershell.exe" || executable === "powershell") {
    return "Windows PowerShell";
  }
  if (executable === "cmd.exe" || executable === "cmd") {
    return "Command Prompt";
  }
  return command;
}

function defaultShellProbe(command) {
  if (process.platform !== "win32") {
    return `printf '${DEFAULT_SHELL_MARKER}\\n'`;
  }
  return command.toLowerCase().endsWith("cmd.exe")
    ? `echo ${DEFAULT_SHELL_MARKER}`
    : `Write-Output ${DEFAULT_SHELL_MARKER}`;
}

test.beforeEach(async ({ request }) => {
  await cleanupTerminals(request);
});

test.afterEach(async ({ request }) => {
  await cleanupTerminals(request);
});

test("the platform default shell starts as a native interactive terminal", async ({
  page,
}) => {
  await openTerminals(page);
  await startTerminal(page);

  const expectedCommand = expectedDefaultShell();
  await expect(page.locator(".terminals-view__identity-primary")).toContainText(
    expectedDefaultShellTitle(expectedCommand),
  );
  await sendToSelectedTerminal(page, defaultShellProbe(expectedCommand));
  await expect(terminalOutput(page)).toContainText(DEFAULT_SHELL_MARKER);
});

test("multiple terminals keep output, input, reconnect, and stop lifecycle isolated", async ({
  page,
}) => {
  await openTerminals(page);
  const terminalItems = page.locator(".terminals-view__list-item");

  await startTerminal(page, {
    command: environment.python,
    arguments: ["-u", "-c", pythonHarness("ALPHA", 1)],
  });
  await expect(terminalItems).toHaveCount(1);
  await expect(terminalItems.nth(0)).toContainText("E2E-ALPHA");
  await expect(terminalOutput(page)).toContainText("READY-ALPHA");

  await startTerminal(page, {
    command: environment.python,
    arguments: ["-u", "-c", pythonHarness("BRAVO")],
  });
  await expect(terminalItems).toHaveCount(2);
  await expect(terminalItems.nth(0)).toHaveAttribute("aria-current", "true");
  await expect(terminalItems.nth(0)).toContainText("E2E-BRAVO");
  await expect(terminalOutput(page)).toContainText("READY-BRAVO");
  await sendToSelectedTerminal(page, "message-for-bravo");
  await expect(terminalOutput(page)).toContainText("BRAVO:message-for-bravo");

  await terminalItems.nth(1).click();
  await expect(terminalItems.nth(1)).toContainText("E2E-ALPHA active");
  await expect(terminalOutput(page)).toContainText("BACKGROUND-ALPHA");
  await expect(terminalOutput(page)).not.toContainText(
    "BRAVO:message-for-bravo",
  );
  await sendToSelectedTerminal(page, "message-for-alpha");
  await expect(terminalOutput(page)).toContainText("ALPHA:message-for-alpha");

  await terminalItems.nth(0).click();
  await expect(terminalOutput(page)).toContainText("BRAVO:message-for-bravo");
  await expect(terminalOutput(page)).not.toContainText(
    "ALPHA:message-for-alpha",
  );

  const navigation = page.getByRole("navigation", { name: "Sections" });
  await navigation.getByRole("button", { name: "Projects" }).click();
  await navigation.getByRole("button", { name: "Terminals" }).click();
  await expect(terminalItems).toHaveCount(2);
  await expect(terminalOutput(page)).toContainText("BRAVO:message-for-bravo");

  await stopSelectedTerminal(page);
  await expect(terminalItems).toHaveCount(2);
  await expect(terminalOutput(page)).toContainText("BRAVO:message-for-bravo");
  await terminalItems.filter({ hasText: "E2E-ALPHA" }).click();
  await expect(terminalOutput(page)).toContainText("ALPHA:message-for-alpha");
  await sendToSelectedTerminal(page, "alpha-survived-bravo-stop");
  await expect(terminalOutput(page)).toContainText(
    "ALPHA:alpha-survived-bravo-stop",
  );

  await stopSelectedTerminal(page);
  await expect(terminalItems).toHaveCount(2);
  await expect(terminalOutput(page)).toContainText(
    "ALPHA:alpha-survived-bravo-stop",
  );
  await expect(page.getByText("Read-only history", { exact: false })).toBeVisible();
});
