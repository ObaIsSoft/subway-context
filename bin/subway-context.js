#!/usr/bin/env node
/**
 * subway-context: zero-dependency Node.js binary wrapper
 * Allows subway-context to run directly via `npx subway-context` or `npx -y subway-context`
 * in MCP server configurations across Claude Code, OpenCode, and Antigravity.
 */
const { spawn, spawnSync } = require('child_process');
const path = require('path');

const serverPy = path.join(__dirname, '..', 'server.py');

function resolvePython() {
  if (process.env.PYTHON_BIN) {
    return { bin: process.env.PYTHON_BIN, prefixArgs: [] };
  }
  const isWin = process.platform === 'win32';
  const candidates = isWin
    ? [
        { bin: 'python3', prefixArgs: [] },
        { bin: 'py', prefixArgs: ['-3'] },
        { bin: 'python', prefixArgs: [] },
      ]
    : [
        { bin: 'python3', prefixArgs: [] },
        { bin: 'python', prefixArgs: [] },
      ];

  for (const c of candidates) {
    try {
      const res = spawnSync(c.bin, [...c.prefixArgs, '--version'], { stdio: 'ignore' });
      if (res.status === 0) {
        return c;
      }
    } catch (e) {
      // Ignore and try next
    }
  }
  return candidates[0];
}

const { bin: pythonBin, prefixArgs } = resolvePython();
const args = process.argv.slice(2);
const child = spawn(pythonBin, [...prefixArgs, serverPy, ...args], {
  stdio: 'inherit',
  env: process.env,
});

function forwardSignal(signal) {
  if (child && !child.killed) {
    try {
      child.kill(signal);
    } catch (e) {
      // Ignore errors if child has already exited
    }
  }
}

process.on('SIGINT', () => forwardSignal('SIGINT'));
process.on('SIGTERM', () => forwardSignal('SIGTERM'));

child.on('error', (err) => {
  console.error('[SUBWAY] Failed to spawn python runner:', err.message);
  console.error('[SUBWAY] Ensure Python 3 is installed and accessible on PATH.');
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (code !== null) {
    process.exit(code);
  } else if (signal) {
    // If killed by signal, exit with 128 + signal number or 1
    process.exit(1);
  } else {
    process.exit(1);
  }
});
