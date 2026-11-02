const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');

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

  // Activation onStartupFinished: if the HUD answers /healthz, do nothing more, never auto-open.
  const port = getHudPort();
  checkHealth(port).then((isHealthy) => {
    if (isHealthy) {
      // HUD is running; do nothing more and never auto-open.
    }
  });
}

function deactivate() {}

module.exports = {
  activate,
  deactivate
};
