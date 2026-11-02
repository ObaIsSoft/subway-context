# OpenCode ACP Protocol & Permissions

- Spawns `opencode2 acp` with cwd and PWD set to the work folder, stdin and stdout pipes, one JSON-RPC message per line.
- Handshake: `initialize` {protocolVersion:1, clientCapabilities:{}} -> `session/new` {cwd, mcpServers:[]} returning sessionId and configOptions.
- Configuration: `session/set_config_option` {sessionId, configId:'model', value:'<provider/model>'}, configId 'effort' sets thought level.
- Prompt turn: `session/prompt` {sessionId, prompt:[{type:'text',text}]} runs the turn and replies when turn ends.
- Notifications: `session/update` params.update.sessionUpdate is one of tool_call, tool_call_update, agent_message_chunk, agent_thought_chunk, usage_update, available_commands_update.
- Permission requests: `session/request_permission` {sessionId, toolCall:{title, kind, rawInput}, options:[{kind, optionId}]}, kinds allow_once, allow_always, reject_once.
- Permission answers: {jsonrpc:'2.0', id, result:{outcome:{outcome:'selected', optionId}}}. Allow maps to allow_once, allow_always to allow_always, anything else rejects.
- Permissions enabled via env OPENCODE_CONFIG_CONTENT = json {"permissions":[{"action":"*","resource":"*","effect":"ask"}]}. No config file needed.
- Token spend: usage_update 'used' is context size, not spend. Real spend is queried from read-only SQLite ~/.local/share/opencode/opencode.db table session_v2 (tokens_input, tokens_output, tokens_reasoning, tokens_cache_read).
- Turn completion: when session/prompt replies, supervisor sets finished=True and terminates proc; exit code treated as 0. When stopped, sends notification session/cancel {sessionId}.
