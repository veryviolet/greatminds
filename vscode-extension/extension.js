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

function terminal(name, args) {
  const term = vscode.window.createTerminal({
    name,
    cwd: workspaceRoot(),
    shellPath: cliPath(), shellArgs: args,
    env: { GREATMINDS_PROJECT_DIR: workspaceRoot() }
  });
  term.show();
  return term;
}

function chatTerminal(conversationId) {
  if (typeof conversationId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/.test(conversationId)) {
    throw new Error("Invalid conversation identity from CLI");
  }
  const root = workspaceRoot();
  const term = vscode.window.createTerminal({
    name: `greatminds chat ${conversationId.slice(0, 8)}`, cwd: root,
    shellPath: cliPath(), shellArgs: ["chat", "--project-dir", root, "talk", conversationId],
    env: { GREATMINDS_PROJECT_DIR: root }
  });
  term.show();
  return term;
}

async function chatRows(action) {
  const { stdout } = await runGreatminds(["chat", "--project-dir", workspaceRoot(), action]);
  const rows = JSON.parse(stdout);
  if (!Array.isArray(rows)) throw new Error("Invalid chat metadata from CLI");
  return rows;
}

async function selectConversation() {
  const rows = await chatRows("list");
  return vscode.window.showQuickPick(rows.filter(row => !row.closed).map(row => ({
    label: row.binding_id, description: row.id,
    detail: `${row.task_id || "Project conversation"} · ${row.dispatch?.status || "queued"}`,
    conversationId: row.id
  })), { placeHolder: "Choose a daemon-owned conversation" });
}

function chatCommand(output, action) {
  return async () => {
    try { await action(); }
    catch (error) {
      output.appendLine(`chat failed: ${error.stderr || error.message}`);
      output.show(true);
    }
  };
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
    const treeItem = new vscode.TreeItem(item.id);
    treeItem.description = "ACP · configured";
    treeItem.tooltip = `Adapter: ${item.adapter_version}\nHarness: ${item.harness_version}\nBindings: ${(item.bindings || []).join(", ")}\nConfiguration does not prove live compatibility.`;
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
    vscode.commands.registerCommand("greatminds.openDashboard", () => terminal("greatminds dashboard", ["dashboard"])),
    vscode.commands.registerCommand("greatminds.openRunEvents", () => terminal("greatminds run events", ["run", "events", "--follow"])),
    vscode.commands.registerCommand("greatminds.openCoordd", () => terminal("greatminds coordd", ["coordd", "--verbose"])),
    vscode.commands.registerCommand("greatminds.newChat", chatCommand(output, async () => {
      const rows = await chatRows("bindings");
      const selected = await vscode.window.showQuickPick(rows.map(row => ({
        label: row.role, description: `${row.id} · ${row.agent}`, bindingId: row.id
      })), { placeHolder: "Choose a role binding for ACP chat" });
      if (!selected) return;
      const task = await vscode.window.showInputBox({ prompt: "Optional exact task ID; leave empty for a project conversation" });
      if (task === undefined) return;
      const args = ["chat", "--project-dir", workspaceRoot(), "create", selected.bindingId];
      if (task.trim()) args.push("--task", task.trim());
      const { stdout } = await runGreatminds(args);
      chatTerminal(JSON.parse(stdout).conversation_id);
    })),
    vscode.commands.registerCommand("greatminds.attachChat", chatCommand(output, async () => {
      const selected = await selectConversation();
      if (selected) chatTerminal(selected.conversationId);
    })),
    vscode.commands.registerCommand("greatminds.closeChat", chatCommand(output, async () => {
      const selected = await selectConversation();
      if (!selected) return;
      const { stdout } = await runGreatminds(["chat", "--project-dir", workspaceRoot(), "close", selected.conversationId]);
      output.appendLine(stdout.trimEnd());
      output.show(true);
    })),
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
