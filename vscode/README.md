# Subway Context Editor Extension

This extension integrates the Subway Context HUD directly into VS Code, Cursor, Windsurf, and Google Antigravity IDE using VS Code's internal browser (`simpleBrowser`).

## Installation

### Automatic (Recommended)
The Subway Context installer sets up the extension automatically for detected editors:
```bash
python3 install.py
```
Or with auto-install:
```bash
python3 install.py --yes
```

### Manual Installation
If you prefer to install it manually:

1. Build the `.vsix` package (if not already packaged):
   ```bash
   npm run build:vsix
   ```
   Or inside `vscode/`:
   ```bash
   npx -y @vscode/vsce@3 package --allow-missing-repository --skip-license -o subway-context.vsix
   ```

2. Install the `.vsix` into your editor:
   - **VS Code**: `code --install-extension vscode/subway-context.vsix`
   - **Cursor**: `cursor --install-extension vscode/subway-context.vsix`
   - **Windsurf**: `windsurf --install-extension vscode/subway-context.vsix`
   - **Antigravity IDE**: `~/.antigravity-ide/antigravity-ide/bin/agy-ide --install-extension vscode/subway-context.vsix`

## Usage
- Open the Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`) and run **Subway: Open HUD**.
- When the Subway HUD server starts or when using `--open-url`, the editor URI handler (`vscode://subway.subway-context/open?port=N` or `antigravity://subway.subway-context/open?port=N`) will automatically open the dashboard in your editor.
