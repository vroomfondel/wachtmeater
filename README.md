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

## Why this exists

Long cooks — brisket, pulled pork, pork belly — routinely run 10 to 16 hours. You go to sleep, and while you sleep the fire can go out, the meat can stall for hours, or the target temperature gets reached with nobody there to pull it off. By the time you notice, the cook is ruined or at least far from ideal.

wachtmeater watches the MEATER Cloud page continuously and reaches out only when something actually needs your attention. Periodic Matrix messages give you the full picture at a glance — current internal and ambient temps, elapsed time, remaining estimate — so you can check in whenever you want without opening an app.

When things go sideways (or the cook finishes), a SIP phone call is triggered automatically. A phone call is the reliable escape hatch here: push notifications are easy to sleep through, but a ringing phone is not.

**Tip:** Save the calling number as a contact on your phone and mark it as a VIP / priority / starred contact. That way the call bypasses Do Not Disturb mode and actually wakes you up at 3 a.m. when your brisket hits target.

## Screenshots

*Real-world Beef Brisket cook, ~14 hours, monitored from start to finish.*

![Matrix chat mid-cook status: 86 °C internal, 9 h 22 min elapsed](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Bildschirmfoto_2026-03-10_12-41-49.png)

*Matrix status update mid-cook — 86 °C internal, 9 h 22 min elapsed.*

![MEATER Cloud dashboard showing 5 minutes before cook ends, 94 °C with 95 °C target](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Bildschirmfoto_2026-03-10_12-42-31.png)

*MEATER Cloud dashboard showing "5 minutes before cook ends" — 94 °C with 95 °C target.*

![Matrix chat near-end status: 94 °C internal, 13 h 36 min elapsed](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Bildschirmfoto_2026-03-10_12-42-41.png)

*Matrix status update near the end — 94 °C internal, 13 h 36 min elapsed.*

![MEATER Cloud finished cook summary: target 95 °C, peak 94 °C, approximately 14 hours](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Bildschirmfoto_2026-03-10_12-45-47.png)

*MEATER Cloud finished cook summary — target 95 °C, peak 94 °C, total time ~14 hours.*

## Components

| Module | Description |
|---|---|
| `meater_watcher` | Persistent async event loop: periodic MEATER checks + Matrix command interface |
| `meater_monitor` | Single-shot CDP scraper: extracts temperatures, status, timing from a MEATER Cloud cook URL |
| `create_meater_watcher_job` | Deploys/destroys the watcher stack as a Kubernetes Job (Namespace, Secret, Job). In-cluster vs out-of-cluster auth detected automatically. |
| `operator` | Long-running Matrix-driven controller: spawns/destroys/lists watcher Jobs from `operator …` chat commands. Pitmaster-MXID gate, auto-trusts own Matrix devices for cross-pod E2E. |
| `call_pitmaster` | Triggers a SIP phone call via sipstuff-operator |
| `matrix_adapter` | nio-based Matrix backend; configured-room (ID *or* `#alias:srv`) and per-cook auto-created room can both be active simultaneously |

## CLI Sub-Commands

| Sub-command | Purpose |
|---|---|
| `wachtmeater watcher [--skip-startup-test-call]` | Persistent watcher event loop with Matrix listener |
| `wachtmeater monitor [<url>]` | Single-shot MEATER status check via CDP |
| `wachtmeater call <text>` | Trigger a SIP phone call via sipstuff-operator |
| `wachtmeater send-matrix <text> [--image PATH] [--room ID]` | Send message (and optional screenshot) to Matrix rooms |
| `wachtmeater deploy --meater-url <URL> [--delete]` | Deploy/delete MEATER watcher K8s Job |
| `wachtmeater operator` | Long-running Matrix-driven controller. Same as `wachtmeater-operator` console script for K8s pods. |

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
| `testcall [<text>]` | Trigger a SIP phone call to the pitmaster with optional spoken text (default: `Wachtmeater Testanruf`). The text keeps its original case so TTS pronounces names correctly. |
| `hilfe` / `help` / `?` | Show available commands |
| `stop` / `quit` / `beenden` / `exit` | Shut down the watcher loop |

## Operator Commands

Operator commands are recognised only in the room configured as `MATRIX_OPERATOR_LISTENING_ROOM`, and only when the sender's MXID matches `MATRIX_PITMASTER`. Any other sender is silently ignored.

| Command | Description |
|---|---|
| `operator new <MEATER_URL>` | Spawn a new watcher Job for the given cook URL. Calls `create_resources()` end-to-end (namespace + Secret + Job). |
| `operator delete <spec>` | Delete a watcher. `<spec>` may be a full MEATER URL, the cook UUID, the 8-char short suffix, or the 1-based index from the most recent `operator list` reply. |
| `operator list` | List active `meater-watcher-*` Jobs in the namespace with status (`Active`/`Succeeded`/`Failed`/`Pending`) and cook URL. The shown indices feed `operator delete <N>`. |
| `operator status` | Operator's own health summary: uptime, K8s API reachability, watcher count, listening room. |
| `operator help` / `hilfe` | Show available operator commands |

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
| `MATRIX_PITMASTER` | Matrix user ID to invite into auto-created rooms; also the only MXID allowed to issue `operator …` commands | `""` |
| `MATRIX_OPERATOR_LISTENING_ROOM` | Room (`!id:srv` *or* `#alias:srv`) the operator listens in for `operator …` commands | `""` |
| `CRYPTO_STORE_PATH` | Path for E2E encryption crypto store | `/data/crypto_store` |
| `OPERATOR_CRYPTO_STORE_PATH` | Separate crypto store for the operator pod (avoids SQLite races with watcher pods sharing the same MXID) | `/data/operator_crypto_store` |
| `AUTH_METHOD` | Auth method (`password` or `jwt`) | `password` |
| `KEYCLOAK_URL` | Keycloak base URL (required if `jwt`) | — |
| `KEYCLOAK_REALM` | Keycloak realm name | — |
| `KEYCLOAK_CLIENT_ID` | Keycloak client ID | — |
| `KEYCLOAK_CLIENT_SECRET` | Keycloak client secret | `""` |
| `JWT_LOGIN_TYPE` | Matrix login type for JWT | `com.famedly.login.token.oauth` |
| `SOPERATORURL` | sipstuff-operator call endpoint | `http://sipstuff-operator.sipstuff.svc.cluster.local/call` |

### Alert defaults (first run only)

These set the initial values written into a fresh per-cook state file. Once the watcher has saved state, runtime `enable …` / `disable …` Matrix commands take precedence — changes to these env vars are ignored on subsequent runs unless the state file is removed. The operator propagates all of these into spawned watcher TOMLs so each new cook starts with the operator's preferred profile.

| Variable | Description | Default |
|---|---|---|
| `ALERT_DEFAULT_TEMPDOWN_ENABLED` | Ambient temp drop alert (fire-out detection) | `true` |
| `ALERT_DEFAULT_RUHEPHASE_ENABLED` | Rest/cooldown alert | `false` |
| `ALERT_DEFAULT_RUHEPHASE_TARGET_TEMP` | Target cooldown temp (°C) | `0.0` |
| `ALERT_DEFAULT_STALL_ENABLED` | Stall detection | `false` |
| `ALERT_DEFAULT_STALL_MIN_DELTA` | Min internal-temp rise (°C) over the stall window | `1.0` |
| `ALERT_DEFAULT_WRAP_ENABLED` | One-shot wrap reminder | `false` |
| `ALERT_DEFAULT_WRAP_TARGET_TEMP` | Internal temp (°C) at which to remind about wrapping | `0.0` |
| `ALERT_DEFAULT_AMBIENT_RANGE_ENABLED` | Smoker too hot / too cold alert | `false` |
| `ALERT_DEFAULT_AMBIENT_RANGE_MIN` | Min acceptable ambient temp (°C) | `0.0` |
| `ALERT_DEFAULT_AMBIENT_RANGE_MAX` | Max acceptable ambient temp (°C) | `0.0` |
| `ALERT_DEFAULT_COOKEND_ENABLED` | Cook-end auto-detection | `true` |

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

There are two ways to run watcher Jobs in a cluster:

### A) Direct CLI deploy (one cook at a time)

```bash
wachtmeater deploy --meater-url https://cooks.cloud.meater.com/cook/<uuid>
```

This creates: Namespace `meater`, Secret with the rendered `wachtmeater.toml`, and a Job running `xomoxcc/wachtmeater:latest`. Auth picks `load_incluster_config()` automatically when `KUBERNETES_SERVICE_HOST` is set, otherwise reads `~/.kube/config`.

To tear down (deletes the per-cook Job and Secret; the shared `meater` namespace is kept):

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

### B) Operator (Matrix-driven, always-on)

For a more permanent setup, deploy the **wachtmeater operator** as a long-running Pod in the cluster. It listens in a configured Matrix room for `operator new`, `operator delete`, `operator list`, and `operator status` chat messages — so spawning a new watcher for the next cook is just a Matrix message away, no `kubectl` required.

The operator uses the same Matrix MXID as the watchers it spawns and auto-trusts every device of its own MXID at startup, so cross-pod E2E decryption works without manual verification. A separate crypto-store path (`OPERATOR_CRYPTO_STORE_PATH`) prevents SQLite-store races with watcher pods.

Reference manifests are in [`k8s/operator/`](k8s/operator/):

| File | Purpose |
|---|---|
| `serviceaccount.yaml` | Dedicated ServiceAccount `wachtmeater-operator` |
| `role.yaml` / `rolebinding.yaml` | Namespaced RBAC (jobs/secrets/pods in `meater`) |
| `deployment.yaml` | `replicas: 1`, `strategy: Recreate`, runs `wachtmeater-operator` console script |
| `configmap.example.yaml` | Example `wachtmeater.local.toml` — copy and fill in |
| `README.md` | Step-by-step setup |

Quickstart:

```bash
kubectl create namespace meater
kubectl -n meater create configmap wachtmeater-operator-config \
    --from-file=wachtmeater.local.toml=./wachtmeater.local.toml
kubectl apply -f k8s/operator/serviceaccount.yaml
kubectl apply -f k8s/operator/role.yaml
kubectl apply -f k8s/operator/rolebinding.yaml
kubectl apply -f k8s/operator/deployment.yaml
kubectl -n meater logs deploy/wachtmeater-operator -f
```

The ConfigMap entry is mounted via `subPath` into the container's WORKDIR (`/app/wachtmeater.local.toml`), so the file is picked up by the built-in config-file lookup in `read_dot_env_to_environ()` — no `CONFIG` env var needed.

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
