#!/usr/bin/env node
/**
 * subway-context: zero-dependency Node.js binary wrapper
 * Allows subway-context to run directly via `npx subway-context` or `npx -y subway-context`
 * in MCP server configurations across Claude Code, OpenCode, and Antigravity.
 */
const { spawn } = require('child_process');
const path = require('path');

const serverPy = path.join(__dirname, '..', 'server.py');
const pythonBin = process.env.PYTHON_BIN || 'python3';

const args = process.argv.slice(2);
const child = spawn(pythonBin, [serverPy, ...args], {
  stdio: 'inherit',
  env: process.env,
});

child.on('error', (err) => {
  console.error('[SUBWAY] Failed to spawn python3 runner:', err.message);
  console.error('[SUBWAY] Ensure Python 3 is installed and accessible on PATH.');
  process.exit(1);
});

child.on('exit', (code, signal) => {
  process.exit(code !== null ? code : 1);
});
