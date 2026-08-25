# Swing Music for Omarchy Quattro

Search and play a self-hosted [Swing Music](https://github.com/swingmx/swingmusic)
library from the Omarchy top bar.

## Features

- Connects with a Swing URL, username, and password.
- Stores the password in the desktop Secret Service keyring.
- Shows Songs, Albums, Artists, and Playlists category bubbles.
- Loads up to 20 recently played items (then alphabetical fallback) when a
  category is selected before searching.
- Searches within the selected category.
- Plays songs, albums, artists, and playlists through `mpv`.
- Shows album art and Previous, Pause/Play, Next, and Stop controls.
- Opens result pages in Swing with right-click.
- Accepts browser URLs ending in `/#/` and works with HTTP or HTTPS servers.

## Install

```sh
omarchy plugin add https://github.com/JakeWayneMurray/Swing-Music.git --enable
```

The plugin appears as `io.github.jakewaynemurray.swing-music`. Click its music
note icon in the bar, then enter the Swing server URL, username, and password.

For a local server, use its API origin, for example:
`http://192.168.2.69:1970` (the helper removes a copied `/#/` suffix).

## Dependencies and permissions

- Omarchy Quattro and its standard QML imports.
- Python 3 standard library (no Python packages are installed).
- `mpv` for audio playback.
- `secret-tool` and a Secret Service provider for password storage.
- `xdg-open` for the optional Open Swing action.

The helper runs as the logged-in user. It makes authenticated HTTP requests to
the configured Swing server, starts a user-owned `mpv` process, and creates
mode-0600 temporary runtime files under `$XDG_RUNTIME_DIR`. It does not use
sudo, install services, or modify system files. The saved config contains only
the server URL and username; the password remains in the desktop keyring.
HTTP response bodies are capped at 8 MiB (error bodies at 64 KiB), playback is
capped at 1,000 tracks, and generated playlist/state/output data has strict
size limits before it is written or sent to the shell.

## Remove

```sh
omarchy plugin remove io.github.jakewaynemurray.swing-music
```

Removing the plugin removes its bar widget. It does not delete the keyring
entry or your Swing Music server data. Use the plugin's Change connection flow
or remove the `omarchy/swing-music` keyring entry separately if desired.

## Validate from a checkout

```sh
omarchy plugin validate .
qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml Panel.qml
```
