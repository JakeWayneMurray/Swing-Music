#!/usr/bin/env python3
"""Small stdlib-only bridge between Omarchy Quattro and Swing Music."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


APP_ID = "omarchy-swing-music"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy" / "swing-music"
CONFIG_FILE = CONFIG_DIR / "config.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-swing-music"
PLAYER_PID_FILE = RUNTIME_DIR / "player.pid"
PLAYER_SOCKET = RUNTIME_DIR / "player.sock"
PLAYER_STATE_FILE = RUNTIME_DIR / "player.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_PLAYBACK_TRACKS = 1000
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 1 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class SwingError(RuntimeError):
    pass


def emit(payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            {"ok": False, "error": "Swing Music response is too large."},
            separators=(",", ":"),
        ).encode("utf-8")
    print(encoded.decode("utf-8"))


def read_limited(stream, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise SwingError("Swing Music response is too large.")
        chunks.append(chunk)


def read_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_DIR, 0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_DIR, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, CONFIG_FILE)


def normalize_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SwingError("Enter a full http:// or https:// Swing Music URL.")
    # Users commonly copy Swing's browser URL, which ends in ``/#/``. The
    # hash route belongs to the web client and is never sent to the server;
    # keeping it here would produce /#/auth/login and a misleading HTTP 405.
    parsed = parsed._replace(path=parsed.path.rstrip("/"), params="", query="", fragment="")
    return parsed.geturl().rstrip("/")


def keyring(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["secret-tool", *args],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SwingError("secret-tool is required to store the Swing Music password securely.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SwingError("The desktop keyring did not respond.") from exc


def lookup_password(url: str, username: str) -> str:
    result = keyring("lookup", "service", APP_ID, "url", url, "username", username)
    if result.returncode != 0 or not result.stdout:
        raise SwingError("Password not found in the desktop keyring. Choose ‘Change connection’ and sign in again.")
    return result.stdout.rstrip("\n")


def store_password(url: str, username: str, password: str) -> None:
    result = keyring(
        "store", "--label=Swing Music for Omarchy",
        "service", APP_ID, "url", url, "username", username,
        stdin=password,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise SwingError(detail or "Could not store the password in the desktop keyring.")


def request_json(url: str, *, token: str = "", body: dict | None = None) -> dict:
    # Cloudflare's managed bot checks commonly reject Python's default
    # ``Python-urllib/x.y`` user agent before the request reaches Swing. Use a
    # desktop-client UA so self-hosted instances behind Cloudflare behave the
    # same way as Swing's browser client.
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
            "Omarchy-Swing/1.0"
        ),
    }
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise SwingError("Swing Music response is too large.")
                except ValueError:
                    pass
            return json.loads(read_limited(response, MAX_RESPONSE_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(read_limited(exc, MAX_ERROR_BODY_BYTES).decode("utf-8"))
            message = payload.get("msg") or payload.get("error")
        except SwingError:
            raise
        except Exception:
            message = None
        if exc.code in {401, 422}:
            raise SwingError("Swing Music rejected the username or password.") from exc
        raise SwingError(message or f"Swing Music returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise SwingError(f"Could not reach Swing Music: {reason}") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise SwingError("Swing Music returned an invalid response or timed out.") from exc


def login(url: str, username: str, password: str) -> str:
    payload = request_json(f"{url}/auth/login", body={"username": username, "password": password})
    token = payload.get("accesstoken")
    if not token:
        raise SwingError("Swing Music login succeeded without returning an access token.")
    return str(token)


def artist_names(value) -> str:
    if not isinstance(value, list):
        return ""
    names = [str(item.get("name", "")) for item in value if isinstance(item, dict) and item.get("name")]
    return ", ".join(names)


def result_url(base: str, kind: str, item: dict, query: str) -> str:
    if kind == "track" and item.get("albumhash"):
        return f"{base}/#/albums/{urllib.parse.quote(str(item['albumhash']), safe='')}"
    if kind == "album" and item.get("albumhash"):
        return f"{base}/#/albums/{urllib.parse.quote(str(item['albumhash']), safe='')}"
    if kind == "artist" and item.get("artisthash"):
        return f"{base}/#/artists/{urllib.parse.quote(str(item['artisthash']), safe='')}"
    if kind == "playlist" and item.get("id") is not None:
        return f"{base}/#/playlist/{urllib.parse.quote(str(item['id']), safe='')}"
    return f"{base}/#/search/top?q={urllib.parse.quote(query)}"


def normalize_result(base: str, kind: str, item: dict, query: str) -> dict:
    labels = {"track": "Song", "album": "Album", "artist": "Artist", "playlist": "Playlist"}
    if kind == "track":
        title = item.get("title") or item.get("og_title") or "Untitled"
        subtitle = artist_names(item.get("artists")) or str(item.get("album") or "")
    elif kind == "album":
        title = item.get("title") or "Untitled album"
        subtitle = artist_names(item.get("albumartists"))
    elif kind == "artist":
        title = item.get("name") or "Unknown artist"
        subtitle = ""
    else:
        title = item.get("name") or "Untitled playlist"
        count = item.get("count")
        subtitle = f"{count} tracks" if isinstance(count, int) else ""
    reference = {"kind": kind}
    if kind == "track":
        reference.update({
            "trackhash": str(item.get("trackhash") or ""),
            "filepath": str(item.get("filepath") or ""),
            "image": str(item.get("image") or ""),
            "favorite": item.get("is_favorite") is True,
        })
    elif kind == "album":
        reference["albumhash"] = str(item.get("albumhash") or "")
    elif kind == "artist":
        reference["artisthash"] = str(item.get("artisthash") or "")
    else:
        reference["id"] = item.get("id")

    return {
        "type": kind,
        "typeLabel": labels[kind],
        "title": str(title),
        "subtitle": str(subtitle),
        "url": result_url(base, kind, item, query),
        "ref": reference,
    }


def search_endpoint(base: str, token: str, kind: str, query: str) -> list[dict]:
    if kind == "playlist":
        payload = request_json(f"{base}/playlists?no_images=true", token=token)
        items = payload.get("data", [])
        needle = query.casefold()
        items = [item for item in items if needle in str(item.get("name", "")).casefold()][:8]
    else:
        params = urllib.parse.urlencode({"itemtype": f"{kind}s", "start": 0, "q": query, "limit": 8})
        payload = request_json(f"{base}/search/?{params}", token=token)
        items = payload.get("results", [])
    return [normalize_result(base, kind, item, query) for item in items if isinstance(item, dict)]


def browse_endpoint(base: str, token: str, kind: str) -> list[dict]:
    """Return a useful initial shelf: recently played first, alpha fallback."""
    if kind == "favorite":
        items = request_json(
            f"{base}/favorites/tracks?start=0&limit=1000",
            token=token,
        ).get("tracks", [])
    elif kind == "track":
        recent = request_json(
            f"{base}/playlists/recentlyplayed?no_tracks=false&start=0&limit=100",
            token=token,
        ).get("tracks", [])
        fallback = request_json(
            f"{base}/playlists/recentlyadded?no_tracks=false&start=0&limit=100",
            token=token,
        ).get("tracks", [])
        key = lambda item: (str(item.get("title") or "").casefold(), str(item.get("album") or "").casefold())
        recent_keys = {(item.get("trackhash"),) for item in recent if isinstance(item, dict)}
        items = [item for item in recent if isinstance(item, dict)]
        items += sorted(
            [item for item in fallback if isinstance(item, dict) and (item.get("trackhash"),) not in recent_keys],
            key=key,
        )
    elif kind in {"album", "artist"}:
        itemtype = f"{kind}s"
        sortby = "lastplayed"
        recent = request_json(
            f"{base}/getall/{itemtype}?start=0&limit=100&sortby={sortby}&reverse=1",
            token=token,
        ).get("items", [])
        alpha = request_json(
            f"{base}/getall/{itemtype}?start=0&limit=100&sortby={'title' if kind == 'album' else 'name'}&reverse=0",
            token=token,
        ).get("items", [])
        identity = "albumhash" if kind == "album" else "artisthash"
        seen = set()
        items = []
        for item in recent + alpha:
            if not isinstance(item, dict) or item.get(identity) in seen:
                continue
            seen.add(item.get(identity))
            items.append(item)
    else:
        recent = request_json(f"{base}/nothome/recents/played?limit=100", token=token).get("items", [])
        recent_ids = [str(item.get("hash")) for item in recent if isinstance(item, dict) and item.get("type") == "playlist"]
        all_items = request_json(f"{base}/playlists?no_images=true", token=token).get("data", [])
        by_id = {str(item.get("id")): item for item in all_items if isinstance(item, dict)}
        items = [by_id[item_id] for item_id in recent_ids if item_id in by_id]
        items += sorted(
            [item for item in all_items if isinstance(item, dict) and str(item.get("id")) not in set(recent_ids)],
            key=lambda item: str(item.get("name") or "").casefold(),
        )
    limit = 1000 if kind == "favorite" else 20
    results = [normalize_result(base, "track" if kind == "favorite" else kind, item, "") for item in items[:limit] if isinstance(item, dict)]
    if kind == "favorite":
        for result in results:
            result["ref"]["kind"] = "favorite"
    return results


def cmd_status() -> None:
    config = read_config()
    emit({
        "ok": True,
        "configured": bool(config.get("url") and config.get("username")),
        "url": config.get("url", ""),
        "username": config.get("username", ""),
    })


def cmd_configure() -> None:
    try:
        payload = json.loads(sys.stdin.readline())
        url = normalize_url(str(payload.get("url", "")))
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not username or not password:
            raise SwingError("Username and password are required.")
        login(url, username, password)
        store_password(url, username, password)
        write_config({"url": url, "username": username})
        emit({"ok": True, "configured": True, "url": url, "username": username})
    except (SwingError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": str(exc) or "Invalid setup data."})


def cmd_search(query: str, category: str = "all") -> None:
    try:
        config = read_config()
        url = normalize_url(str(config.get("url", "")))
        username = str(config.get("username", ""))
        password = lookup_password(url, username)
        token = login(url, username, password)
        kinds = ("track", "album", "artist", "playlist")
        if category == "all":
            with ThreadPoolExecutor(max_workers=4) as pool:
                groups = list(pool.map(lambda kind: search_endpoint(url, token, kind, query), kinds))
        elif category in kinds:
            groups = [search_endpoint(url, token, category, query)]
        else:
            raise SwingError("Unknown search category.")
        results = [item for group in groups for item in group]
        emit({"ok": True, "results": results})
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})
    except Exception as exc:
        emit({"ok": False, "error": f"Search failed: {exc}"})


def cmd_browse(category: str = "all") -> None:
    try:
        config = read_config()
        url = normalize_url(str(config.get("url", "")))
        username = str(config.get("username", ""))
        password = lookup_password(url, username)
        token = login(url, username, password)
        kinds = ("track", "album", "artist", "playlist")
        if category == "all":
            results = []
            for kind in kinds:
                results.extend(browse_endpoint(url, token, kind)[:5])
        elif category == "favorite":
            results = browse_endpoint(url, token, category)
        elif category in kinds:
            results = browse_endpoint(url, token, category)
        else:
            raise SwingError("Unknown browse category.")
        emit({"ok": True, "results": results})
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})
    except Exception as exc:
        emit({"ok": False, "error": f"Could not load library: {exc}"})


def tracks_for_reference(base: str, token: str, reference: dict) -> list[dict]:
    kind = str(reference.get("kind", ""))
    if kind == "track":
        tracks = [reference]
    elif kind == "album":
        albumhash = urllib.parse.quote(str(reference.get("albumhash", "")), safe="")
        tracks = request_json(f"{base}/album/{albumhash}/tracks?classical_view=false", token=token)
    elif kind == "artist":
        artisthash = urllib.parse.quote(str(reference.get("artisthash", "")), safe="")
        tracks = request_json(f"{base}/artist/{artisthash}/tracks", token=token)
    elif kind == "favorite":
        tracks = request_json(
            f"{base}/favorites/tracks?start=0&limit={MAX_PLAYBACK_TRACKS}",
            token=token,
        ).get("tracks", [])
        selected_hash = str(reference.get("trackhash", ""))
        selected_index = next(
            (index for index, track in enumerate(tracks) if isinstance(track, dict) and str(track.get("trackhash", "")) == selected_hash),
            None,
        )
        if selected_index is not None:
            tracks = tracks[selected_index:] + tracks[:selected_index]
    elif kind == "playlist":
        playlist_id = urllib.parse.quote(str(reference.get("id", "")), safe="")
        payload = request_json(
            f"{base}/playlists/{playlist_id}?no_tracks=false&start=0&limit={MAX_PLAYBACK_TRACKS}",
            token=token,
        )
        info = payload.get("info", {})
        if isinstance(info, dict) and int(info.get("count", 0) or 0) > MAX_PLAYBACK_TRACKS:
            raise SwingError(f"This playlist has more than {MAX_PLAYBACK_TRACKS} tracks and cannot be played here.")
        tracks = payload.get("tracks", [])
    else:
        raise SwingError("Unsupported playback result.")

    if not isinstance(tracks, list):
        raise SwingError("Swing Music returned an invalid track list.")
    if len(tracks) > MAX_PLAYBACK_TRACKS:
        raise SwingError(f"Playback is limited to {MAX_PLAYBACK_TRACKS} tracks.")
    playable = [
        track for track in tracks
        if isinstance(track, dict) and track.get("trackhash") and track.get("filepath")
    ]
    if not playable:
        raise SwingError("No playable tracks were returned.")
    return playable


def stream_url(base: str, track: dict) -> str:
    trackhash = urllib.parse.quote(str(track["trackhash"]), safe="")
    query = urllib.parse.urlencode({
        "filepath": str(track["filepath"]),
        "container": "mp3",
        "quality": "original",
    })
    return f"{base}/file/{trackhash}/legacy?{query}"


def artwork_url(base: str, track: dict) -> str:
    image = str(track.get("image") or "").strip()
    if not image:
        return ""
    parsed = urllib.parse.urlsplit(image)
    filename = urllib.parse.quote(parsed.path.lstrip("/"), safe="")
    query = urllib.parse.urlencode(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    suffix = f"?{query}" if query else ""
    return f"{base}/img/thumbnail/medium/{filename}{suffix}"


def stop_player() -> None:
    try:
        pid = int(PLAYER_PID_FILE.read_text(encoding="utf-8").strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        if str(Path(__file__).resolve()) in cmdline and "_player" in cmdline:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        pass
    PLAYER_PID_FILE.unlink(missing_ok=True)
    if RUNTIME_DIR.exists():
        for pattern in ("player-*.conf", "player-*.m3u", "player.sock", "player.json"):
            for path in RUNTIME_DIR.glob(pattern):
                path.unlink(missing_ok=True)


def write_private_temp(suffix: str, content: str) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUNTIME_DIR, 0o700)
    descriptor, path = tempfile.mkstemp(prefix="player-", suffix=suffix, dir=RUNTIME_DIR)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return Path(path)


def cmd_play() -> None:
    config_path = None
    playlist_path = None
    try:
        item = json.loads(sys.stdin.readline())
        reference = item.get("ref", {}) if isinstance(item, dict) else {}
        config = read_config()
        base = normalize_url(str(config.get("url", "")))
        username = str(config.get("username", ""))
        password = lookup_password(base, username)
        token = login(base, username, password)
        tracks = tracks_for_reference(base, token, reference)

        playlist_lines = ["#EXTM3U"]
        for track in tracks:
            title = str(track.get("title") or track.get("og_title") or "Swing Music").replace("\n", " ")
            playlist_lines.extend([f"#EXTINF:-1,{title}", stream_url(base, track)])
        playlist_text = "\n".join(playlist_lines) + "\n"
        if len(playlist_text.encode("utf-8")) > MAX_PLAYLIST_BYTES:
            raise SwingError("The playback playlist is too large.")
        state_text = json.dumps({
            "base": base,
            "tracks": [{
                "title": str(track.get("title") or track.get("og_title") or "Swing Music"),
                "trackhash": str(track.get("trackhash") or ""),
                "favorite": track.get("is_favorite") is True or track.get("favorite") is True,
                "artwork": artwork_url(base, track),
            } for track in tracks],
        }, ensure_ascii=False, separators=(",", ":"))
        if len(state_text.encode("utf-8")) > MAX_STATE_BYTES:
            raise SwingError("The playback metadata is too large.")

        stop_player()
        config_path = write_private_temp(
            ".conf",
            "http-header-fields=Authorization: Bearer " + token + "\n",
        )
        playlist_path = write_private_temp(".m3u", playlist_text)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        PLAYER_STATE_FILE.write_text(state_text, encoding="utf-8")
        os.chmod(PLAYER_STATE_FILE, 0o600)

        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_player", str(config_path), str(playlist_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        PLAYER_PID_FILE.write_text(str(process.pid), encoding="utf-8")
        os.chmod(PLAYER_PID_FILE, 0o600)
        emit({"ok": True, "tracks": len(tracks)})
    except (SwingError, json.JSONDecodeError) as exc:
        for path in (config_path, playlist_path):
            if path:
                path.unlink(missing_ok=True)
        emit({"ok": False, "error": str(exc) or "Invalid playback request."})
    except Exception as exc:
        for path in (config_path, playlist_path):
            if path:
                path.unlink(missing_ok=True)
        emit({"ok": False, "error": f"Could not start playback: {exc}"})


def mpv_commands(commands: list[dict]) -> dict:
    if not PLAYER_SOCKET.exists():
        raise SwingError("Playback is not active.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            connection.connect(str(PLAYER_SOCKET))
            for command in commands:
                connection.sendall((json.dumps(command) + "\n").encode("utf-8"))
            responses = {}
            while len(responses) < len(commands):
                line = connection.recv(8192)
                if not line:
                    break
                for raw in line.splitlines():
                    try:
                        response = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if response.get("request_id"):
                        responses[response["request_id"]] = response
            return responses
    except (OSError, TimeoutError) as exc:
        raise SwingError("Playback is not responding.") from exc


def cmd_favorite(trackhash: str, action: str) -> None:
    try:
        if action not in {"add", "remove"}:
            raise SwingError("Unknown favorite action.")
        config = read_config()
        base = normalize_url(str(config.get("url", "")))
        username = str(config.get("username", ""))
        password = lookup_password(base, username)
        token = login(base, username, password)
        request_json(
            f"{base}/favorites/{action}",
            token=token,
            body={"hash": trackhash, "type": "track"},
        )
        emit({"ok": True, "favorite": action == "add"})
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})


def cmd_favorite_status(trackhash: str) -> None:
    try:
        config = read_config()
        base = normalize_url(str(config.get("url", "")))
        username = str(config.get("username", ""))
        password = lookup_password(base, username)
        token = login(base, username, password)
        data = request_json(
            f"{base}/favorites/check?hash={urllib.parse.quote(trackhash, safe='')}&type=track",
            token=token,
        )
        emit({
            "ok": True,
            "trackhash": trackhash,
            "favorite": data.get("is_favorite") is True,
        })
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})


def cmd_signout() -> None:
    try:
        config = read_config()
        url = str(config.get("url", ""))
        username = str(config.get("username", ""))
        if url and username:
            keyring("clear", "service", APP_ID, "url", url, "username", username)
        CONFIG_FILE.unlink(missing_ok=True)
        stop_player()
        emit({"ok": True})
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})


def cmd_player_status() -> None:
    try:
        pid = int(PLAYER_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        emit({"ok": True, "active": False})
        return
    try:
        responses = mpv_commands([
            {"command": ["get_property", "pause"], "request_id": "pause"},
            {"command": ["get_property", "media-title"], "request_id": "title"},
            {"command": ["get_property", "playlist-pos"], "request_id": "index"},
            {"command": ["get_property", "playlist-count"], "request_id": "count"},
            {"command": ["get_property", "volume"], "request_id": "volume"},
        ])
        state = {}
        try:
            state = json.loads(PLAYER_STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        index = int(responses.get("index", {}).get("data", 0) or 0)
        state_tracks = state.get("tracks", []) if isinstance(state, dict) else []
        current = state_tracks[index] if 0 <= index < len(state_tracks) and isinstance(state_tracks[index], dict) else {}
        emit({
            "ok": True,
            "active": True,
            "paused": bool(responses.get("pause", {}).get("data", False)),
            "title": str(responses.get("title", {}).get("data", "")),
            "index": index,
            "count": int(responses.get("count", {}).get("data", 0) or 0),
            "volume": max(0, min(100, float(responses.get("volume", {}).get("data", 100) or 0))),
            "trackhash": str(current.get("trackhash", "")),
            "favorite": current.get("favorite") is True,
            "artwork": str(current.get("artwork", "")),
        })
    except (SwingError, ValueError, TypeError):
        emit({"ok": True, "active": False})


def cmd_player_control(action: str, value: str | None = None) -> None:
    commands = {
        "pause": ["cycle", "pause"],
        "previous": ["playlist-prev", "weak"],
        "next": ["playlist-next", "weak"],
    }
    try:
        if action == "volume":
            try:
                volume = max(0, min(100, float(value or "")))
            except ValueError as exc:
                raise SwingError("Invalid volume.") from exc
            commands[action] = ["set_property", "volume", volume]
        elif action not in commands:
            raise SwingError("Unknown playback control.")
        mpv_commands([{"command": commands[action], "request_id": "control"}])
        emit({"ok": True})
    except SwingError as exc:
        emit({"ok": False, "error": str(exc)})


def run_player(config_path: str, playlist_path: str) -> None:
    config_file = Path(config_path)
    playlist_file = Path(playlist_path)
    try:
        subprocess.run(
            [
                "mpv", "--no-video", "--force-window=no", "--really-quiet",
                f"--input-ipc-server={PLAYER_SOCKET}",
                f"--include={config_file}", f"--playlist={playlist_file}",
            ],
            stdin=subprocess.DEVNULL,
            check=False,
        )
    finally:
        config_file.unlink(missing_ok=True)
        playlist_file.unlink(missing_ok=True)
        PLAYER_SOCKET.unlink(missing_ok=True)
        PLAYER_STATE_FILE.unlink(missing_ok=True)
        try:
            if PLAYER_PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PLAYER_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "status":
        cmd_status()
    elif command == "configure":
        cmd_configure()
    elif command == "search" and len(sys.argv) > 2:
        cmd_search(sys.argv[2].strip(), sys.argv[3] if len(sys.argv) > 3 else "all")
    elif command == "browse":
        cmd_browse(sys.argv[2] if len(sys.argv) > 2 else "all")
    elif command == "play":
        cmd_play()
    elif command == "player-status":
        cmd_player_status()
    elif command == "favorite" and len(sys.argv) == 4:
        cmd_favorite(sys.argv[2], sys.argv[3])
    elif command == "favorite-status" and len(sys.argv) == 3:
        cmd_favorite_status(sys.argv[2])
    elif command == "signout" and len(sys.argv) == 2:
        cmd_signout()
    elif command == "control" and len(sys.argv) in {3, 4}:
        cmd_player_control(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "stop":
        stop_player()
        emit({"ok": True})
    elif command == "_player" and len(sys.argv) == 4:
        run_player(sys.argv[2], sys.argv[3])
    else:
        emit({"ok": False, "error": "Usage: swing_client.py status|configure|search QUERY [CATEGORY]|browse [CATEGORY]|play|player-status|control ACTION|stop"})
        raise SystemExit(2)


if __name__ == "__main__":
    main()
