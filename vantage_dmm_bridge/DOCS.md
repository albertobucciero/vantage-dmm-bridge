# Vantage DMM bridge

This add-on makes a LinkPlay network player look like a **Vantage DMM**
(Digital Media Manager) to a Vantage InFusion controller and to the Vantage
**Equinox** app. The app then shows cover art and browse lists for that
player, which it does not show for third-party drivers.

## What you get in the Equinox app

- **Now playing:** cover art, title and artist. Play/pause, next/previous,
  shuffle, repeat and volume.
- **Library**, when Music Assistant is configured: playlists, albums, artists
  and genres. Inside albums and playlists the app shows the tracks, and an
  artist opens its albums. The queue page shows the real Music Assistant
  queue.
- **vtuner** tab: the presets saved in the player.

## Requirements

- A LinkPlay player that answers the HTTP API on port 80, for example
  `http://<player>/httpapi.asp?command=getPlayerStatus`. Devices that only
  accept HTTPS, such as recent WiiM firmware, are not supported.
- Optional: the Music Assistant add-on with the player added to it.
- The Equinox app on the same network as Home Assistant. The app connects
  directly to this add-on, so it does not work over remote connections.

## Options

| Option | Meaning |
|---|---|
| `player_host` | IP address of the LinkPlay player (required). |
| `ma_player` | Home Assistant entity of the player in Music Assistant, for example `media_player.living_room`. When Music Assistant is playing, next/previous, shuffle and repeat are sent to it. |
| `ma_url` | Music Assistant server. The default `http://127.0.0.1:8095` works for the Music Assistant add-on. |
| `ma_token` | A Music Assistant long-lived token, created in Music Assistant's user settings. Without it the library lists are empty. |
| `zone_names` | Names of the 4 DMM zones; only zone 1 is used. |
| `diagnostics` | When on, `http://<home assistant>:30545/diag` shows the connections and the last 400 log lines. Leave it off in normal use. |

The add-on uses the host network and opens ports **30535**, **30521** and
**30545** on Home Assistant.

## Design Center setup

1. Add a **Vantage DMM** object (Audio/Video) and set its **HostName** to the
   IP address of Home Assistant.
2. In the audio source that should show the player (for example the "Network"
   source of the player's driver), set **Source Device** to **Vantage DMM
   Source 1**.
3. Program the controller, then close and reopen the Equinox app.

Volume and input switching stay with the player's own Vantage driver. The
bridge only feeds the source page of the app.

## Limits

- One player per add-on, in DMM zone 1.
- Track lists inside albums and playlists, play-from-track and the queue need
  the Music Assistant token.
- The DMM protocol is not documented by Vantage. This add-on follows what the
  Vantage DMM driver and the Equinox app were observed to send and expect. A
  future Equinox update may break it.

Not affiliated with or endorsed by Vantage, Legrand, LinkPlay or Arylic.
