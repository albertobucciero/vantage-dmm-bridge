#!/usr/bin/env python3
"""Vantage DMM bridge: makes a LinkPlay player (tested with an Arylic Up2Stream Amp V4) look
like a Vantage DMM (Digital Media Manager) to a Vantage InFusion controller and to the
Equinox app, with the Music Assistant library as the DMM library.

Ports (same as a real Vantage DMM):
  30535  DMM command port (text lines)  - Vantage controller driver and Equinox app
  30521  DMM serial-over-IP port        - controller driver (accepted, ignored)
  30545  DMM HTTP port (cover art)      - Equinox app

Protocol, as observed between the Vantage DMM driver, the Equinox app and this bridge:
  R:<CMD> <zone> <value...>       reply
  E:<CMD> <field> <value> <zone>  list element / media field
  S:<EVENT> <value> <zone>        unsolicited status, pushed to clients that sent STATUS ...
Lists: "GETROWS <QUERY> <start> <count>", rows numbered from 1; the closing
"R:GETROWS ..." line carries one more number for sub-lists (QUERYSONGSBY*, QUERYALBUMSBY*).

Zone 1 is the LinkPlay player; the other zones exist (a Vantage DMM has 4 sources) but are idle.
The presets of the player (UPnP PlayQueue GetKeyMapping) are shown in the app's "vtuner" tab.
The player must answer the LinkPlay HTTP API on port 80 (devices that only accept HTTPS,\nsuch as recent WiiM firmware, are not supported).\nNot affiliated with or endorsed by Vantage / Legrand.
"""
import asyncio
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime

OPTIONS = "/data/options.json"
opt = {}
if os.path.exists(OPTIONS):
    with open(OPTIONS, encoding="utf-8") as f:
        opt = json.load(f)
PLAYER = opt.get("player_host", "")                    # IP address of the LinkPlay player
ZONES = opt.get("zone_names") or ["Network", "Zone 2", "Zone 3", "Zone 4"]
MA_PLAYER = opt.get("ma_player", "")                   # HA entity of the player in Music Assistant
MA_URL = opt.get("ma_url") or "http://127.0.0.1:8095"  # Music Assistant server
MA_TOKEN = opt.get("ma_token", "")                     # Music Assistant long-lived token
DIAGNOSTICS = bool(opt.get("diagnostics", False))      # http://<host>:30545/diag

SUBLISTS = {"QUERYSONGSBYALBUM", "QUERYSONGSBYARTIST", "QUERYSONGSBYGENRE",
            "QUERYSONGSBYPLAYLIST", "QUERYALBUMSBYARTIST", "QUERYALBUMSBYGENRE"}


LOG_TAIL = []


def log(msg):
    line = datetime.now().strftime("%H:%M:%S.%f")[:-3] + " " + msg
    print(line, flush=True)
    LOG_TAIL.append(line)
    del LOG_TAIL[:-400]


# ---------------------------------------------------------------- Home Assistant API
# (homeassistant_api: true in config.yaml -> SUPERVISOR_TOKEN, no user token needed)
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def ha(method, path, body=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://supervisor/core/api" + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + HA_TOKEN,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def ha_service(domain, service, data, response=True):
    return ha("POST", "/services/%s/%s%s" % (domain, service, "?return_response" if response else ""), data)


def ma_api(command, args=None, timeout=20):
    """Music Assistant server API (POST /api), authenticated with the token from the options."""
    body = json.dumps({"command": command, "args": args or {}}).encode("utf-8")
    req = urllib.request.Request(MA_URL + "/api", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + MA_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def show(s):
    return "".join(c if 32 <= ord(c) < 127 else "<%02X>" % ord(c) for c in s)


def q(s):
    return '"' + (s or "").replace('"', "'") + '"'


# ---------------------------------------------------------------- LinkPlay player access
def http(url, soap_action=None, body=None, timeout=4):
    data = None
    headers = {}
    if body is not None:
        data = body.encode("utf-8")
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": soap_action}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tag(xml, name):
    m = re.search(r"<" + re.escape(name) + r"[^>]*>(.*?)</" + re.escape(name) + r">", xml, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def from_hex(h):
    try:
        t = bytes.fromhex(h).decode("utf-8", "replace")
    except ValueError:
        return h
    return "" if t == "Unknown" else t


class Media:
    def __init__(self):
        self.title = self.artist = self.album = self.art = ""
        self.state = "STOPPED"
        self.pos = self.length = self.vol = 0
        self.loop = 4
        self.mute = False
        self.album_id = 1
        self.mode = 0          # LinkPlay mode: 1 AirPlay, 2 DLNA (= Music Assistant stream), 31 Spotify...


cur = Media()


def soap(path, service, action, args=""):
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s>'
            '</s:Body></s:Envelope>' % (action, service, args, action))
    return http("http://%s:49152%s" % (PLAYER, path), '"%s#%s"' % (service, action), body).decode("utf-8", "replace")


def read_status():
    j = json.loads(http("http://%s/httpapi.asp?command=getPlayerStatus" % PLAYER).decode("utf-8", "replace"))
    m = Media()
    m.title, m.artist, m.album = (from_hex(j.get(k, "")) for k in ("Title", "Artist", "Album"))
    st = j.get("status", "")
    m.state = {"play": "PLAYING", "pause": "PAUSED"}.get(st, cur.state if st == "load" else "STOPPED")
    for attr, key in (("pos", "curpos"), ("length", "totlen"), ("vol", "vol"), ("loop", "loop")):
        try:
            setattr(m, attr, int(j.get(key, "0")))
        except ValueError:
            pass
    m.mute = j.get("mute") == "1"
    try:
        m.mode = int(j.get("mode", "0"))
    except ValueError:
        pass
    return m


def from_ma(m):
    """True when the player is playing a stream sent by Music Assistant (DLNA/AirPlay):
    then the queue, next/previous, shuffle and repeat belong to the MA player."""
    return m.mode in (1, 2) and bool(HA_TOKEN) and bool(MA_PLAYER)


def ma_cmd(service, data=None):
    try:
        d = {"entity_id": MA_PLAYER}
        d.update(data or {})
        ha_service("media_player", service, d, response=False)
        log("MA <- %s %s" % (service, data or ""))
    except Exception as e:  # noqa: BLE001
        log("MA command error (%s): %s" % (service, e))


def ma_loop(m):
    """Shuffle/repeat of the MA player, as a LinkPlay loop code (see shuffle()/repeat())."""
    try:
        a = ha("GET", "/states/" + MA_PLAYER).get("attributes", {})
        sh, rp = bool(a.get("shuffle")), a.get("repeat", "off") != "off"
        m.loop = (2 if rp else 3) if sh else (0 if rp else 4)
    except Exception as e:  # noqa: BLE001
        log("MA state error: %s" % e)


def position_info(m):
    """Cover URL (and album/duration if missing) from the UPnP AVTransport."""
    try:
        x = soap("/upnp/control/rendertransport1", "urn:schemas-upnp-org:service:AVTransport:1",
                 "GetPositionInfo", "<InstanceID>0</InstanceID>")
        meta = tag(x, "TrackMetaData")
        m.art = tag(meta, "upnp:albumArtURI")
        if not m.album:
            m.album = tag(meta, "upnp:album")
        if not m.length:
            d = tag(x, "TrackDuration")
            mm = re.match(r"(\d+):(\d+):(\d+)", d)
            if mm:
                m.length = (int(mm[1]) * 3600 + int(mm[2]) * 60 + int(mm[3])) * 1000
    except Exception as e:  # noqa: BLE001
        log("UPnP error: %s" % e)


def linkplay(cmd):
    try:
        http("http://%s/httpapi.asp?command=%s" % (PLAYER, cmd))
        log("PLAYER <- " + cmd)
    except Exception as e:  # noqa: BLE001
        log("player error: %s" % e)


def shuffle(loop):
    return loop in (2, 3)


def repeat(loop):
    return loop in (0, 1, 2)


def set_loop(sh, rp):
    linkplay("setPlayerCmd:loopmode:%d" % ((2 if rp else 3) if sh else (0 if rp else 4)))


# ---------------------------------------------------------------- library
# Library = Music Assistant, read through its own API (POST /api with the token of the
# options): playlists (+ the player presets), albums, artists, tracks, genres, the
# tracks of an album/playlist, the albums of an artist/genre and the real queue.
# Playback: player_queues/play_media on the MA player of the LinkPlay device, "replace" when
# the app cleared the queue just before, otherwise "add".
#
# Numeric ids handed to the app (it sends them back in QUEUE/QUEUEBY commands):
#   100000 + i  playlist entry i     200000 + i  library album i
#   300000 + i  library artist i     400000 + i  library track i
#   500000 + i  genre i              600000 + n  any other item (registry)
ID_PLAYLIST, ID_ALBUM, ID_ARTIST, ID_TRACK, ID_GENRE, ID_DYN = 100000, 200000, 300000, 400000, 500000, 600000
ID_RADIO = 90000      # 90000 + key: player preset <key>, shown in the app's "vtuner" tab
RADIO_QUERIES = ("RADIOQUERYPRESETS", "RADIOQUERYLASTPLAYED", "RADIOQUERYMOSTPLAYED", "RADIOQUERYCHILDNODES")
PLURAL = {"playlist": "playlists", "album": "albums", "artist": "artists", "track": "tracks", "genre": "genres"}

presets = []          # list of (key, name, pic)
presets_read = 0.0
lib_cache = {}        # media_type -> (time, items)
child_cache = {}      # (kind, uri) -> (time, items)
registry = {}         # dyn id -> item (+ "parent": uri of the album/playlist it belongs to)
reg_by_key = {}
next_dyn = ID_DYN
queue_id = None
last_parent = None    # {"kind", "item", "children"} of the last sub-list asked by the app
clear_pending = 0.0   # time of the last CLEARQUEUE: the next queue command replaces the queue


def get_presets():
    global presets, presets_read
    if time.time() - presets_read < 60:
        return presets
    try:
        x = soap("/upnp/control/PlayQueue1", "urn:schemas-wiimu-com:service:PlayQueue:1", "GetKeyMapping")
        ctx = tag(x, "QueueContext")
        found = []
        for k in range(1, 22):
            m = re.search(r"<Key%d>(.*?)</Key%d>" % (k, k), ctx, re.S)
            if m:
                name = tag(m.group(1), "Name")
                if name:
                    found.append((k, name, tag(m.group(1), "PicUrl")))
        presets, presets_read = found, time.time()
        log("presets: %d" % len(found))
    except Exception as e:  # noqa: BLE001
        log("presets error: %s" % e)
    return presets


def ma_image_base():
    m = re.match(r"(https?://[^:/]+)", MA_URL)
    return (m.group(1) if m else "http://127.0.0.1") + ":8097"


def norm(it):
    """A Music Assistant item reduced to what the bridge needs."""
    if not isinstance(it, dict):
        return None
    media = it.get("media_item") if it.get("media_item") else it      # queue items wrap the track
    imgs = ((media.get("metadata") or {}).get("images") or []) or ((it.get("image") or {}) and [it.get("image")])
    if not imgs and isinstance(media.get("album"), dict):
        imgs = (media["album"].get("metadata") or {}).get("images") or []
    img = ""
    if imgs and isinstance(imgs[0], dict):
        pid = imgs[0].get("proxy_id")
        # The MA image proxy accepts only some sizes (256 and 512 work, 300 gives 400 Bad Request)
        img = "%s/imageproxy/%s?size=256&fmt=jpeg" % (ma_image_base(), pid) if pid else imgs[0].get("path", "")
    name = media.get("name") or it.get("name") or ""
    if media.get("version"):
        name += " (%s)" % media["version"]
    return {
        "uri": media.get("uri") or "",
        "media_type": media.get("media_type") or "",
        "item_id": media.get("item_id"),
        "provider": media.get("provider"),
        "name": name,
        "sort": media.get("sort_name") or name.lower(),
        "artist": ", ".join(a.get("name", "") for a in (media.get("artists") or []) if isinstance(a, dict)),
        "album": (media.get("album") or {}).get("name", "") if isinstance(media.get("album"), dict) else "",
        "image": img,
        "duration": int(media.get("duration") or it.get("duration") or 0),
        "queue_item_id": it.get("queue_item_id"),
    }


def ma_list(command, args):
    """A list command of the MA API, normalized; [] on error."""
    if not MA_TOKEN:
        return []
    try:
        r = ma_api(command, args)
        if isinstance(r, dict):
            r = r.get("items") or r.get("result") or []
        return [x for x in (norm(i) for i in (r or [])) if x and x["uri"]]
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if hasattr(e, "read"):
            try:
                msg += " " + e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
        log("MA API error %s: %s" % (command, msg))
        return []


def lib(media_type):
    """Whole MA library of one type, cached for 10 minutes."""
    hit = lib_cache.get(media_type)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    items, offset = [], 0
    while True:
        args = {"limit": 500, "offset": offset, "order_by": "sort_name"}
        if media_type == "genre":
            args["hide_empty"] = True
        page = ma_list("music/%s/library_items" % PLURAL[media_type], args)
        items += page
        if len(page) < 500 or offset > 20000:
            break
        offset += 500
    if items or not hit:
        lib_cache[media_type] = (time.time(), items)
        log("MA %s: %d" % (media_type, len(items)))
    return lib_cache[media_type][1]


def children(kind, item):
    """Contents of a sub-list (cached 5 minutes): tracks of an album/playlist, albums of an artist/genre."""
    key = (kind, item["uri"])
    hit = child_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    ref = {"item_id": item["item_id"], "provider_instance_id_or_domain": item["provider"]}
    if kind == "album":
        got = ma_list("music/albums/album_tracks", ref)
    elif kind == "playlist":
        got = ma_list("music/playlists/playlist_tracks", ref)
    elif kind == "artist_albums":
        got = ma_list("music/artists/artist_albums", ref)
    elif kind == "artist_tracks":
        got = ma_list("music/artists/artist_tracks", ref)
    elif kind == "genre_albums":
        got = ma_list("music/genres/albums", {"item_id": item["item_id"], "limit": 500})
    elif kind == "genre_tracks":
        got = ma_list("music/genres/tracks", {"item_id": item["item_id"], "limit": 500})
    else:
        got = []
    child_cache[key] = (time.time(), got)
    while len(child_cache) > 200:
        del child_cache[next(iter(child_cache))]
    return got


def reg(item, parent=None):
    """Numeric id for an item that is not in a library list (track of an album, ...)."""
    global next_dyn
    key = (item["uri"], parent or "")
    if key not in reg_by_key:
        registry[next_dyn] = dict(item, parent=parent)
        reg_by_key[key] = next_dyn
        next_dyn += 1
    return reg_by_key[key]


def playlists():
    """Entries: (name, kind, item_or_key, image); kind 'ma' (MA item) or 'preset' (player preset key)."""
    out = [(it["name"], "ma", it, it["image"]) for it in lib("playlist")]
    return out          # the player presets are in the "vtuner" tab (RADIOQUERYPRESETS)


def radio_row(query, row, preset):
    """A vTuner row (32 fields; the app reads up to field 27).
    Preset lists (RADIOQUERYPRESETS/...): field 11 = type; type 2 = station with name in
    field 10 and id in field 16 (the id the app and the controller send back in RADIOQUEUE).
    The controller also reads field 4 (preset number) and 5 (name).
    Browse list (RADIOQUERYCHILDNODES): field 4 = type; type 2 = station with name in field 3
    and id in field 9 (type 3 is a folder, id = field 7 + "~" + field 8, opened with
    another RADIOQUERYCHILDNODES)."""
    key, name, _pic = preset
    rid = str(ID_RADIO + key)
    f = [q("")] * 32
    f[0], f[1], f[2], f[3] = "E:GETROWS", query, str(row), q(name)
    if query == "RADIOQUERYCHILDNODES":
        f[4], f[9] = "2", rid
    else:
        f[4], f[5], f[10], f[11], f[16] = str(key), q(name), q(name), "2", rid
    return " ".join(f)


def item_by_id(arg):
    """(kind, item) for an id sent back by the app, or for a name."""
    a = arg.strip().strip('"')
    if a.isdigit():
        n = int(a)
        if ID_RADIO < n < ID_PLAYLIST:
            pr = [p for p in get_presets() if p[0] == n - ID_RADIO]
            return ("radio", pr[0]) if pr else (None, None)
        if n >= ID_DYN:
            it = registry.get(n)
            return ("dyn", it) if it else (None, None)
        for base, mt in ((ID_GENRE, "genre"), (ID_TRACK, "track"), (ID_ARTIST, "artist"), (ID_ALBUM, "album")):
            if n >= base:
                lst = lib(mt)
                return (mt, lst[n - base]) if n - base < len(lst) else (None, None)
        if n >= ID_PLAYLIST:
            lst = playlists()
            return ("playlist", lst[n - ID_PLAYLIST]) if n - ID_PLAYLIST < len(lst) else (None, None)
        return (None, None)
    for e in playlists():
        if e[0] == a:
            return ("playlist", e)
    for mt in ("album", "artist", "genre"):
        for it in lib(mt):
            if it["name"] == a or it["name"].split(" (")[0] == a:
                return (mt, it)
    return (None, None)


def get_queue_id():
    """MA queue/player id of the player (matched on its UPnP UDN)."""
    global queue_id
    if queue_id:
        return queue_id
    try:
        udn = ""
        try:
            udn = tag(http("http://%s:49152/description.xml" % PLAYER).decode("utf-8", "replace"), "UDN")
            udn = udn.replace("uuid:", "").lower()
        except Exception:  # noqa: BLE001
            pass
        players = ma_api("players/all") or []
        for p in players:
            pid = p.get("player_id", "")
            if udn and udn in pid.lower():
                queue_id = pid
        if not queue_id:
            for p in players:
                if p.get("name") == MA_PLAYER.split(".")[-1] or p.get("name", "").lower() == MA_PLAYER.split(".")[-1]:
                    queue_id = p.get("player_id")
        log("MA queue id: %s" % queue_id)
    except Exception as e:  # noqa: BLE001
        log("MA players error: %s" % e)
    return queue_id


def ma_play(uri, what, start_item=None):
    global clear_pending
    option = "replace" if time.time() - clear_pending < 10 else "add"
    clear_pending = 0.0
    args = {"queue_id": get_queue_id(), "media": uri, "option": option}
    if start_item:
        args["start_item"] = start_item
    try:
        ma_api("player_queues/play_media", args)
        log("MA PLAY %s (%s%s)" % (what, option, ", from " + start_item if start_item else ""))
    except Exception as e:  # noqa: BLE001
        log("MA play error: %s" % e)


def play_entry(kind, item):
    """Play what an id/name points to. Returns False when there is nothing to play."""
    if not item:
        return False
    if kind == "radio":
        linkplay("MCUKeyShortClick:%d" % item[0])
        log("PRESET %d %s" % (item[0], item[1]))
        return True
    if kind == "playlist":
        name, pk, ref, _ = item
        if pk == "preset":
            linkplay("MCUKeyShortClick:%d" % ref)
            log("PRESET %d %s" % (ref, name))
        else:
            ma_play(ref["uri"], "playlist " + name)
        return True
    if kind == "dyn" and item.get("media_type") == "track" and item.get("parent") and \
            time.time() - clear_pending < 10:
        # A track of an album/playlist chosen after "clear queue": play the album/playlist from it
        ma_play(item["parent"], "%s from %s" % (item["parent"], item["name"]), start_item=item["uri"])
        return True
    ma_play(item["uri"], "%s %s" % (item.get("media_type"), item.get("name")))
    return True


def quoted_arg(line):
    m = re.search(r'"([^"]*)"', line)
    if m:
        return m.group(1)
    parts = line.split(" ")
    return parts[1] if len(parts) > 1 else ""


def song_row(query, row, title, ident, artist, album, album_id, seconds):
    # The app reads song fields by position: pad the row so it never reads past its end
    f = ["E:GETROWS", query, str(row), q(title), str(ident), q(artist), q(album), str(album_id), str(seconds)]
    f += [q("")] * (32 - len(f))
    return " ".join(f)


def padded(fields, n=16):
    return " ".join(fields + [q("")] * max(0, n - len(fields)))


def open_sub_list(cmd, arg):
    """The app opened a playlist / album / artist / genre: remember its contents."""
    kind, item = item_by_id(arg)
    sub = None
    if cmd == "QUERYSONGSBYPLAYLIST" and kind == "playlist":
        if item[1] == "ma":
            sub = {"kind": "playlist", "item": item[2], "children": children("playlist", item[2])}
        else:
            sub = {"kind": "preset", "item": item, "children": []}
    elif cmd == "QUERYSONGSBYALBUM" and kind in ("album", "dyn") and item.get("media_type") == "album":
        sub = {"kind": "album", "item": item, "children": children("album", item)}
    elif cmd == "QUERYSONGSBYALBUM" and kind in ("artist", "genre"):
        sub = {"kind": kind, "item": item, "children": []}          # an artist/genre entry opened as album
    elif cmd == "QUERYALBUMSBYARTIST" and kind in ("artist", "dyn"):
        sub = {"kind": "artist_albums", "item": item, "children": children("artist_albums", item)}
    elif cmd == "QUERYSONGSBYARTIST" and kind in ("artist", "dyn"):
        sub = {"kind": "artist_tracks", "item": item, "children": children("artist_tracks", item)}
    elif cmd == "QUERYALBUMSBYGENRE" and kind == "genre":
        sub = {"kind": "genre_albums", "item": item, "children": children("genre_albums", item)}
    elif cmd == "QUERYSONGSBYGENRE" and kind == "genre":
        sub = {"kind": "genre_tracks", "item": item, "children": children("genre_tracks", item)}
    log("SUBLIST %s %s -> %s (%d)" % (cmd, arg, sub["kind"] if sub else None, len(sub["children"]) if sub else 0))
    return sub


def sub_list_count():
    if not last_parent:
        return 0
    return len(last_parent["children"]) or 1


def sub_list_rows(query, rows):
    if not last_parent:
        return []
    sub, out = last_parent, []
    kids = sub["children"]
    if kids:
        parent_uri = sub["item"]["uri"] if sub["kind"] in ("album", "playlist") else None
        for r in rows:
            if r > len(kids):
                break
            c = kids[r - 1]
            i = reg(c, parent_uri)
            album = c["name"] if c["media_type"] == "album" else (c["album"] or sub["item"].get("name", ""))
            out.append(song_row(query, r, c["name"], i, c["artist"], album, i, c["duration"]))
        return out
    if 1 in rows:
        # Nothing inside (a radio preset, an artist without albums...): one entry that plays it
        if sub["kind"] == "preset":
            name = sub["item"][0]
            i = ID_PLAYLIST + [e[0] for e in playlists()].index(name)
            out.append(song_row(query, 1, name, i, "", name, i, 0))
        else:
            it = sub["item"]
            i = reg(it)
            out.append(song_row(query, 1, it["name"], i, it.get("artist", ""), it["name"], i, 0))
    return out


def queue_items(start, count):
    qid = get_queue_id()
    if not qid:
        return []
    return ma_list("player_queues/items", {"queue_id": qid, "limit": count, "offset": start - 1})


def queue_count():
    qid = get_queue_id()
    if not qid or not MA_TOKEN:
        return 0
    try:
        qd = ma_api("player_queues/get", {"queue_id": qid}) or {}
        return int(qd.get("items") or 0)
    except Exception as e:  # noqa: BLE001
        log("MA queue error: %s" % e)
        return 0


def get_rows(p):
    query = p[1].upper() if len(p) > 1 else ""
    start = int(p[2]) if len(p) > 2 and p[2].isdigit() else 1
    count = int(p[3]) if len(p) > 3 and p[3].isdigit() else 100
    start = max(start, 1)
    out = []

    def window(n):
        return range(start, min(n, start + count - 1) + 1)

    def simple(lst, base):
        for r in window(len(lst)):
            out.append("E:GETROWS %s %d %s %d" % (query, r, q(lst[r - 1]["name"]), base + r - 1))

    if query in RADIO_QUERIES:
        # vTuner tab: the player presets (also used by the controller for tuner presets)
        lst = get_presets() if query in ("RADIOQUERYPRESETS", "RADIOQUERYCHILDNODES") else []
        for r in window(len(lst)):
            out.append(radio_row(query, r, lst[r - 1]))
    elif query == "QUERYPLAYLISTS":
        lst = playlists()
        for r in window(len(lst)):
            out.append("E:GETROWS %s %d %s %d" % (query, r, q(lst[r - 1][0]), ID_PLAYLIST + r - 1))
    elif query == "QUERYARTISTS":
        simple(lib("artist"), ID_ARTIST)
    elif query == "QUERYGENRES":
        simple(lib("genre"), ID_GENRE)
    elif query == "QUERYALBUMS":
        al = lib("album")
        for r in window(len(al)):
            it = al[r - 1]
            out.append(padded(["E:GETROWS", query, str(r), str(ID_ALBUM + r - 1), q(it["name"]), q(it["artist"])]))
    elif query == "QUERYSONGS":
        tr = lib("track")
        for r in window(len(tr)):
            it = tr[r - 1]
            out.append(song_row(query, r, it["name"], ID_TRACK + r - 1, it["artist"], it["album"], ID_TRACK + r - 1,
                                it["duration"]))
    elif query in SUBLISTS:
        out += sub_list_rows(query, list(window(sub_list_count())))
    elif query == "QUERYQUEUE":
        items = queue_items(start, count)
        if items:
            for k, it in enumerate(items):
                i = reg(it)
                out.append(song_row(query, start + k, it["name"], i, it["artist"], it["album"], i, it["duration"]))
        elif start == 1:
            m = cur
            out.append(song_row(query, 1, m.title or "Now playing", 1, m.artist, m.album, m.album_id, m.length // 1000))
    out.append("R:GETROWS %s %d %d%s" % (query, start, count, (" %d" % count) if query in SUBLISTS else ""))
    return out


# ---------------------------------------------------------------- command answers
def answer(line):
    global last_parent, clear_pending
    p = line.split(" ")
    cmd = p[0].upper()
    zone = p[-1] if len(p) > 1 else "1"
    z1 = zone == "1"
    m = cur if z1 else Media()
    o = []
    if cmd in ("DELIMITER", "UTF8", "STATUS"):
        o.append("R:" + line)
    elif cmd == "VERSION":
        o.append("R:VERSION 4.0.0.0")
    elif cmd == "GETZONES":
        o += ["E:GETZONES %d %s" % (i + 1, q(n)) for i, n in enumerate(ZONES)]
        o.append("R:GETZONES %d" % len(ZONES))
    elif cmd == "ZONE":
        o.append("R:ZONE %s OK" % zone)
    elif cmd == "GETPLAY":
        o.append("R:GETPLAY %s %s" % (zone, m.state))
    elif cmd == "GETPOSITION":
        o.append("R:GETPOSITION %s %d" % (zone, m.pos // 1000))
    elif cmd == "GETVOLUME":
        o.append("R:GETVOLUME %s %d" % (zone, m.vol))
    elif cmd == "GETMUTE":
        o.append("R:GETMUTE %s %s" % (zone, "ON" if m.mute else "OFF"))
    elif cmd == "GETSHUFFLE":
        o.append("R:GETSHUFFLE %s %s" % (zone, "ON" if shuffle(m.loop) else "OFF"))
    elif cmd == "GETREPEAT":
        o.append("R:GETREPEAT %s %s" % (zone, "ON" if repeat(m.loop) else "OFF"))
    elif cmd == "GETQUEUEPOSITION":
        o.append('R:GETQUEUEPOSITION %s 0 "1"' % zone)
    elif cmd == "GETCURRENTMEDIA":
        for key, val in (("Title", q(m.title)), ("Author", q(m.artist)), ("WM/AlbumArtist", q(m.artist)),
                         ("Album", q(m.album)), ("WM/AlbumTitle", q(m.album)), ("AlbumID", q(str(m.album_id))),
                         ("Duration", str(m.length // 1000)), ("Kind", q("MUSIC"))):
            o.append("E:GETCURRENTMEDIA %s %s %s" % (key, val, zone))
        o.append("R:GETCURRENTMEDIA %s OK" % zone)
    elif cmd in ("PLAY", "PAUSE", "STOP", "NEXT", "PREVIOUS"):
        if z1 and from_ma(m):
            ma_cmd({"PLAY": "media_play", "PAUSE": "media_pause", "STOP": "media_stop",
                    "NEXT": "media_next_track", "PREVIOUS": "media_previous_track"}[cmd])
        elif z1:
            linkplay("setPlayerCmd:" + {"PLAY": "resume", "PAUSE": "pause", "STOP": "stop",
                                      "NEXT": "next", "PREVIOUS": "prev"}[cmd])
        o.append("R:" + line)
    elif cmd == "VOLUME":
        if z1 and len(p) > 2:
            linkplay("setPlayerCmd:vol:%d" % round(float(p[1])))
        o.append("R:" + line)
    elif cmd == "MUTE":
        if z1 and len(p) > 2:
            linkplay("setPlayerCmd:mute:%d" % (1 if p[1].upper() in ("ON", "1") else 0))
        o.append("R:" + line)
    elif cmd == "POSITION":
        if z1 and len(p) > 2:
            if from_ma(m):
                ma_cmd("media_seek", {"seek_position": float(p[1])})
            else:
                linkplay("setPlayerCmd:seek:%d" % int(float(p[1])))
        o.append("R:" + line)
    elif cmd == "SHUFFLE":
        if z1 and len(p) > 2:
            if from_ma(m):
                ma_cmd("shuffle_set", {"shuffle": p[1].upper() == "ON"})
            else:
                set_loop(p[1].upper() == "ON", repeat(m.loop))
        o.append("R:" + line)
    elif cmd == "REPEAT":
        if z1 and len(p) > 2:
            if from_ma(m):
                ma_cmd("repeat_set", {"repeat": "all" if p[1].upper() == "ON" else "off"})
            else:
                set_loop(shuffle(m.loop), p[1].upper() == "ON")
        o.append("R:" + line)
    elif cmd in RADIO_QUERIES:
        # Count of the vTuner list (repeated: the controller reads field 1, keep field 2 valid too)
        n = len(get_presets()) if cmd in ("RADIOQUERYPRESETS", "RADIOQUERYCHILDNODES") else 0
        o.append("R:%s %d %d" % (cmd, n, n))
    elif cmd == "RADIOQUEUE":
        # "RADIOQUEUE <id> <zone>" (controller) or "RADIOQUEUE "<id>"" (app)
        arg = quoted_arg(line) if '"' in line else (p[1] if len(p) > 1 else "")
        kind, item = item_by_id(arg)
        if kind == "radio":
            play_entry(kind, item)
        else:
            log("RADIOQUEUE unknown: " + line)
        o.append("R:" + line)
    # ---- library counts
    elif cmd == "QUERYPLAYLISTS":
        o.append("R:QUERYPLAYLISTS %d" % len(playlists()))
    elif cmd == "QUERYALBUMS":
        o.append("R:QUERYALBUMS %d" % len(lib("album")))
    elif cmd == "QUERYARTISTS":
        o.append("R:QUERYARTISTS %d" % len(lib("artist")))
    elif cmd == "QUERYSONGS":
        o.append("R:QUERYSONGS %d" % len(lib("track")))
    elif cmd == "QUERYGENRES":
        o.append("R:QUERYGENRES %d" % len(lib("genre")))
    elif cmd in SUBLISTS:
        arg = quoted_arg(line)
        last_parent = open_sub_list(cmd, arg)
        o.append("R:%s %s %d" % (cmd, q(arg), sub_list_count()))
    elif cmd == "QUERYQUEUE":
        o.append("R:QUERYQUEUE %s %d" % (zone, queue_count() or 1))
    elif cmd == "GETROWS":
        o += get_rows(p)
    # ---- playback of library items
    elif cmd == "CLEARQUEUE":
        clear_pending = time.time()
        o.append("R:" + line)
    elif cmd in ("QUEUEPLAYLIST", "QUEUE"):
        kind, item = item_by_id(quoted_arg(line))
        if not play_entry(kind, item):
            log("nothing to play for: " + line)
        o.append("R:" + line)
    elif cmd == "QUEUEBY":
        kind, item = item_by_id(quoted_arg(line))
        if not play_entry(kind, item):
            log("QUEUEBY nothing to play: " + line)
        o.append("R:" + line)
    elif cmd == "QUEUEPOSITION":
        # Jump to an item of the queue: "QUEUEPOSITION <n> "<id>" <zone>"
        qid = get_queue_id()
        if qid and len(p) > 1 and p[1].isdigit():
            try:
                ma_api("player_queues/play_index", {"queue_id": qid, "index": int(p[1]) - 1})
                log("MA queue -> index %s" % p[1])
            except Exception as e:  # noqa: BLE001
                log("MA play_index error: %s" % e)
        o.append("R:" + line)
    else:
        o.append("R:" + line)
    return o


def image_for(req_path):
    """Cover for /Vantage?Cmd=...&Id=...: the image of that item, or the current cover."""
    m = re.search(r"[?&]Id=([^&]*)", req_path)
    ident = urllib.parse.unquote(m.group(1)) if m else ""
    if ident and not (ident.isdigit() and int(ident) >= 1000000):
        kind, item = item_by_id(ident)
        if kind == "radio" and item and item[2]:
            return item[2]
        if kind == "playlist" and item and item[3]:
            return item[3]
        if kind and kind not in ("playlist", "radio") and item and item.get("image"):
            return item["image"]
    return cur.art


# ---------------------------------------------------------------- sessions
sessions = set()


class Session:
    def __init__(self, writer, who):
        self.writer = writer
        self.who = who
        self.eol = "\r\n"
        self.status = False

    def send(self, line, show_it=True):
        try:
            self.writer.write((line + self.eol).encode("utf-8"))
            if show_it:
                log("SEND %s : %s" % (self.who, show(line)))
        except Exception:  # noqa: BLE001
            pass


def push(line):
    for s in list(sessions):
        if s.status:
            s.send(line)


async def command_client(reader, writer):
    peer = writer.get_extra_info("peername")
    s = Session(writer, "%s:%s" % (peer[0], peer[1]) if peer else "?")
    log("CONNECT " + s.who)
    sessions.add(s)
    buf = ""
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for line in parts:
                line = line.strip()
                if not line:
                    continue
                log("RECV %s : %s" % (s.who, show(line)))
                if line == "DELIMITER 0D":
                    s.eol = "\r"
                if line.startswith("STATUS"):
                    s.status = True
                replies = await asyncio.get_running_loop().run_in_executor(None, answer, line)
                for r in replies:
                    s.send(r, not r.startswith("R:GET") and not r.startswith("E:GETCURRENTMEDIA"))
                await writer.drain()
    except Exception as e:  # noqa: BLE001
        log("ERROR %s : %s" % (s.who, e))
    sessions.discard(s)
    log("CLOSE " + s.who)
    writer.close()


async def idle_client(reader, writer):
    peer = writer.get_extra_info("peername")
    log("CONNECT %s -> 30521" % (peer[0] if peer else "?"))
    try:
        while await reader.read(4096):
            pass
    except Exception:  # noqa: BLE001
        pass
    writer.close()


art_cache = {}        # url -> bytes (the last ~150 images)


def square(data):
    """The app stretches every cover to a square: centre non-square images (radio logos)
    on a square canvas of the colour of their corner. Square images are returned as they are."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w == h or not w or not h:
            return data
        rgba = im.convert("RGBA")
        bg = rgba.getpixel((0, 0))
        if bg[3] < 128:                       # transparent corner: white background
            bg = (255, 255, 255, 255)
        side = max(w, h)
        canvas = Image.new("RGB", (side, side), bg[:3])
        canvas.paste(rgba, ((side - w) // 2, (side - h) // 2), rgba)
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        log("square error: %s" % e)
        return data


def cover_bytes(path):
    url = image_for(path)
    if not url:
        return b""
    if url not in art_cache:
        data = square(http(url, timeout=8))
        art_cache[url] = data
        while len(art_cache) > 150:
            del art_cache[next(iter(art_cache))]
        log("cover downloaded %d bytes" % len(data))
    return art_cache[url]


async def cover_client(reader, writer):
    try:
        req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        first = req.split(b"\r\n", 1)[0].decode("latin-1")
        path = first.split(" ")[1] if " " in first else ""
        if path.startswith("/diag") and DIAGNOSTICS:
            # Diagnostic page: sessions + log tail (option "diagnostics")
            text = "sessions:\n" + "\n".join("  %s status=%s" % (s.who, s.status) for s in list(sessions))
            text += "\n\nlog:\n" + "\n".join(LOG_TAIL)
            body = text.encode("utf-8")
            writer.write(("HTTP/1.0 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\n"
                          "Connection: close\r\n\r\n" % len(body)).encode("ascii") + body)
            await writer.drain()
            writer.close()
            return
        log("HTTP " + show(first))
        try:
            body = await asyncio.get_running_loop().run_in_executor(None, cover_bytes, path)
        except Exception as e:  # noqa: BLE001
            log("cover error: %s" % e)
            body = b""
        ctype = "image/png" if body[:4] == b"\x89PNG" else "image/jpeg"
        status = "200 OK" if body else "404 Not Found"
        writer.write(("HTTP/1.0 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                      % (status, ctype, len(body))).encode("ascii") + body)
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    writer.close()


# ---------------------------------------------------------------- poller
async def poller():
    global cur
    loop = asyncio.get_running_loop()
    while True:
        try:
            m = await loop.run_in_executor(None, read_status)
            old = cur
            track_changed = m.title != old.title or m.artist != old.artist
            if track_changed or not old.art:
                await loop.run_in_executor(None, position_info, m)
            else:
                m.art = old.art
                m.length = m.length or old.length
                m.album = m.album or old.album
            # Above the library id ranges (100000-499999) so image_for() never confuses them
            m.album_id = 1000000 + (zlib.crc32((m.art or (m.album + m.artist)).encode("utf-8")) & 0x7FFFFFF)
            if from_ma(m):
                await loop.run_in_executor(None, ma_loop, m)
            cur = m
            if track_changed or m.art != old.art:
                log("NOW %s / %s / %s / %s" % (m.title, m.artist, m.album, m.art))
                push("S:MEDIACHANGE NEWMEDIA 1")
            if m.state != old.state:
                push("S:PLAYSTATE %s 1" % m.state)
            if m.vol != old.vol:
                push("S:VOLUME %d 1" % m.vol)
            if m.mute != old.mute:
                push("S:MUTE %s 1" % ("ON" if m.mute else "OFF"))
            if shuffle(m.loop) != shuffle(old.loop):
                push("S:SHUFFLE %s 1" % ("ON" if shuffle(m.loop) else "OFF"))
            if repeat(m.loop) != repeat(old.loop):
                push("S:REPEAT %s 1" % ("ON" if repeat(m.loop) else "OFF"))
        except Exception as e:  # noqa: BLE001
            log("poll error: %s" % e)
        await asyncio.sleep(2)


async def main():
    log("Vantage DMM bridge - player %s, zones %s, Music Assistant %s" % (PLAYER or "(not set)", ZONES, "on" if MA_TOKEN else "off"))
    servers = [
        await asyncio.start_server(command_client, "0.0.0.0", 30535),
        await asyncio.start_server(idle_client, "0.0.0.0", 30521),
        await asyncio.start_server(cover_client, "0.0.0.0", 30545),
    ]
    log("listening on 30535, 30521, 30545")
    asyncio.create_task(poller())
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
