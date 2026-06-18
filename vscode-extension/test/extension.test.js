"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

const extensionPath = path.resolve(__dirname, "..", "extension.js");

function loadExtension({ execImpl, workspace = "/tmp/greatminds-project" } = {}) {
  delete require.cache[extensionPath];
  const childProcess = require("node:child_process");
  const originalExecFile = childProcess.execFile;
  const originalLoad = Module._load;
  const commands = new Map();
  const terminals = [];
  const output = { lines: [], shown: false, appendLine(line) { this.lines.push(line); }, show() { this.shown = true; } };
  const subscriptions = [];
  const vscodeMock = {
    workspace: {
      workspaceFolders: [{ uri: { fsPath: workspace } }],
      getConfiguration(section) {
        assert.equal(section, "greatminds");
        return { get(name) { return name === "cliPath" ? "greatminds-test" : undefined; } };
      }
    },
    window: {
      createOutputChannel(name) {
        assert.equal(name, "greatminds");
        return output;
      },
      createStatusBarItem() {
        return { showCalled: false, show() { this.showCalled = true; } };
      },
      createTerminal(opts) {
        const term = {
          opts,
          shown: false,
          sent: [],
          show() { this.shown = true; },
          sendText(text) { this.sent.push(text); }
        };
        terminals.push(term);
        return term;
      },
      registerTreeDataProvider(id, provider) {
        return { id, provider };
      }
    },
    commands: {
      registerCommand(name, fn) {
        commands.set(name, fn);
        return { name, dispose() {} };
      }
    },
    EventEmitter: class {
      constructor() { this.fireCount = 0; this.event = () => {}; }
      fire() { this.fireCount += 1; }
    },
    TreeItem: class {
      constructor(label) { this.label = label; }
    },
    StatusBarAlignment: { Left: 1 }
  };

  childProcess.execFile = execImpl || ((cmd, args, options, cb) => {
    cb(null, "[]", "");
  });
  Module._load = function(request, parent, isMain) {
    if (request === "vscode") {
      return vscodeMock;
    }
    return originalLoad.apply(this, arguments);
  };
  const extension = require(extensionPath);

  function cleanup() {
    childProcess.execFile = originalExecFile;
    Module._load = originalLoad;
    delete require.cache[extensionPath];
  }

  return { extension, vscodeMock, commands, terminals, output, subscriptions, cleanup };
}

test("runGreatminds calls configured CLI with workspace env", async () => {
  const calls = [];
  const harness = loadExtension({
    execImpl(cmd, args, options, cb) {
      calls.push({ cmd, args, options });
      cb(null, "ok", "");
    }
  });
  try {
    const result = await harness.extension.runGreatminds(["agent", "tools"]);
    assert.deepEqual(result, { stdout: "ok", stderr: "" });
    assert.equal(calls[0].cmd, "greatminds-test");
    assert.deepEqual(calls[0].args, ["agent", "tools"]);
    assert.equal(calls[0].options.cwd, "/tmp/greatminds-project");
    assert.equal(calls[0].options.env.GREATMINDS_PROJECT_DIR, "/tmp/greatminds-project");
  } finally {
    harness.cleanup();
  }
});

test("AgentToolsProvider refresh parses CLI JSON and renders tree item", async () => {
  const harness = loadExtension({
    execImpl(_cmd, _args, _options, cb) {
      cb(null, JSON.stringify([
        { name: "cline", label: "Cline CLI", start_agent: true, start_modes: ["loop", "chat"], driven: true, driven_transport: "cline --json subprocess", notes: "ok" }
      ]), "");
    }
  });
  try {
    const provider = new harness.extension.AgentToolsProvider(harness.output);
    await provider.refresh();
    const children = await provider.getChildren();
    assert.equal(children.length, 1);
    const item = provider.getTreeItem(children[0]);
    assert.equal(item.label, "Cline CLI");
    assert.equal(item.description, "cline");
    assert.match(item.tooltip, /start-agent: loop,chat/);
    assert.match(item.tooltip, /driven: cline --json subprocess/);
  } finally {
    harness.cleanup();
  }
});

test("activate registers cockpit commands and opens terminals", async () => {
  const harness = loadExtension({
    execImpl(_cmd, args, _options, cb) {
      if (args.join(" ") === "agent tools --json") {
        cb(null, "[]", "");
        return;
      }
      cb(null, "human output", "");
    }
  });
  try {
    harness.extension.activate({ subscriptions: harness.subscriptions });
    for (const name of [
      "greatminds.refresh",
      "greatminds.openDashboard",
      "greatminds.openDrivenLog",
      "greatminds.openCoordd",
      "greatminds.showAgentTools",
      "greatminds.showStandStatus"
    ]) {
      assert.ok(harness.commands.has(name), `${name} registered`);
    }
    await harness.commands.get("greatminds.openDashboard")();
    await harness.commands.get("greatminds.openDrivenLog")();
    await harness.commands.get("greatminds.openCoordd")();
    assert.equal(harness.terminals.length, 3);
    assert.equal(harness.terminals[0].sent[0], "greatminds-test dashboard");
    assert.equal(harness.terminals[1].sent[0], "greatminds-test driven-log");
    assert.equal(harness.terminals[2].sent[0], "greatminds-test coordd --verbose");
    await harness.commands.get("greatminds.showStandStatus")();
    assert.match(harness.output.lines.join("\n"), /human output/);
  } finally {
    harness.cleanup();
  }
});
