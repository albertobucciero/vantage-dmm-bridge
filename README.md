# Vantage DMM bridge for Home Assistant

A Home Assistant add-on that makes a LinkPlay network player (tested with an
Arylic Up2Stream Amp V4) look like a **Vantage DMM** (Digital Media Manager) to
a Vantage InFusion controller and to the Vantage **Equinox** app.

The Equinox app shows cover art and browse lists only for media servers it
knows: Vantage DMM, NuVo, iPort, Kaleidescape, Autonomic and Mozaex. Other
drivers get a simple page with a title line at most. With this add-on the
player's source page in the app gets:

- the now-playing page, with cover art, title, artist and transport;
- the Music Assistant library (playlists, albums, artists, genres), with the
  tracks of albums and playlists and the queue;
- the player presets in the **vtuner** tab.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories** and add `https://github.com/albertobucciero/vantage-dmm-bridge`.
   Or copy the `vantage_dmm_bridge` folder into the `addons` share of Home
   Assistant and reload the store.
2. Install **Vantage DMM bridge**, set the options (see the add-on
   documentation) and start it.
3. In Design Center, add a **Vantage DMM** object with HostName = the IP of
   Home Assistant. Set the **Source Device** of the player's audio source to
   **Vantage DMM Source 1**, then program the controller.

The full documentation is in [vantage_dmm_bridge/DOCS.md](vantage_dmm_bridge/DOCS.md).

## How it works

The add-on listens on the DMM ports (30535 commands, 30545 cover art, 30521
accepted). It answers the text protocol that the Vantage DMM driver and the
Equinox app use, with data from:

- the LinkPlay HTTP API (`getPlayerStatus`, `setPlayerCmd`, `MCUKeyShortClick`);
- UPnP on port 49152 (cover art URL, presets);
- the Music Assistant server API (library, tracks, queue, playback).

Protocol notes, as observed:

- Replies are `R:<CMD> <zone> <values>`, list items `E:<CMD> ...`, events
  `S:<EVENT> <value> <zone>`.
- After `DELIMITER 0D`, lines end with CR only.
- Lists are paged with `GETROWS <QUERY> <start> <count>`, and rows are
  numbered from 1.
- For the sub-lists (`QUERYSONGSBY*`, `QUERYALBUMSBY*`), the closing
  `R:GETROWS` line needs one more number than for the main lists. Without it
  the app crashes.
- Song rows are read by position up to field 27, so short rows must be padded.
- vTuner rows carry a type: 2 is a station, 3 is a folder. The station id is
  sent back in `RADIOQUEUE`.
- Cover art is requested as
  `http://<host>:30545/Vantage?Cmd=AlbumCoverArt&Id=<AlbumID>&Width=..&Height=..`.

## Status

Experimental. Tested with Design Center 4.11, Equinox for iOS, Music Assistant
2.10 and one Arylic Up2Stream Amp V4. Reports from other setups are welcome.

## Licence

GNU General Public License v3.0, see [LICENSE](LICENSE).

Not affiliated with or endorsed by Vantage, Legrand, LinkPlay or Arylic. Vantage,
InFusion and Equinox are trademarks of their owners.
