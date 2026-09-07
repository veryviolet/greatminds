"use strict";

// Loaded by the real VS Code Extension Development Host, not the Node API mock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");

async function until(predicate, label, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}

exports.run = async function run() {
  assert.equal(process.platform, "linux", "this smoke inspects Linux process argv");
  const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const cli = vscode.workspace.getConfiguration("greatminds").get("cliPath");
  assert.ok(root.includes("project with spaces"));
  assert.ok(cli.includes("cli with spaces $(literal)"));
  const extension = vscode.extensions.getExtension("veryviolet.greatminds");
  assert.ok(extension, "development extension discovered");
  await extension.activate();
  assert.ok(extension.isActive);
  const commands = await vscode.commands.getCommands(true);
  for (const name of ["refresh", "newChat", "attachChat", "closeChat", "openRunEvents", "openCoordd"]) {
    assert.ok(commands.includes(`greatminds.${name}`), name);
  }
  await vscode.commands.executeCommand("greatminds.showAgentTools");
  await vscode.commands.executeCommand("greatminds.refresh");
  const observed = [];
  for (const [name, argv] of [
    ["openRunEvents", ["run", "events", "--follow"]],
    ["openCoordd", ["coordd", "--verbose"]],
    ["openDashboard", ["dashboard"]]
  ]) {
    const term = await vscode.commands.executeCommand(`greatminds.${name}`);
    let pid;
    try {
      assert.equal(term.creationOptions.shellPath, cli);
      assert.deepEqual(term.creationOptions.shellArgs, argv);
      pid = await Promise.race([term.processId,
        new Promise((_, reject) => setTimeout(() => reject(new Error("terminal pid timeout")), 10000))]);
      assert.ok(Number.isInteger(pid) && pid > 0);
      await until(() => {
        try {
          const actual = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0").filter(Boolean);
          return actual.includes(cli) && argv.every((arg, i) => actual[actual.length - argv.length + i] === arg);
        } catch { return false; }
      }, `${name} real CLI process`);
      observed.push({ command: name, argv, processObserved: true });
    } finally {
      term.dispose();
      if (pid) await until(() => !fs.existsSync(`/proc/${pid}`), `${name} terminal cleanup`);
    }
  }
  const statePath = path.join(root, ".greatminds", ".runtime", "state.json");
  if (fs.existsSync(statePath)) assert.deepEqual(JSON.parse(fs.readFileSync(statePath, "utf8")).runs, {});
  const report = { version: 1, vscode: vscode.version, activation: true,
    cliBackend: true, terminalProcesses: observed, agentRuns: 0,
    limits: ["Quick-pick interactions and live ACP chat were not exercised in this host smoke."] };
  fs.writeFileSync(path.join(root, "host-smoke-result.json"), JSON.stringify(report, null, 2) + "\n");
};
