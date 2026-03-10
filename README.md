[![black-lint](https://github.com/vroomfondel/wachtmeater/actions/workflows/checkblack.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/checkblack.yml)
[![mypy and pytests](https://github.com/vroomfondel/wachtmeater/actions/workflows/mypynpytests.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/mypynpytests.yml)
[![BuildAndPushMultiarch](https://github.com/vroomfondel/wachtmeater/actions/workflows/buildmultiarchandpush.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/buildmultiarchandpush.yml)
[![Cumulative Clones](https://img.shields.io/endpoint?logo=github&url=https://gist.githubusercontent.com/vroomfondel/b0b152c310e1ea619801a2c00886d8fb/raw/wachtmeater_clone_count.json)](https://github.com/vroomfondel/wachtmeater)
[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/wachtmeater?logo=docker)](https://hub.docker.com/r/xomoxcc/wachtmeater/tags)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/wachtmeater?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=PyPi+Downloads)](https://pepy.tech/projects/wachtmeater)
[![PyPI](https://img.shields.io/pypi/v/wachtmeater?logo=pypi&logoColor=white)](https://pypi.org/project/wachtmeater/)

[![Gemini_Generated_Image_vka2wyvka2wyvka2_250x250.png](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Gemini_Generated_Image_vka2wyvka2wyvka2_250x250.png)](https://hub.docker.com/r/xomoxcc/wachtmeater/tags)

# wachtmeater

MEATER BBQ probe monitoring with Matrix chat alerts and SIP phone call notifications.
Scrapes MEATER Cloud cook pages via CDP (Chrome DevTools Protocol), posts status updates
to E2E-encrypted Matrix rooms, and calls you when something needs attention (fire out,
target reached, stall, cook ended, etc.). Supports automatic per-cook Matrix room creation
and TOML-based configuration.

## Components

| Module | Description |
|---|---|
| `meater_watcher` | Persistent async event loop: periodic MEATER checks + Matrix command interface |
| `meater_monitor` | Single-shot CDP scraper: extracts temperatures, status, timing from a MEATER Cloud cook URL |
| `create_meater_watcher_job` | Deploys the watcher stack as a Kubernetes Job (Namespace, ConfigMap, Secret, Job) |
| `call_pitmaster` | Triggers a SIP phone call via sipstuff-operator |

## CLI Sub-Commands

| Sub-command | Purpose |
|---|---|
| `wachtmeater watcher [--skip-startup-test-call]` | Persistent watcher event loop with Matrix listener |
| `wachtmeater monitor [<url>]` | Single-shot MEATER status check via CDP |
| `wachtmeater call <text>` | Trigger a SIP phone call via sipstuff-operator |
| `wachtmeater send-matrix <text> [--image PATH] [--room ID]` | Send message (and optional screenshot) to Matrix rooms |
| `wachtmeater deploy --meater-url <URL> [--delete]` | Deploy/delete MEATER watcher K8s Job |

## Matrix Commands

Send these in the Matrix room the bot has joined (case-insensitive):

| Command | Description |
|---|---|
| `status` | Fetch and post current MEATER status |
| `enable tempdown` | Enable ambient temp drop alert (fire out detection) |
| `disable tempdown` | Disable ambient temp drop alert |
| `enable ruhephase <temp>` | Enable rest/cooldown alert at target temp (C) |
| `disable ruhephase` | Disable rest/cooldown alert |
| `enable stall [<delta>]` | Enable stall detection (optional: min rise in C) |
| `disable stall` | Disable stall detection |
| `enable wrap <temp>` | Enable wrap reminder at internal temp (C) |
| `disable wrap` | Disable wrap reminder |
| `enable ambient <min> <max>` | Enable ambient range alert (min/max C) |
| `disable ambient` | Disable ambient range alert |
| `enable cookend` | Enable cook-end detection (auto-stop) |
| `disable cookend` | Disable cook-end detection |
| `reset stall` | Reset stall alert (re-arms after it fired) |
| `reset wrap` | Reset wrap alert (re-arms after it fired) |
| `hilfe` / `help` / `?` | Show available commands |
| `stop` / `quit` / `beenden` / `exit` | Shut down the watcher loop |

## Cook-End Detection

When `cookend` is enabled, the watcher uses four independent mechanisms to detect that a cook has finished:

| # | Mechanism | Trigger | Default threshold |
|---|---|---|---|
| A | **Consecutive fetch errors** | CDP scrape fails N times in a row | `COOKEND_ERROR_THRESHOLD` = 3 |
| B | **Probe removed** | Internal temp drops below threshold after having been above 50 °C | `COOKEND_PROBE_REMOVED_TEMP` = 35.0 °C |
| C | **Status "done"** | MEATER reports internal temp ≥ target | — |
| D | **MEATER Cloud "finished"** | Cook page shows summary view (`#cook.finished` CSS class) | — |

Mechanisms A, B, and D post a Matrix message and trigger a SIP phone call. Mechanism C sets cook-ended state silently (the watcher loop posts a generic shutdown message). The watcher loop stops automatically once any mechanism fires.

## Configuration

Settings are loaded from a TOML config file (`wachtmeater.toml` or path in `$CONFIG`), a flat `.env` file, or plain environment variables. Existing env vars always take precedence. See `wachtmeater.toml.example` for a full annotated example.

| Variable | Description | Default |
|---|---|---|
| `MEATER_URL` | MEATER Cloud cook URL to monitor | — (**required**) |
| `BROWSER_CDP_URL` | CDP endpoint for headless Chrome | `http://chrome-kasmvnc.kasmvnc.svc.cluster.local:9222` |
| `SCREENSHOT_DIR` | Directory for cook page screenshots | `/data` |
| `STATE_FILE_DIR` | Directory for the JSON state file | `/data` |
| `STATE_FILE_NAME` | State file name (auto-generated from cook UUID when empty) | `""` |
| `CHECK_INTERVAL` | Seconds between periodic checks | `600` |
| `STALL_WINDOW` | Number of consecutive checks for stall detection | `3` |
| `AMBIENT_TEMP_DROP_THRESHOLD` | Ambient temp drop (°C) to trigger fire-out alert | `10` |
| `COOKEND_ERROR_THRESHOLD` | Consecutive fetch errors before declaring cook ended | `3` |
| `COOKEND_PROBE_REMOVED_TEMP` | Internal temp (°C) below which probe counts as removed | `35.0` |
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://synapse.matrix.svc.cluster.local:8008` |
| `MATRIX_USER` | Matrix bot user ID | — (**required**) |
| `MATRIX_PASSWORD` | Matrix bot password | — (**required**) |
| `MATRIX_ROOM` | Default room ID or alias to join | `!exampleroom:matrix.example.com` |
| `MATRIX_AUTO_CREATE_ROOM` | Auto-create an E2E-encrypted Matrix room per cook UUID | `false` |
| `MATRIX_PITMASTER` | Matrix user ID to invite into auto-created rooms | `""` |
| `CRYPTO_STORE_PATH` | Path for E2E encryption crypto store | `/data/crypto_store` |
| `AUTH_METHOD` | Auth method (`password` or `jwt`) | `password` |
| `KEYCLOAK_URL` | Keycloak base URL (required if `jwt`) | — |
| `KEYCLOAK_REALM` | Keycloak realm name | — |
| `KEYCLOAK_CLIENT_ID` | Keycloak client ID | — |
| `KEYCLOAK_CLIENT_SECRET` | Keycloak client secret | `""` |
| `JWT_LOGIN_TYPE` | Matrix login type for JWT | `com.famedly.login.token.oauth` |
| `SOPERATORURL` | sipstuff-operator call endpoint | `http://sipstuff-operator.sipstuff.svc.cluster.local/call` |

## Installation

```bash
pip install wachtmeater
```

### From source

```bash
git clone https://github.com/vroomfondel/wachtmeater.git
cd wachtmeater
make install
source .venv/bin/activate
```

## Running locally

```bash
# Single status check
./runcli.sh monitor

# Persistent watcher with Matrix listener
./runcli.sh watcher
```

Override settings in `runcli.local.include.sh` (sourced automatically, gitignored).

## Kubernetes Deployment

```bash
wachtmeater deploy --meater-url https://cooks.cloud.meater.com/cook/<uuid>
```

This creates: Namespace `meater`, ConfigMap with scripts, Secret with `.env`, and a Job running `xomoxcc/wachtmeater:latest`.

To tear down (deletes the per-cook Job, Secret, and ConfigMap; the shared `meater` namespace is kept):

```bash
wachtmeater deploy --meater-url https://cooks.cloud.meater.com/cook/<uuid> --delete
```

<details><summary>Alternative: module invocation</summary>

```bash
python -m wachtmeater.create_meater_watcher_job \
  --meater-url https://cooks.cloud.meater.com/cook/<uuid>
python -m wachtmeater.create_meater_watcher_job --delete
```

</details>

## Development

| Target | Description |
|---|---|
| `make install` | Create virtualenv and install all dependencies |
| `make tests` | Run pytest |
| `make lint` | Format code with black (line length 120) |
| `make isort` | Sort imports with isort |
| `make tcheck` | Static type checking with mypy (strict) |
| `make commit-checks` | Run pre-commit hooks on all files |
| `make prepare` | Run tests + commit-checks |
| `make pypibuild` | Build sdist + wheel with hatch |
| `make pypipush` | Publish to PyPI with hatch |

## License
This project is licensed under the LGPL where applicable/possible — see [LICENSE.md](LICENSE.md). Some files/parts may use other licenses: [MIT](LICENSEMIT.md) | [GPL](LICENSEGPL.md) | [LGPL](LICENSELGPL.md). Always check per‑file headers/comments.


## Authors
- Repo owner (primary author)
- Additional attributions are noted inline in code comments


## Acknowledgments
- Inspirations and snippets are referenced in code comments where appropriate.


## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.
