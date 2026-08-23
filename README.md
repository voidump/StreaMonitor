# StreaMonitor
A Python3 application for monitoring and saving (mostly adult) live streams from various websites.

Inspired by [Recordurbate](https://github.com/oliverjrose99/Recordurbate)

## Supported sites
| Site name     | Abbreviation | Aliases                     | Quirks                 | Selectable resolution |
|---------------|--------------|-----------------------------|------------------------|-----------------------|
| Bongacams     | `BC`         |                             |                        | Yes                   |
| Cam4          | `C4`         |                             |                        | Yes                   |
| Cams.com      | `CC`         |                             |                        | Currently only 360p   |
| CamSoda       | `CS`         |                             |                        | Yes                   |
| Chaturbate    | `CB`         |                             |                        | Yes                   |
| DreamCam      | `DC`         |                             |                        | No                    |
| DreamCam VR   | `DCVR`       |                             | for VR videos          | No                    |
| FanslyLive    | `FL`         |                             |                        | Yes                   |
| Flirt4Free    | `F4F`        |                             |                        | Yes                   |
| MyFreeCams    | `MFC`        |                             |                        | Yes                   |
| SexChat.hu    | `SCHU`       |                             | use the id as username | No                    |
| StreaMate     | `SM`         | PornHubLive, PepperCams,... |                        | Yes                   |
| StripChat     | `SC`         | XHamsterLive,...            | must add crypto keys   | Yes                   |
| StripChat VR  | `SCVR`       |                             | for VR videos          | No                    |
| XLoveCam      | `XLC`        |                             |                        | No                    |

Currently not supported:
* Amateur.TV (They use Widevine now)
* Cherry.tv (They switched to Agora)
* ImLive (Too strict captcha protection for scraping)
* LiveJasmin (No nudity in free streams)
* ManyVids Live (They switched to Agora)

There are hundreds of clones of the sites above, you can read about them on [this site](https://adultwebcam.site/clone-sites-by-platform/).

## Requirements
* Python 3
  * Install packages listed in requirements.txt with pip.
* FFmpeg

## Usage

The application has the following interfaces:
* Console
* External console via ZeroMQ (sort of working)
* Web interface

#### Starting and console
Start the downloader (it does not fork yet)\
Automatically imports all streamers from the config file.
```
python3 Downloader.py
```

On the console you can use the following commands:
```
add <username> <site> - Add streamer to the list (also starts monitoring)
remove <username> [<site>] - Remove streamer from the list
start <username> [<site>] - Start monitoring streamer
start * - Start all
stop <username> [<site>] - Stop monitoring
stop * - stop all
status - Status display 
status2 - A slightly more readable status table
quit - Clean exit (Pressing CTRL-C also behaves like this)
```
For the `username` input, you usually have to enter the username as represented in the original URL of the room. 
Some sites are case-sensitive.

For the `site` input, you can use either the full or the short format of the site name. (And it is case-insensitive)

#### "Remote" controller
Add or remove a streamer to record (Also saves config file)
```
python3 Controller.py add <username> <website>
python3 Controller.py remove <username>
```

Start/stop recording streamers
```
python3 Controller.py <start|stop> <username>
```

List the streamers in the config
```
python3 Controller.py status
```

The remote controller talks to the downloader over ZeroMQ. Because this
interface accepts unauthenticated commands, it only binds to `127.0.0.1:6969`
by default, so `Controller.py` works from the same machine but is not reachable
from the network. You can change this with environment variables:
* `STRMNTR_ZMQ_HOST` - bind address (default `127.0.0.1`; use `0.0.0.0` to
  expose it to the network, only behind a firewall)
* `STRMNTR_ZMQ_PORT` - port (default `6969`)
* `STRMNTR_ZMQ_ENABLED` - set to `false` to disable the ZeroMQ interface entirely

#### Web interface

You can access the web interface on port 5000.
The default user name is `admin`. If the password is left at the factory
default, a **random password is generated on the first start**, printed in the
logs and saved to `.env` — check the startup log for it. Set `STRMNTR_USERNAME`
and `STRMNTR_PASSWORD` (or change them in the settings page) to pick your own;
an empty password disables the login entirely (only do this on `127.0.0.1`).
Repeated failed logins from one IP are locked out with exponential backoff.
Behind a reverse proxy every client shares the proxy's IP and would be locked
out together — set `STRMNTR_TRUST_PROXY=true` (only when the server is reached
exclusively through a trusted proxy) so the `X-Forwarded-For` client IP is
used instead.
When you set `STRMNTR_HOST` to `0.0.0.0` the console is also accessible from
other computers on the network.

##### v2 recording console (`webui`)

The web interface is the redesigned single-page console (`webui`), which is the
only skin. The legacy skins (`kseen715`, `truck-kun`, `shaftoverflow`) have been
removed. The `STRMNTR_SKIN` environment variable defaults to `webui`; any other
value falls back to `webui` with a warning, so old configurations keep working.

The console is a static front-end that talks to the backend over a JSON API
(`/api/v2/...`) and adds:

* Dashboard with status chips (recording / online / private / paused / stopped /
  offline / error …), per-site filters, column sorting and a live recording
  indicator.
* Three-layer lifecycle per streamer: **pause / resume** recording, **stop
  recording** (archive the entry for one-click restart) and **purge** (permanent
  delete, optionally removing local files).
* Multi-select **batch actions** (pause / resume / stop recording / purge).
* A **history** of purged streamers with re-add and remove.
* Built-in **recording player**: drag-to-seek, ±5/±30s jumps, playback speed,
  volume and keyboard shortcuts; the file currently being recorded is protected
  from deletion.

## Docker support

You can run this application in docker. I prefer docker-compose so I included an example docker-compose.yml file that you can use.
Simply start it in the folder with `docker-compose up`.

The container keeps **all runtime state in one directory** (`/data`, mounted
from `./data` in the compose file): `config.json`, `.env` (settings changed in
the web UI, including the password), the login session key, the StripChat key
cache and the NSFW model under `data/models/`. Migrating from an older setup:

```bash
mkdir -p data/models
mv config.json stripchat_mouflon_keys.json data/
cp models/nsfw-model.onnx data/models/   # only if you use NSFW analysis
```

Do **not** bind-mount `config.json` as a single file: atomic saves
(`os.replace`) onto a file mount fail inside the container and every change is
silently lost on restart.

## Configuration

Configuration is driven by `STRMNTR_*` environment variables (see
[parameters.py](parameters.py) for the full annotated list — every option and
its default is documented there). Values set in the web UI's settings page are
persisted into `.env` in the data directory.

Key things to know:

* `STRMNTR_DATA_DIR` — directory for all runtime state files (default: the
  working directory). Everything you would want to back up lives there:
  `config.json`, `history.json`, `.env`, `.session_secret`,
  `.strmntr_online_history.json`, `.strmntr_recording_resolution.json`,
  `stripchat_mouflon_keys.json` and (by default) `models/`.
* **Options that delete files** — be deliberate with these:
  * `STRMNTR_MIN_VIDEO_SIZE_MB` (default `10`) removes finished videos smaller
    than this, including small historic files found by the library scanner.
    Set `0` to disable.
  * `STRMNTR_NSFW_ACTION=recycle|delete` moves/removes videos classified
    non-adult; `recycle` keeps them `STRMNTR_NSFW_RECYCLE_RETENTION_DAYS` days.
  * `STRMNTR_OOS_CLEANUP_NON_ADULT` deletes non-adult videos (oldest first)
    when disk space runs low.
* **NSFW analysis** (`STRMNTR_NSFW_ENABLED`) needs the optional
  `onnxruntime`/`numpy` packages and a model file at `STRMNTR_NSFW_MODEL_PATH`
  (default `models/nsfw-model.onnx` under the data dir; bring your own model —
  see `streamonitor/nsfw_analyzer.py` for supported models). Without them,
  videos are labeled `unknown` and nothing else changes.

StripChat decryption keys are auto-extracted when possible and cached in
`stripchat_mouflon_keys.json`; you can also add keys there manually.

## Disclaimer

This program is only a proof of concept and education project, I don't encourage anybody to use it. \
Most (if not every) streamers disallow recording their shows. Please respect their wish. \
If you don't, and you record them despite this request, please don't ever publish or share any recordings. \
If you either record or share the recorded shows, you might be legally punished. \
Also, please don't use this tool for monetization in any way.
