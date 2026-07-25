import { execFileSync } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { environment } from "./environment.js";

export default async function stopServer() {
  execFileSync(
    environment.python,
    [
      path.join(environment.repoRoot, "scripts", "test-env.py"),
      "stop",
      "--host",
      environment.host,
      "--port",
      String(environment.port),
      "--data-dir",
      environment.dataDir,
    ],
    {
      cwd: environment.repoRoot,
      stdio: "inherit",
    },
  );
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  stopServer().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
