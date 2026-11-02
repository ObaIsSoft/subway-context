# OpenCode 2.0 Permission System & Service API

## Confirmed Findings

### 1. Service Discovery & Authentication
- Service details are stored in `~/.local/state/opencode/service.json` (`url`, `pid`, `password`, `version`, `id`).
- Password is also stored in `~/.config/opencode/service.json` (`{"password": "..."}`).
- Authentication uses HTTP Basic Auth: `Authorization: Basic <base64("opencode:" + password)>`. The username is fixed: `"opencode"`.

### 2. Location Selection
- Default service location is `~`.
- Parameter `--param directory=X` / `?directory=X` is ignored. Location is selected via:
  - Header: `x-opencode-directory: <directory_path>`
  - Query parameter: `?location[directory]=<directory_path>` (OpenAPI `deepObject`, explode=true)
  - CLI: `opencode2 api GET /api/permission/request --header x-opencode-directory:<path>`.

### 3. HTTP Routes
- `GET /api/permission/request` (`v2.permission.request.list`): Lists pending permission requests for a location.
- `POST /api/session/{sessionID}/permission/{requestID}/reply` (`v2.session.permission.reply`): Answers a request.
  Request body: `{"reply": "once" | "always" | "reject", "message": string | null}`.
- `GET /api/session/{sessionID}/permission` (`v2.session.permission.list`): Lists session permissions.
- `GET /api/session/{sessionID}/permission/{requestID}` (`v2.session.permission.get`).
- `POST /api/session/{sessionID}/permission` (`v2.session.permission.create`).
- `GET /api/permission/saved` (`v2.permission.saved.list`): Lists saved permission rules.
- `DELETE /api/permission/saved/{id}` (`v2.permission.saved.remove`).
- `GET /api/event` (`v2.event.subscribe`): SSE stream for real-time events.

### 4. Permission Request Structure & Events
- Request payload: `{"id": "^per...", "sessionID": "^ses...", "action": string, "resources": string[], "source": {"type": "tool", "messageID": string, "id": string}}`.
- Event names confirmed in binary: `permission.asked`, `permission.replied`, `permission.rejected`.

### 5. Configuration Keys
- Configured under `permissions` ruleset array:
  `"permissions": [{"action": "<action>", "resource": "<glob>", "effect": "allow" | "deny" | "ask"}]`.
- Allowed effects: `"allow"`, `"deny"`, `"ask"`.
- Allowed reply values: `"once"`, `"always"`, `"reject"`.

## Inferred Findings
- `opencode2 run --format json` buffers or omits live permission prompts in headless mode when waiting on the background service. An external supervisor must poll `GET /api/permission/request` (or subscribe to `GET /api/event`) with `x-opencode-directory` to detect pending requests and POST replies.
- `"always"` persists the permission rule in the database `~/.local/share/opencode/opencode.db` (`permission` table) for future executions.
