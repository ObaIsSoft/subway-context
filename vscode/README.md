# Subway Context VS Code & Antigravity Extension

Subway Context extension connects the Subway HUD directly into VS Code and Antigravity.

## Features

- **Subway: Open HUD** (`subway.openHud`): Opens the Live Subway HUD inside the editor using the built-in simple browser (`simpleBrowser.show`).
- Reads the HUD port from `~/.subway-context/hud.json` (defaults to port `7890`).
- Activates on `onStartupFinished`, pinging `/healthz` without auto-opening unprompted.

## Installation

You can package and install this extension locally:

1. Package the extension into a `.vsix` file:
   ```sh
   npx vsce package
   ```

2. Install via command line:
   ```sh
   code --install-extension subway-context-0.1.0.vsix
   ```
   Or inside VS Code / Antigravity:
   - Open Command Palette (`Cmd+Shift+P` or `Ctrl+Shift+P`).
   - Select **Extensions: Install from VSIX...**
   - Choose the generated `.vsix` file.
