#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import spotipy
import tidalapi
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
# Load .env from the same folder as this script
load_dotenv(ENV_FILE, override=True)

STATE_FILE = BASE_DIR / "transfer_state.json"
UNMATCHED_FILE = BASE_DIR / "unmatched.csv"
TIDAL_SESSION_FILE = BASE_DIR / "tidal_session.json"
SCOPES = "user-library-read user-follow-read playlist-read-private playlist-read-collaborative"
WORKERS = 6  # parallel Tidal lookups; need to lower this (e.g. 3) if we ever see rate-limit errors
REDIRECT_URI = "http://127.0.0.1:8888/callback" 


# ---------- helpers ----------
def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          
    s = re.sub(r"\s-\s.*$", "", s)                   
    return re.sub(r"[^a-z0-9]+", "", s)


def retry(fn, *args, tries=5, **kwargs):
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # network hiccups / rate limits
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def load_state():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for k in ("tracks", "albums", "artists", "playlists"):
        state.setdefault(k, [])
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ---------- Spotify ----------
def env_help(var=None):
    msg = f"Missing {var} - " if var else ""
    msg += f"expected a file named exactly '.env' at: {ENV_FILE}"
    if not ENV_FILE.exists():
        for wrong in (".env.txt", ".env.example", "env", "env.txt"):
            if (BASE_DIR / wrong).exists():
                msg += f"\nFound '{wrong}' instead - rename it to '.env' (turn on 'File name extensions' in Explorer)."
                break
        else:
            msg += "\nThe file doesn't exist yet. Create it with SPOTIPY_CLIENT_ID=... and SPOTIPY_CLIENT_SECRET=... lines."
    else:
        msg += "\nThe file exists, but that variable isn't set in it (format: NAME=value, no quotes or spaces)."
    return msg


def spotify_client():
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET"):
        if not os.environ.get(var):
            sys.exit(env_help(var))
    print(f"Spotify redirect URI in use: {REDIRECT_URI}")
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope=SCOPES, redirect_uri=REDIRECT_URI, open_browser=True,
        cache_path=str(BASE_DIR / ".spotify_cache")))


def paged(sp, first_page):
    page = first_page
    while page:
        yield from page["items"]
        page = retry(sp.next, page) if page.get("next") else None


def spotify_tracks(sp):
    items = list(paged(sp, retry(sp.current_user_saved_tracks, limit=50)))
    return [i["track"] for i in reversed(items) if i.get("track")]  # oldest first


def spotify_albums(sp):
    items = list(paged(sp, retry(sp.current_user_saved_albums, limit=50)))
    return [i["album"] for i in reversed(items)]


def spotify_artists(sp):
    out, after = [], None
    while True:
        res = retry(sp.current_user_followed_artists, limit=50, after=after)["artists"]
        out += res["items"]
        after = res.get("cursors", {}).get("after")
        if not after or not res["items"]:
            break
    return out


def spotify_playlists(sp):
    items = paged(sp, retry(sp.current_user_playlists, limit=50))
    return [p for p in items if p and p.get("id")]


def playlist_tracks(sp, pid):
    """All real (non-local, non-podcast) tracks of a Spotify playlist, in order."""
    out = []
    try:
        page = retry(sp.playlist_items, pid, limit=100, additional_types=("track",))
    except Exception:
        page = retry(sp._get, f"playlists/{pid}/items", limit=100)
    while page:
        for it in page["items"]:
            tr = it.get("track") or it.get("item")  # Spotify renamed this field in newer API versions
            if tr and tr.get("id") and not tr.get("is_local") and tr.get("type", "track") == "track":
                out.append(tr)
        page = retry(sp.next, page) if page.get("next") else None
    return out


# ---------- Tidal ----------
def tidal_client():
    session = tidalapi.Session()
    if hasattr(session, "login_session_file"):
        session.login_session_file(TIDAL_SESSION_FILE)   # opens a link once, then caches
    else:
        session.login_oauth_simple()
    if not session.check_login():
        sys.exit("Tidal login failed.")
    return session


def match_track(tidal, t):
    isrc = (t.get("external_ids") or {}).get("isrc")
    if isrc and hasattr(tidal, "get_tracks_by_isrc"):
        try:
            res = retry(tidal.get_tracks_by_isrc, isrc)
            if res:
                return res[0].id
        except Exception:
            pass
    artist = t["artists"][0]["name"]
    res = retry(tidal.search, f"{t['name']} {artist}", models=[tidalapi.media.Track], limit=10)
    for cand in res.get("tracks", []):
        same_title = norm(cand.name) == norm(t["name"])
        same_artist = norm(cand.artist.name) == norm(artist)
        close_len = abs((cand.duration or 0) - t["duration_ms"] / 1000) <= 5
        if same_title and same_artist and close_len:
            return cand.id
    return None


def match_album(tidal, a):
    upc = (a.get("external_ids") or {}).get("upc")
    if upc and hasattr(tidal, "get_albums_by_barcode"):
        try:
            res = retry(tidal.get_albums_by_barcode, upc)
            if res:
                return res[0].id
        except Exception:
            pass
    artist = a["artists"][0]["name"]
    res = retry(tidal.search, f"{a['name']} {artist}", models=[tidalapi.album.Album], limit=10)
    for cand in res.get("albums", []):
        if norm(cand.name) == norm(a["name"]) and norm(cand.artist.name) == norm(artist):
            return cand.id
    return None


def match_artist(tidal, a):
    res = retry(tidal.search, a["name"], models=[tidalapi.artist.Artist], limit=10)
    for cand in res.get("artists", []):
        if norm(cand.name) == norm(a["name"]):
            return cand.id
    return None


def pipelined(items, matcher, tidal):
    """Look items up on Tidal in parallel, but yield (item, tidal_id, error) in the ORIGINAL order.
    The caller adds them one by one, so the add order (and Tidal's 'date added') stays exact."""
    def work(item):
        try:
            return item, matcher(tidal, item), None
        except Exception as e:
            return item, None, e

    ex = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        yield from ex.map(work, items)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


_TRACK_CACHE = {}


def tidal_playlists_by_name(tidal):
    try:
        return {p.name: p for p in tidal.user.playlists() if hasattr(p, "add")}
    except Exception:
        return {}


def transfer_playlist(sp, tidal, pl, existing, dry_run):
    """Copy one Spotify playlist to a Tidal playlist of the same name.
    Returns (matched_count, total_count, missing[(type, label, url)])."""
    tracks = playlist_tracks(sp, pl["id"])
    ids, missing = [], []
    todo = list({t["id"]: t for t in tracks if t["id"] not in _TRACK_CACHE}.values())

    def find(t):
        try:
            return t["id"], match_track(tidal, t)
        except Exception:
            return t["id"], None

    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for sid, tid in ex.map(find, todo):
                _TRACK_CACHE[sid] = tid
    for t in tracks:
        tid = _TRACK_CACHE.get(t["id"])
        if tid:
            ids.append(str(tid))
        else:
            missing.append((f"playlist: {pl['name']}", describe("tracks", t),
                            t.get("external_urls", {}).get("spotify", "")))
    if not dry_run and ids:
        tp = existing.get(pl["name"])  # reuse a same-named playlist so reruns do not duplicate
        if tp is None:
            tp = retry(tidal.user.create_playlist, pl["name"], pl.get("description") or "")
            existing[pl["name"]] = tp
        for i in range(0, len(ids), 100):
            retry(tp.add, ids[i:i + 100])
            time.sleep(0.1)
    return len(ids), len(tracks), missing


# ---------- main ----------
def describe(kind, item):
    if kind == "tracks":
        return f"{item['artists'][0]['name']} - {item['name']}"
    if kind == "albums":
        return f"{item['artists'][0]['name']} - {item['name']}"
    return item["name"]


def run_kind(kind, items, matcher, adder, tidal, state, dry_run, unmatched):
    done = set(state[kind])
    todo = [i for i in items if i["id"] not in done]
    print(f"\n{kind}: {len(items)} on Spotify, {len(todo)} to transfer")
    added = 0
    with closing(pipelined(todo, matcher, tidal)) as results:  # parallel lookups, ordered adds
        for n, (item, tid, err) in enumerate(results, 1):
            label = describe(kind, item)
            url = item.get("external_urls", {}).get("spotify", "")
            if err is not None:
                print(f"  [{n}/{len(todo)}] ERROR matching {label}: {err}")
                unmatched.append((kind, label, url))
                continue
            if tid is None:
                print(f"  [{n}/{len(todo)}] no match: {label}")
                unmatched.append((kind, label, url))
                continue
            if not dry_run:
                try:
                    retry(adder, tid)
                except Exception as e:
                    print(f"  [{n}/{len(todo)}] ERROR adding {label}: {e}")
                    unmatched.append((kind, label, url))
                    continue
                state[kind].append(item["id"])
                if n % 25 == 0:
                    save_state(state)
            added += 1
            print(f"  [{n}/{len(todo)}] ok: {label}")
    if not dry_run:
        save_state(state)
    print(f"{kind}: {added} matched{'' if dry_run else ' and added'}, {len(todo) - added} unmatched")


def main():
    p = argparse.ArgumentParser(description="Transfer Spotify library to Tidal")
    p.add_argument("--only", action="append", choices=["tracks", "albums", "artists", "playlists"])
    p.add_argument("--dry-run", action="store_true", help="match only, add nothing")
    args = p.parse_args()
    kinds = args.only or ["tracks", "albums", "artists", "playlists"]

    print("Logging in to Spotify...")
    sp = spotify_client()
    print("Logging in to Tidal...")
    tidal = tidal_client()
    fav = tidal.user.favorites
    state = load_state()
    unmatched = []

    if "tracks" in kinds:
        run_kind("tracks", spotify_tracks(sp), match_track, fav.add_track, tidal, state, args.dry_run, unmatched)
    if "albums" in kinds:
        run_kind("albums", spotify_albums(sp), match_album, fav.add_album, tidal, state, args.dry_run, unmatched)
    if "artists" in kinds:
        run_kind("artists", spotify_artists(sp), match_artist, fav.add_artist, tidal, state, args.dry_run, unmatched)

    if "playlists" in kinds:
        existing = tidal_playlists_by_name(tidal)
        done = set(state["playlists"])
        pls = [p for p in spotify_playlists(sp) if p["id"] not in done]
        print(f"\nplaylists: {len(pls)} to transfer")
        for n, pl in enumerate(pls, 1):
            try:
                ok, total, missing = transfer_playlist(sp, tidal, pl, existing, args.dry_run)
            except Exception as e:
                print(f"  [{n}/{len(pls)}] ERROR {pl['name']}: {e}")
                continue
            unmatched.extend(missing)
            if not args.dry_run:
                state["playlists"].append(pl["id"])
                save_state(state)
            print(f"  [{n}/{len(pls)}] {pl['name']}: {ok}/{total} tracks matched")

    if unmatched:
        with UNMATCHED_FILE.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["type", "name", "spotify_url"])
            w.writerows(unmatched)
        print(f"\n{len(unmatched)} items couldn't be transferred -> {UNMATCHED_FILE}")
    print("\nDone.")


if __name__ == "__main__":
    main()
