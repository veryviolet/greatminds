"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

const extensionPath = path.resolve(__dirname, "..", "extension.js");

function loadExtension({ execImpl, workspace = "/tmp/greatminds-project", configuredCli = "greatminds-test",
                         pick = rows => rows[0], taskInput = "" } = {}) {
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
        return { get(name) { return name === "cliPath" ? configuredCli : undefined; } };
      }
    },
    window: {
      async showQuickPick(rows) { return pick(rows); },
      async showInputBox() { return taskInput; },
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
        { id: "cline", transport: "acp", adapter_version: "fixture-adapter", harness_version: "fixture-harness", bindings: ["developer"], verification: "configured" }
      ]), "");
    }
  });
  try {
    const provider = new harness.extension.AgentToolsProvider(harness.output);
    await provider.refresh();
    const children = await provider.getChildren();
    assert.equal(children.length, 1);
    const item = provider.getTreeItem(children[0]);
    assert.equal(item.label, "cline");
    assert.equal(item.description, "ACP · configured");
    assert.match(item.tooltip, /Adapter: fixture-adapter/);
    assert.match(item.tooltip, /Bindings: developer/);
    assert.match(item.tooltip, /does not prove live compatibility/);
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

test("new ACP chat passes task and workspace as argv and launches only the CLI client", async () => {
  const calls = [];
  const root = "/tmp/project with spaces $(literal)";
  const harness = loadExtension({ workspace: root, configuredCli: "/tmp/cli with spaces", taskInput: "0001-work",
    execImpl(cmd, args, _options, cb) {
      calls.push({cmd, args});
      if (args.includes("bindings")) cb(null, JSON.stringify([{id:"dev",role:"DEVELOPER",agent:"fixture"}]), "");
      else if (args.includes("create")) cb(null, JSON.stringify({conversation_id:"conversation-1"}), "");
      else cb(null, "[]", "");
    }
  });
  try {
    harness.extension.activate({subscriptions: harness.subscriptions});
    await harness.commands.get("greatminds.newChat")();
    assert.deepEqual(calls.find(c => c.args.includes("create")).args,
      ["chat", "--project-dir", root, "create", "dev", "--task", "0001-work"]);
    assert.equal(harness.terminals.length, 1);
    assert.equal(harness.terminals[0].opts.shellPath, "/tmp/cli with spaces");
    assert.deepEqual(harness.terminals[0].opts.shellArgs, ["chat", "--project-dir", root, "talk", "conversation-1"]);
    assert.deepEqual(harness.terminals[0].sent, []);
  } finally { harness.cleanup(); }
});

test("attach and close use existing conversation identity without recreating a session", async () => {
  const calls = [];
  const harness = loadExtension({execImpl(_cmd,args,_options,cb) {
    calls.push(args);
    if (args.includes("list")) cb(null, JSON.stringify([
      {id:"closed",binding_id:"dev",closed:true},
      {id:"existing",binding_id:"dev",closed:false,task_id:"0001-work"}
    ]), "");
    else cb(null, args.includes("close") ? '{"close_requested":true}' : "[]", "");
  }});
  try {
    harness.extension.activate({subscriptions:harness.subscriptions});
    await harness.commands.get("greatminds.attachChat")();
    assert.equal(harness.terminals[0].opts.shellArgs.at(-1), "existing");
    await harness.commands.get("greatminds.closeChat")();
    assert.deepEqual(calls.find(args => args.includes("close")),
      ["chat","--project-dir","/tmp/greatminds-project","close","existing"]);
    assert.equal(harness.terminals.length, 1);
    assert.ok(!calls.some(args => args.includes("create")));
    assert.match(harness.output.lines.join("\n"), /close_requested/);
  } finally { harness.cleanup(); }
});

test("cancelled role selection performs no create and no terminal launch", async () => {
  const calls=[];
  const harness=loadExtension({pick:()=>undefined,execImpl(_cmd,args,_opts,cb) {
    calls.push(args);cb(null,"[]","");
  }});
  try {
    harness.extension.activate({subscriptions:harness.subscriptions});
    await harness.commands.get("greatminds.newChat")();
    assert.equal(harness.terminals.length,0);
    assert.ok(!calls.some(args=>args.includes("create")));
  } finally { harness.cleanup(); }
});
