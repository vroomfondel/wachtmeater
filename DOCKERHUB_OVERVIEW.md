[![black-lint](https://github.com/vroomfondel/wachtmeater/actions/workflows/checkblack.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/checkblack.yml)
[![mypy and pytests](https://github.com/vroomfondel/wachtmeater/actions/workflows/mypynpytests.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/mypynpytests.yml)
[![BuildAndPushMultiarch](https://github.com/vroomfondel/wachtmeater/actions/workflows/buildmultiarchandpush.yml/badge.svg)](https://github.com/vroomfondel/wachtmeater/actions/workflows/buildmultiarchandpush.yml)
[![Cumulative Clones](https://img.shields.io/endpoint?logo=github&url=https://gist.githubusercontent.com/vroomfondel/b0b152c310e1ea619801a2c00886d8fb/raw/wachtmeater_clone_count.json)](https://github.com/vroomfondel/wachtmeater)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/wachtmeater?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=PyPi+Downloads)](https://pepy.tech/projects/wachtmeater)
[![PyPI](https://img.shields.io/pypi/v/wachtmeater?logo=pypi&logoColor=white)](https://pypi.org/project/wachtmeater/)

[![Gemini_Generated_Image_vka2wyvka2wyvka2_250x250.png](https://raw.githubusercontent.com/vroomfondel/wachtmeater/main/Gemini_Generated_Image_vka2wyvka2wyvka2_250x250.png)](https://github.com/vroomfondel/wachtmeater)

# wachtmeater

MEATER BBQ probe monitoring with Matrix chat alerts and SIP phone call notifications.

Scrapes MEATER Cloud cook pages via CDP (Chrome DevTools Protocol), posts periodic status
updates to Matrix rooms, and triggers SIP phone calls when alerts fire (fire out, target
temperature reached, stall detected, etc.).

- **Source**: [GitHub](https://github.com/vroomfondel/wachtmeater)
- **PyPI**: [wachtmeater](https://pypi.org/project/wachtmeater/)
- **License**: LGPLv3

## What it does

- Periodically scrapes a MEATER Cloud cook URL via a remote Chrome browser (CDP)
- Posts temperature status (internal, ambient, target) to E2E-encrypted Matrix rooms
- Auto-creates a dedicated Matrix room per cook UUID (optional)
- Listens for Matrix commands to enable/disable alerts (tempdown, stall, wrap, ruhephase, ambient range, cookend)
- Cook-end detection via 4 mechanisms: consecutive fetch errors, probe removed, target reached, MEATER Cloud "finished" state
- Triggers SIP phone calls via sipstuff-operator when alerts fire
- Persists state per cook UUID for resumable monitoring
- TOML config support (`wachtmeater.toml`) alongside env vars / `.env`

## Quick Start

```bash
docker run --rm \
  -e MEATER_URL=https://cooks.cloud.meater.com/cook/<uuid> \
  -e BROWSER_CDP_URL=http://your-chrome:9222 \
  -e MATRIX_HOMESERVER=https://matrix.example.com \
  -e MATRIX_USER=@bot:example.com \
  -e MATRIX_PASSWORD=secret \
  xomoxcc/wachtmeater:latest wachtmeater watcher
```

## Key Environment Variables

| Variable | Description |
|---|---|
| `MEATER_URL` | MEATER Cloud cook URL to monitor (**required**) |
| `BROWSER_CDP_URL` | CDP endpoint for headless Chrome |
| `MATRIX_HOMESERVER` | Matrix homeserver URL |
| `MATRIX_USER` | Matrix bot user ID |
| `MATRIX_PASSWORD` | Matrix bot password |
| `AUTH_METHOD` | `password` or `jwt` |
| `CHECK_INTERVAL` | Seconds between checks (default: 600) |
| `COOKEND_ERROR_THRESHOLD` | Consecutive fetch errors before cook-end (default: 3) |
| `COOKEND_PROBE_REMOVED_TEMP` | Internal temp (°C) below which probe counts as removed (default: 35.0) |
| `MATRIX_AUTO_CREATE_ROOM` | Auto-create E2E-encrypted Matrix room per cook (default: false) |
| `SOPERATORURL` | sipstuff-operator call endpoint |

See the full [README](https://github.com/vroomfondel/wachtmeater#configuration) for all options.

## Image Details

- Base: `python:3.14-slim-trixie`
- Non-root user (`pythonuser`)
- Entrypoint: `tini --`
- Multi-arch: `linux/amd64`, `linux/arm64`

## License
This project is licensed under the LGPL where applicable/possible — see [LICENSE.md](LICENSE.md). Some files/parts may use other licenses: [MIT](LICENSEMIT.md) | [GPL](LICENSEGPL.md) | [LGPL](LICENSELGPL.md). Always check per‑file headers/comments.


## Authors
- Repo owner (primary author)
- Additional attributions are noted inline in code comments


## Acknowledgments
- Inspirations and snippets are referenced in code comments where appropriate.


## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.
