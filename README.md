# fortune-wheel-llm

Plugin Hermes — rotation intelligente des fallback LLM pour le profil `ha-only`.

## Principe

Le plugin pivote l'ordre de la chaîne `fallback_providers` (config.yaml) de deux manières contrôlées par une seule variable.

- Par défaut, **mode pondéré** : `score = position*Wpos + quota*0.0005 + latency_seconds*Wlat`. Un provider lent ou proche de son quota RPD recule ; un provider rapide et frais monte.
- Si `HERMES_FALLBACK_WEIGHTED=false`, **mode round-robin pur** : rotation persistée entre les runs, avec saut des providers en cooldown.

Dans les deux modes, un provider en 429 déclenche un cooldown exponentiel `60s * 2^n` (cap 4h), inspiré de `llm-keypool/rotator`. Le cooldown peut aussi être ponctué par les headers du provider quand `llm-keypool` est installé.

La latence est estimée par EWMA (alpha=0.3) sur `_api_latency_history`, pollué toutes les 2 secondes. L'alias `gemini→google` est assuré automatiquement.

## Install

### 1. Dépôt

```bash
cd ~/.hermes/plugins
git clone https://github.com/jimmysapède/fortune-wheel-llm.git
```

### 2. Activer dans le profil `ha-only`

Édite `~/.hermes/profiles/ha-only/config.yaml` :

```yaml
plugins:
  enabled:
    - fortune-wheel-llm
```

Puis redémarre le profil `ha-only`.

### 3. Prérequis optionnels (pour cooldown header-aware)

La détection de cooldown via les headers du provider utilise `llm_keypool.providers.headers.extract_cooldown`. Si `llm-keypool` est présent dans l'environnement, le plugin l'utilise automatiquement ; sinon il se rabat sur les stratégies de fallback statiques (rolling / daily / monthly).

## Variables d'environnement

| Variable | DÉFAUT | Description |
| --- | --- | --- |
| `HERMES_FALLBACK_ROTATE` | `true` | Active/désactive le plugin. |
| `HERMES_FALLBACK_WEIGHTED` | `true` | `true` = pondération position+latency+quota ; `false` = round-robin pur. |
| `HERMES_FALLBACK_WEIGHT_POS` | `0.6` | Poids de la position dans la chaîne (ordre préférentiel). |
| `HERMES_FALLBACK_WEIGHT_LAT` | `1.0` | Poids de la latence EWMA dans le score. |
| `HERMES_FALLBACK_LATENCY_ALPHA` | `0.3` | Alpha EWMA de la latence. |
| `HERMES_FALLBACK_LATENCY_DEFAULT_MS` | `800` | Latence par défaut quand aucune mesure n'est disponible. |
| `HERMES_FALLBACK_COOLDOWN_BASE_S` | `60` | Cooldown de base (429). |
| `HERMES_FALLBACK_COOLDOWN_MAX_S` | `14400` | Cap du backoff exponentiel (4h). |
| `HERMES_FALLBACK_ROTATE_STATE` | `~/.hermes/ha-fallback-rotate-state.json` | Fichier d'état local (JSON, 0600). |
| `HERMES_PROFILE` | `default` | Profil courant ; injecté dans la clé d'état. |

## Fichier d'état

`~/.hermes/ha-fallback-rotate-state.json` est écrit avec permissions `0o600` par le plugin. Il contient uniquement :

- `stats.<provider>.avg_ms`
- `stats.<provider>.count`
- `stats.<provider>.consecutive_429`
- `stats.<provider>.requests_today`
- `stats.<provider>.last_latency_ms`
- `stats.<provider>.cooldown_until`
- `cooldown.<provider>`

**Aucune clé API, aucun token, aucun prompt n'est stocké.**

Le fichier peut être réinitialisé manuellement :

```bash
truncate -s 0 ~/.hermes/ha-fallback-rotate-state.json
```

## Test

### Mode pondéré

```bash
ha-only chat -q "test" --oneshot -v | grep -E "fortunewheel-llm|Fallback chain|fortunewheel cooldown"
cat ~/.hermes/ha-fallback-rotate-state.json
```

On vérifie que :

- la chaîne affichée correspond au mode actif,
- un 429 laisse un cooldown pour le provider concerné,
- l'état JSON ne contient aucune clé API.

### Mode round-robin pur

```bash
HERMES_FALLBACK_WEIGHTED=false ha-only chat -q "test" --oneshot -v | grep -E "Fallback chain|fortunewheel cooldown"
```

La chaîne doit revenir à un ordre déterministe par rotation persistée, en sautant les providers en cooldown.

## Sécurité

- Le plugin n'effectue **aucun appel réseau**.
- Il ne contient **pas** de `eval`, `exec`, `subprocess`, ni de chargement dynamique.
- Les seuls fichiers lus sont :
  - `~/.config/llm-keypool/providers.json`, en lecture seule,
  - `~/.hermes/ha-fallback-rotate-state.json`, en lecture/écriture locale.
- Les seuls fichiers écrits sont le state file (JSON, 0o600) et son temporaire de remplacement.
- Aucune donnée utilisateur, prompt, résultat, clé API n'est journalisée.
- Les threads usés sont daemon ; le polling s'arrête à l'extinction du processus.

## Fichiers livrés

- `fortune_wheel_llm.py`
- `plugin.yaml`
- `README.md`
