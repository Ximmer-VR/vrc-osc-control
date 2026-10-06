# VRChat OSC Control

A Windows desktop app for selecting VRChat avatar OSC parameters, assigning shared names, and letting remote users control them through a website.

## Requirements

- Windows with Python 3.10 or newer and Tkinter
- VRChat running with OSC enabled
- Network access to `osccontrol.app` to create a share session

## Run From Source

In PowerShell from the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python osccontrol.py
```

The app uses `resource/icon.ico` for its window icon.

The interface follows the Windows light/dark appearance setting and updates while the app is running. The theme preference is detected automatically; no settings-file changes are needed.

## VRChat Connection

The app discovers VRChat's `_oscjson._tcp.local.` OSCQuery service with Zeroconf. It uses the advertised host and TCP port for OSCQuery, and uses the advertised `OSC_IP` and `OSC_PORT` properties for outgoing OSC when available. By default, VRChat receives OSC on UDP `9000`. The app listens for VRChat OSC on an OS-assigned UDP port and advertises that port through `_osc._udp.local.` and its OSCQuery `HOST_INFO`, so it doesn't contend for the common `9001` receive port.

Avatar parameter definitions and initial values are read from OSCQuery. The app also listens for `/avatar/change` and OSC updates for selected shared parameters. It queries OSCQuery for the current avatar ID and falls back to the most recent `/avatar/change` event when necessary. Parameters can be added manually if discovery is unavailable.

On first launch, the app creates `%LOCALAPPDATA%\VRChatOSCControl\settings.json` with defaults. Edit that file to configure connection and logging settings. OSCQuery and outgoing OSC overrides default to `null`, which lets Zeroconf discovery supply connection information. The receive listener defaults to `127.0.0.1` and `osc_receive_port: 0`; port `0` asks the OS for an available UDP port, which the app advertises to VRChat. Set a nonzero `osc_receive_port` to use a fixed port. The old default of `9001` is migrated to `0`.

Settings include `oscquery_url`, `osc_send_host`, `osc_send_port`, `osc_receive_host`, `osc_receive_port`, `api_websocket_url`, `control_url_template`, `log_level`, and `log_file`. Leave `oscquery_url`, `osc_send_host`, and `osc_send_port` as `null` to use discovered values and localhost fallbacks. The default websocket and share-link URLs point to `osccontrol.app`.

Avatar names and shared parameter definitions are saved per avatar to `%LOCALAPPDATA%\VRChatOSCControl\shared_parameters.json`. When the avatar ID changes, the app loads that avatar's saved name and parameter set. Override the location with `VRCHAT_OSC_SHARED_PARAMETERS_FILE`.

## Websocket Protocol

The default websocket URL is `wss://osccontrol.app/ws`; override it with `OSC_API_WS_URL`. Session creation sends the current avatar and the selected parameters, including their current values:

```json
{
  "type": "register",
  "version": 1,
  "parameters": [
    {
      "path": "/avatar/parameters/Wave",
      "name": "Wave",
      "type": "bool",
      "value": true
    }
  ]
}
```

The API generates the session token and returns a `registered` message containing the session URL:

```json
{
  "type": "registered",
  "version": 1,
  "token": "grFMwOxz",
  "url": "http://osccontrol.app/t/grFMwOxz",
  "count": 16
}
```

The app puts the API-provided URL in the control session link and uses the token for subsequent updates. If the response omits `url`, the app falls back to the configured `control_url_template`. The app expects remote changes in this format:

If the websocket connection attempt receives HTTP 503 or closes with code 1013 and reason `instance draining`, the app retries the connection up to three times to allow the load balancer to route it to another API shard.

After registration, and whenever the active avatar name changes, the app sends:

```json
{
  "type": "set_avatar_name",
  "name": "Avatar Alpha"
}
```

```json
{
  "type": "parameter_changed",
  "path": "/avatar/parameters/Wave",
  "name": "Wave",
  "parameter_type": "bool",
  "value": false
}
```

The app validates the path, name, and type against the active session, then sends the value to VRChat over OSC. Local OSC changes are reported to the API as:

```json
{
  "type": "update_parameter",
  "token": "<session token>",
  "path": "/avatar/parameters/Wave",
  "value": false
}
```

While the session is active, changes to the shared-parameter list are sent over the same websocket. Adding parameters uses the same parameter objects as registration, including their current values:

```json
{
  "type": "add_parameters",
  "token": "<session token>",
  "parameters": [
    {
      "path": "/avatar/parameters/Wave",
      "name": "Wave",
      "type": "bool",
      "value": true
    }
  ]
}
```

Removing selected parameters sends their OSC paths:

```json
{
  "type": "remove_parameters",
  "token": "<session token>",
  "paths": ["/avatar/parameters/Wave"]
}
```

When the avatar changes, the app sends `clear_parameters` and then adds the shared parameters saved for the new avatar. The clear message contains only its type and session token:

```json
{"type":"clear_parameters","token":"<session token>"}
```

Adding or removing parameters during an active session also updates that avatar's saved parameter file.

The default share link is `https://osccontrol.app/?token={token}`. Override it with `OSC_API_CONTROL_URL`, keeping `{token}` as the placeholder. The API's websocket endpoint and message schema must match this documented contract.

## Logs

Rotating logs are written to `%LOCALAPPDATA%\VRChatOSCControl\app.log`. INFO is the default level; set `VRCHAT_OSC_LOG_LEVEL=DEBUG` for more detail or use `WARNING`/`ERROR` to reduce it. Set `VRCHAT_OSC_LOG_FILE` to choose another log path. Session tokens and parameter values are not written to logs.

## Build Windows Executable

Install PyInstaller in the active environment and run the provided batch file:

```powershell
python -m pip install pyinstaller
.\make_exe.bat
```

The script builds a one-file, windowed executable under `dist` and bundles `resource/icon.ico`.

## License

This project is source-available under the proprietary terms in [LICENSE](LICENSE). Source viewing, personal noncommercial use, and private modifications are permitted; redistribution and commercial use are not. Third-party dependencies remain under their respective licenses.