"use strict";

const cp = require("child_process");
const vscode = require("vscode");

function workspaceRoot() {
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  return folder ? folder.uri.fsPath : process.cwd();
}

function cliPath() {
  return vscode.workspace.getConfiguration("greatminds").get("cliPath") || "greatminds";
}

function runGreatminds(args, options = {}) {
  const cwd = options.cwd || workspaceRoot();
  return new Promise((resolve, reject) => {
    cp.execFile(cliPath(), args, {
      cwd,
      env: { ...process.env, GREATMINDS_PROJECT_DIR: cwd },
      maxBuffer: 4 * 1024 * 1024
    }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function terminal(name, command) {
  const term = vscode.window.createTerminal({
    name,
    cwd: workspaceRoot(),
    env: { GREATMINDS_PROJECT_DIR: workspaceRoot() }
  });
  term.show();
  term.sendText(command);
  return term;
}

class AgentToolsProvider {
  constructor(output) {
    this.output = output;
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.items = [];
  }

  refresh() {
    return runGreatminds(["agent", "tools", "--json"])
      .then(({ stdout }) => {
        this.items = JSON.parse(stdout);
        this._onDidChangeTreeData.fire();
      })
      .catch((error) => {
        this.items = [];
        this.output.appendLine(`agent tools failed: ${error.stderr || error.message}`);
        this.output.show(true);
        this._onDidChangeTreeData.fire();
      });
  }

  getTreeItem(item) {
    const treeItem = new vscode.TreeItem(item.label || item.name);
    treeItem.description = `${item.name}`;
    treeItem.tooltip = `start-agent: ${item.start_agent ? item.start_modes.join(",") : "no"}\ndriven: ${item.driven ? item.driven_transport : "no"}\n${item.notes || ""}`;
    treeItem.contextValue = "greatmindsTool";
    return treeItem;
  }

  getChildren() {
    return Promise.resolve(this.items);
  }
}

function activate(context) {
  const output = vscode.window.createOutputChannel("greatminds");
  const provider = new AgentToolsProvider(output);
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.text = "$(hubot) greatminds";
  status.command = "greatminds.refresh";
  status.show();

  context.subscriptions.push(
    output,
    status,
    vscode.window.registerTreeDataProvider("greatminds.tools", provider),
    vscode.commands.registerCommand("greatminds.refresh", () => provider.refresh()),
    vscode.commands.registerCommand("greatminds.openDashboard", () => terminal("greatminds dashboard", `${cliPath()} dashboard`)),
    vscode.commands.registerCommand("greatminds.openDrivenLog", () => terminal("greatminds driven-log", `${cliPath()} driven-log`)),
    vscode.commands.registerCommand("greatminds.openCoordd", () => terminal("greatminds coordd", `${cliPath()} coordd --verbose`)),
    vscode.commands.registerCommand("greatminds.showAgentTools", async () => {
      const { stdout } = await runGreatminds(["agent", "tools"]);
      output.appendLine(stdout.trimEnd());
      output.show(true);
      await provider.refresh();
    }),
    vscode.commands.registerCommand("greatminds.showStandStatus", async () => {
      const { stdout } = await runGreatminds(["stand", "status"]);
      output.appendLine(stdout.trimEnd());
      output.show(true);
    })
  );

  provider.refresh();
}

function deactivate() {}

module.exports = {
  activate,
  deactivate,
  runGreatminds,
  AgentToolsProvider
};
