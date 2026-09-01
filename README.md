# fortune-wheel-llm

Hermes plugin — intelligent LLM fallback rotation. Works on **any profile**.

## How it works

The plugin reorders the `fallback_providers` chain (from your profile's `config.yaml`) using two modes controlled by a single variable.

- **Weighted mode** (default): `score = position*Wpos + quota*0.0005 + latency_seconds*Wlat`. A slow or quota-depleted provider drops; a fast, fresh provider rises.
- **Pure round-robin** (`HERMES_FALLBACK_WEIGHTED=false`): persistent rotation between runs, skipping providers in cooldown.

In both modes, a 429 triggers an exponential cooldown `60s * 2^n` (capped at 4h), inspired by `llm-keypool/rotator`. Cooldown can also be driven by provider headers when `llm-keypool` is installed.

Latency is estimated via EWMA (alpha=0.3) on `_api_latency_history`, polled every 2 seconds. The `gemini→google` alias is handled automatically.

## Resilience

- **No `fallback_providers` defined** → plugin is a no-op, chain returned as-is.
- **Single fallback provider** → no reordering, returned directly.
- **Missing provider in state** → default latency used, no crash.
- **Corrupted state file** → silently ignored, starts fresh.
- **Backward compatible** → migrates old `ha-fallback-rotate-state.json` if present.

## Install

### 1. Repository

```bash
cd ~/.hermes/plugins
git clone https://github.com/jsapede/fortune-wheel-llm.git
```

### 2. Enable in any profile

Edit `~/.hermes/profiles/<your-profile>/config.yaml`:

```yaml
plugins:
  enabled:
    - fortune-wheel-llm
```

Then restart the profile.

### 3. Optional prerequisites (header-aware cooldown)

Cooldown detection from provider headers uses `llm_keypool.providers.headers.extract_cooldown`. If `llm-keypool` is present, the plugin uses it automatically; otherwise it falls back to static strategies (rolling / daily / monthly).

## Example config

### `fallback_providers` block (in `~/.hermes/profiles/<your-profile>/config.yaml`)

```yaml
plugins:
  enabled:
    - fortune-wheel-llm

fallback_providers:
  - provider: google
    model: gemini-3.1-flash-lite
  - provider: nvidia
    model: nvidia/nemotron-3-super-120b-a12b:free
  - provider: openrouter
    model: nvidia/nemotron-3-super-120b-a12b:free
  - provider: cerebras
    model: gemma-4-31b
  - provider: mistral
    model: mistral-small-latest
  - provider: cohere
    model: command-a-plus-05-2026
  - provider: huggingface
    model: meta-llama/Llama-3.3-70b-Instruct
  - provider: ollama-cloud
    model: gemma4:cloud
```

Order = preferred base order for scoring.

### `.env` (optional, for header-aware cooldown via llm-keypool)

```bash
HERMES_FALLBACK_ROTATE=true
HERMES_FALLBACK_WEIGHTED=true
HERMES_FALLBACK_WEIGHT_POS=0.6
HERMES_FALLBACK_WEIGHT_LAT=1.0
HERMES_FALLBACK_LATENCY_ALPHA=0.3
HERMES_FALLBACK_LATENCY_DEFAULT_MS=800
HERMES_FALLBACK_COOLDOWN_BASE_S=60
HERMES_FALLBACK_COOLDOWN_MAX_S=14400
HERMES_FALLBACK_ROTATE_STATE=$HOME/.hermes/fortune-wheel-state.json
```

## Environment variables

| Variable | DEFAULT | Description |
| --- | --- | --- |
| `HERMES_FALLBACK_ROTATE` | `true` | Enable/disable the plugin. |
| `HERMES_FALLBACK_WEIGHTED` | `true` | `true` = position+latency+quota weighting; `false` = pure round-robin. |
| `HERMES_FALLBACK_WEIGHT_POS` | `0.6` | Position weight in the scoring chain. |
| `HERMES_FALLBACK_WEIGHT_LAT` | `1.0` | EWMA latency weight in the score. |
| `HERMES_FALLBACK_LATENCY_ALPHA` | `0.3` | EWMA alpha for latency. |
| `HERMES_FALLBACK_LATENCY_DEFAULT_MS` | `800` | Default latency when no measurement is available. |
| `HERMES_FALLBACK_COOLDOWN_BASE_S` | `60` | Base cooldown on 429. |
| `HERMES_FALLBACK_COOLDOWN_MAX_S` | `14400` | Exponential backoff cap (4h). |
| `HERMES_FALLBACK_ROTATE_STATE` | `~/.hermes/fortune-wheel-state.json` | Local state file (JSON, 0600). |

## State file

`~/.hermes/fortune-wheel-state.json` is written with `0o600` permissions. It contains only:

- `stats.<provider>.avg_ms`
- `stats.<provider>.count`
- `stats.<provider>.consecutive_429`
- `stats.<provider>.requests_today`
- `stats.<provider>.last_latency_ms`
- `stats.<provider>.cooldown_until`
- `cooldown.<provider>`

**No API keys, tokens, or prompts are stored.**

To reset manually:

```bash
truncate -s 0 ~/.hermes/fortune-wheel-state.json
```

## Testing

### Weighted mode

```bash
ha-only chat -q "test" --oneshot -v | grep -E "fortune-wheel|Fallback chain|fortune-wheel cooldown"
cat ~/.hermes/fortune-wheel-state.json
```

### Pure round-robin mode

```bash
HERMES_FALLBACK_WEIGHTED=false ha-only chat -q "test" --oneshot -v | grep -E "Fallback chain|fortune-wheel cooldown"
```

## Security

- **No network calls**.
- **No** `eval`, `exec`, `subprocess`, or dynamic loading.
- Only files read: `~/.config/llm-keypool/providers.json` (read-only), `~/.hermes/fortune-wheel-state.json` (local).
- Only files written: state file (JSON, 0o600) and its temporary replacement.
- No user data, prompts, results, or API keys are logged.
- Daemon threads; polling stops when the process exits.

## Delivered files

- `fortune_wheel_llm.py`
- `plugin.yaml`
- `README.md`
