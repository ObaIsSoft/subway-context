const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');
const crypto = require('crypto');

const terminalStates = new WeakMap();
const activeTerminals = new Set();
const eventRetryBuffer = [];
let eventPostChain = Promise.resolve();
let pollInterval = null;
let hudSharingEnabled = false;
let sharingPollInterval = null;

function isTerminalSharingActive() {
  const vsSetting = vscode.workspace.getConfiguration('subway').get('terminalSharing.enabled', false);
  return !!(hudSharingEnabled || vsSetting);
}

async function updateHudSharingStatus() {
  const data = await getJson('/api/terminals');
  if (data && typeof data.sharing === 'boolean') {
    hudSharingEnabled = data.sharing;
  }
}

function getHudPort() {
  try {
    const hudPath = path.join(os.homedir(), '.subway-context', 'hud.json');
    if (fs.existsSync(hudPath)) {
      const data = JSON.parse(fs.readFileSync(hudPath, 'utf8'));
      if (data && data.port) {
        return Number(data.port);
      }
    }
  } catch (e) {}
  return 7890;
}

function checkHealth(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/healthz`, { timeout: 2000 }, (res) => {
      resolve(res.statusCode >= 200 && res.statusCode < 400);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

function getHudToken() {
  try {
    const tokenPath = path.join(os.homedir(), '.subway-context', 'hud_token');
    return fs.readFileSync(tokenPath, 'utf8').trim();
  } catch (e) {
    return '';
  }
}

function postJson(pathStr, bodyObj) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (!settled) {
        settled = true;
        resolve(result);
      }
    };
    try {
      const req = http.request({
        hostname: '127.0.0.1',
        port: getHudPort(),
        path: pathStr,
        method: 'POST',
        timeout: 2000,
        headers: {
          'X-Subway-Token': getHudToken(),
          'Content-Type': 'application/json'
        }
      }, (res) => {
        res.resume();
        finish(res.statusCode >= 200 && res.statusCode < 300);
      });
      req.on('error', () => finish(false));
      req.on('timeout', () => {
        req.destroy();
        finish(false);
      });
      req.end(JSON.stringify(bodyObj));
    } catch (e) {
      finish(false);
    }
  });
}

function getJson(pathStr) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (!settled) {
        settled = true;
        resolve(result);
      }
    };
    try {
      const req = http.request({
        hostname: '127.0.0.1',
        port: getHudPort(),
        path: pathStr,
        method: 'GET',
        timeout: 2000,
        headers: {
          'X-Subway-Token': getHudToken(),
          'Content-Type': 'application/json'
        }
      }, (res) => {
        let response = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { response += chunk; });
        res.on('end', () => {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            finish(null);
            return;
          }
          try {
            finish(JSON.parse(response));
          } catch (e) {
            finish(null);
          }
        });
      });
      req.on('error', () => finish(null));
      req.on('timeout', () => {
        req.destroy();
        finish(null);
      });
      req.end();
    } catch (e) {
      finish(null);
    }
  });
}

function getTerminalState(terminal) {
  let entry = terminalStates.get(terminal);
  if (!entry) {
    entry = {
      id: crypto.randomUUID(),
      windowId: vscode.env.sessionId,
      sharing: false,
      allowCommands: false,
      runInFlight: false
    };
    terminalStates.set(terminal, entry);
    activeTerminals.add(terminal);
  }
  return entry;
}

function terminalOptInBody(terminal, entry) {
  return {
    terminal_id: entry.id,
    name: terminal.name,
    window_id: entry.windowId,
    folder: (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''),
    share_enabled: entry.sharing,
    allow_commands: entry.allowCommands,
    shell_integration: !!terminal.shellIntegration
  };
}

function bufferTerminalEvent(pathStr, body) {
  if (eventRetryBuffer.length >= 50) eventRetryBuffer.shift();
  eventRetryBuffer.push({ path: pathStr, body });
}

function postTerminalEvent(body) {
  if (!isTerminalSharingActive()) return;
  eventPostChain = eventPostChain.then(async () => {
    if (!isTerminalSharingActive()) {
      eventRetryBuffer.length = 0;
      return;
    }
    while (eventRetryBuffer.length) {
      if (!isTerminalSharingActive()) {
        eventRetryBuffer.length = 0;
        return;
      }
      const queued = eventRetryBuffer[0];
      if (!(await postJson(queued.path, queued.body))) break;
      eventRetryBuffer.shift();
    }
    if (!(await postJson('/api/terminal_event', body))) {
      if (isTerminalSharingActive()) {
        bufferTerminalEvent('/api/terminal_event', body);
      }
    }
  });
}

async function readExecutionOutput(execution) {
  let output = '';
  try {
    for await (const chunk of execution.read()) {
      output += chunk;
      if (output.length > 65536) {
        output = output.slice(0, 65536);
        break;
      }
    }
  } catch (err) {
    // Some shells and terminals do not support reading execution output.
  }
  return output;
}

async function pollTerminalRuns(terminal, entry) {
  if (entry.pollInFlight || entry.runInFlight) return;
  entry.pollInFlight = true;
  try {
    const data = await getJson(`/api/pending_runs?terminal_id=${encodeURIComponent(entry.id)}`);
    if (terminalStates.get(terminal) !== entry || !data || !Array.isArray(data.runs)) return;
    const run = data.runs.find((candidate) => candidate.status === 'pending');
    if (!run) return;

    entry.runInFlight = true;
    if (!terminal.shellIntegration) {
      await postJson('/api/run_result', {
        run_id: run.id, output: '', exit_code: null, error: 'no shell integration'
      });
      entry.runInFlight = false;
      return;
    }

    const pendingRun = { runId: run.id, execution: null };
    entry.pendingRun = pendingRun;
    try {
      pendingRun.execution = terminal.shellIntegration.executeCommand(run.command);
    } catch (err) {
      await postJson('/api/run_result', {
        run_id: run.id, output: '', exit_code: null, error: 'execution failed'
      });
      entry.pendingRun = null;
      entry.runInFlight = false;
    }
  } finally {
    entry.pollInFlight = false;
  }
}

function activate(context) {
  const openHudCommand = vscode.commands.registerCommand('subway.openHud', () => {
    const port = getHudPort();
    const url = `http://127.0.0.1:${port}`;
    vscode.commands.executeCommand('simpleBrowser.show', url);  // It takes a plain URL string.
  });
  context.subscriptions.push(openHudCommand);

  const uriHandler = vscode.window.registerUriHandler({
    handleUri(uri) {
      let port = getHudPort();
      if (uri.query) {
        try {
          const params = new URLSearchParams(uri.query);
          const p = params.get('port');
          if (p && !isNaN(Number(p))) {
            port = Number(p);
          }
        } catch (e) {
          const match = uri.query.match(/port=(\d+)/);
          if (match) {
            port = Number(match[1]);
          }
        }
      }
      const url = `http://127.0.0.1:${port}`;
      vscode.commands.executeCommand('simpleBrowser.show', url);
    }
  });
  context.subscriptions.push(uriHandler);

  const shareTerminalCommand = vscode.commands.registerCommand('subway.shareTerminal', async () => {
    const terminal = vscode.window.activeTerminal;
    if (!terminal) {
      vscode.window.showWarningMessage('No active terminal.');
      return;
    }
    const entry = getTerminalState(terminal);
    entry.sharing = true;
    entry.allowCommands = false;
    await postJson('/api/terminal_opt_in', terminalOptInBody(terminal, entry));
    const message = terminal.shellIntegration
      ? 'Terminal sharing enabled.'
      : 'Terminal shared, but nothing will be captured until shell integration is available for this terminal.';
    vscode.window.showInformationMessage(message);
  });
  context.subscriptions.push(shareTerminalCommand);

  const stopSharingTerminalCommand = vscode.commands.registerCommand('subway.stopSharingTerminal', async () => {
    const terminal = vscode.window.activeTerminal;
    if (!terminal) {
      vscode.window.showWarningMessage('No active terminal.');
      return;
    }
    const entry = terminalStates.get(terminal);
    if (!entry || !entry.sharing) {
      vscode.window.showInformationMessage('Terminal is not shared.');
      return;
    }
    entry.sharing = false;
    await postJson('/api/terminal_opt_in', terminalOptInBody(terminal, entry));
    vscode.window.showInformationMessage('Terminal sharing stopped.');
  });
  context.subscriptions.push(stopSharingTerminalCommand);

  const allowTerminalCommandsCommand = vscode.commands.registerCommand('subway.allowTerminalCommands', async () => {
    const terminal = vscode.window.activeTerminal;
    if (!terminal) {
      vscode.window.showWarningMessage('No active terminal.');
      return;
    }
    const entry = terminalStates.get(terminal);
    if (!entry || !entry.sharing) {
      vscode.window.showWarningMessage('Share this terminal first.');
      return;
    }
    entry.allowCommands = !entry.allowCommands;
    await postJson('/api/terminal_opt_in', terminalOptInBody(terminal, entry));
    vscode.window.showInformationMessage(
      `Commands from the HUD ${entry.allowCommands ? 'allowed' : 'blocked'} for this terminal.`
    );
  });
  context.subscriptions.push(allowTerminalCommandsCommand);

  const startExecutionListener = vscode.window.onDidStartTerminalShellExecution((e) => {
    if (!isTerminalSharingActive()) return;
    const entry = terminalStates.get(e.terminal);
    if (!entry || !entry.sharing) return;
    entry.pendingCommand = {
      command: e.execution.commandLine.value,
      startTs: Date.now() / 1000
    };
  });
  const endExecutionListener = vscode.window.onDidEndTerminalShellExecution(async (e) => {
    const entry = terminalStates.get(e.terminal);
    if (!entry) return;
    const pendingRun = entry.pendingRun && entry.pendingRun.execution === e.execution
      ? entry.pendingRun
      : null;
    if (!entry.sharing && !pendingRun) return;
    const pendingCommand = (entry.sharing && isTerminalSharingActive()) ? entry.pendingCommand : null;
    if (!pendingCommand && !pendingRun) return;
    entry.pendingCommand = null;
    const output = await readExecutionOutput(e.execution);
    if (terminalStates.get(e.terminal) !== entry) return;
    if (pendingCommand && isTerminalSharingActive()) {
      postTerminalEvent({
        terminal_id: entry.id,
        command: pendingCommand.command,
        exit_code: (typeof e.exitCode === 'number' ? e.exitCode : null),
        start_ts: pendingCommand.startTs,
        end_ts: Date.now() / 1000,
        output,
        caller_id: 'editor',
        name: e.terminal.name,
        window_id: entry.windowId,
        folder: (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '')
      });
    }
    if (pendingRun) {
      await postJson('/api/run_result', {
        run_id: pendingRun.runId,
        output,
        exit_code: (typeof e.exitCode === 'number' ? e.exitCode : null),
        error: ''
      });
      if (entry.pendingRun === pendingRun) {
        entry.pendingRun = null;
        entry.runInFlight = false;
      }
    }
  });
  context.subscriptions.push(startExecutionListener, endExecutionListener);

  const closeTerminalListener = vscode.window.onDidCloseTerminal((terminal) => {
    terminalStates.delete(terminal);
    activeTerminals.delete(terminal);
  });
  context.subscriptions.push(closeTerminalListener);

  pollInterval = setInterval(() => {
    if (!isTerminalSharingActive()) return;
    for (const terminal of activeTerminals) {
      const entry = terminalStates.get(terminal);
      if (entry && entry.sharing && entry.allowCommands && !entry.runInFlight) {
        void pollTerminalRuns(terminal, entry);
      }
    }
  }, 3000);

  void updateHudSharingStatus();
  sharingPollInterval = setInterval(updateHudSharingStatus, 30000);

  // Activation onStartupFinished: if the HUD answers /healthz, do nothing more, never auto-open.
  const port = getHudPort();
  checkHealth(port).then((isHealthy) => {
    if (isHealthy) {
      // HUD is running; do nothing more and never auto-open.
    }
  });
}

function deactivate() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
  if (sharingPollInterval) {
    clearInterval(sharingPollInterval);
    sharingPollInterval = null;
  }
}

module.exports = {
  activate,
  deactivate
};
