# Limits and Benchmarks Reference

## Part 1: Benchmark Comparison & Recommendation
- **Artificial Analysis**: Covers coding and general indices, but requires API key authentication ([Docs](https://artificialanalysis.ai/api)).
- **LMArena (LMSYS)**: Continuous crowdsourced Elo covering coding and general ability across Claude, Gemini, DeepSeek, Kimi, and GPT ([Arena](https://lmarena.ai)). Machine-readable without API keys or paid tiers via Hugging Face Serverless API.
- **LiveBench**: Non-contaminated objective benchmark with monthly releases, but has fragmented task splits and lacks unified instant querying ([LiveBench](https://livebench.ai)).
- **Aider Leaderboard**: Specializes purely in code editing / SWE-bench; does not track general reasoning or non-coding tasks ([Aider](https://aider.chat/docs/leaderboards)).
- **OpenRouter Rankings**: Ranks models by token throughput / usage volume rather than capability; its benchmark endpoint requires an API key ([OpenRouter](https://openrouter.ai/rankings)).
- **Recommendation**: **LMArena (LMSYS)**.
  - **Exact URL**: `https://datasets-server.huggingface.co/rows?dataset=lmarena-ai/leaderboard-dataset&config=text&split=latest`
  - **Data Format**: JSON (`rows` array returning `model_name`, `organization`, `rating`, `rating_lower`, `rating_upper`, `rank`).

## Part 2: Usage Limits Replenishment Mechanics
- **Claude Pro**:
  - *5-Hour Limit*: Rolling window starting at first interaction, resetting fully 5 hours later ([Claude Help](https://support.anthropic.com/en/articles/8324991-about-claude-pro-usage-limits)).
  - *Weekly Limit*: Fixed weekly reset anchored to account creation/billing date ([Claude Help](https://support.anthropic.com/en/articles/8324991-about-claude-pro-usage-limits)).
  - *Reset API/Headers*: Messages API sends `anthropic-ratelimit-requests-reset` (RFC 3339) and `retry-after` ([Anthropic API](https://docs.anthropic.com/en/api/rate-limits)); Pro web limits use internal `GET /api/organizations/{org_id}/usage`.
- **Google Antigravity Pro**:
  - *5-Hour & Weekly Pools*: 5-hour rolling compute cycle bounded by an overarching weekly fixed ceiling ([Antigravity](https://antigravity.google)).
  - *Reset API/Headers*: Discovered via Cloud Code PA internal API `POST https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels` ([Google Cloud Code](https://cloud.google.com/products/gemini/code-assist)); returns HTTP 429 cooldowns.
- **OpenCode Free Models & OpenCode Go**:
  - *Free Models*: Rolling hourly/daily throttle windows on hosted backend ([OpenCode Docs](https://opencode.ai/docs)).
  - *OpenCode Go*: Sliding windows per model: 5-hour cap (20% of monthly quota), weekly cap (50%), and 100% monthly cycle ([OpenCode Go](https://opencode.ai/docs)).
  - *Reset API/Headers*: No CLI headers exposed; reset schedule surfaced via Web Console (`https://opencode.ai/console`) ([OpenCode Repo](https://github.com/sst/opencode)).
- **GitHub Copilot**:
  - *Replenishment*: Rolling short-term/session rate windows; premium request allowances reset monthly on fixed billing cycle / 1st of month ([GitHub Billing](https://docs.github.com/en/billing/managing-billing-for-github-copilot)).
  - *Reset API/Headers*: Returns `x-ratelimit-reset` (UTC epoch timestamp) and `retry-after` ([GitHub API Limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)).
- **Kimi Code**:
  - *Replenishment*: Rolling 5-hour session allowances coupled with fixed monthly plan cycles ([Kimi Docs](https://kimi.ai)).
  - *Reset API/Headers*: Moonshot API reports `X-RateLimit-Reset` and `retry-after` on 429 errors ([Moonshot API](https://platform.moonshot.cn/docs/api/limits)).

## Unconfirmed Items
- Existence of an official public benchmark category specifically verifying the unannounced/internal "Gemini Argon" codename.
- Public, non-authenticated rate-limit monitoring endpoints for consumer-tier Claude Pro subscriptions.
- Documented HTTP reset response headers for OpenCode CLI's free-tier proxy gateway.
