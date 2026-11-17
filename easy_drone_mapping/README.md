# Easy Drone Mapping — QGIS plugin

Turn a folder of drone photos into an **orthophoto**, **DSM / DTM elevation
models** and a **point cloud**, using free open-source
[OpenDroneMap](https://www.opendronemap.org/) — without leaving QGIS.

Everything runs on your own computer. Photos never leave it.

## Install

1. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**, choose
   `easy_drone_mapping.zip`, click **Install Plugin**.
2. Click the green drone button on the toolbar (or **Plugins → Easy Drone
   Mapping**). The panel opens on the right.

Works with QGIS 3.28 or newer, including QGIS 4.

## The processing engine

**Windows — nothing to set up.** The first time you click *Process photos*,
the plugin asks to download OpenDroneMap (about 234 MB) from the project's
official GitHub releases and installs it for your user account. No
administrator rights, no Docker, no other program to open. After that,
everything happens inside QGIS.

**macOS / Linux — Docker.** OpenDroneMap publishes a ready-made installer for
Windows only, so on these systems the plugin runs it in Docker:

* macOS — install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  and start it once.
* Linux — `sudo apt install docker.io`, then
  `sudo usermod -aG docker $USER` and log out and in again.

Click *Process photos* and the plugin starts the engine itself (first time:
a download of a few GB).

You can switch engines in **Settings → Processing engine** (for example to use
Docker on Windows, or a more powerful computer on your network).

## Use

1. **Processing engine** — nothing to do: clicking *Process photos* installs
   or starts the engine for you when needed.
2. **Photos** — choose the folder with the photos from one flight. *Show on
   map* plots where each photo was taken so you can check coverage first.
3. **What to create** — tick the products and pick a quality:

   | Product | What you get | File |
   |---|---|---|
   | Orthophoto | Seamless, map-accurate aerial image | `odm_orthophoto/odm_orthophoto.tif` |
   | Surface model (DSM) | Elevation incl. trees and buildings | `odm_dem/dsm.tif` |
   | Terrain model (DTM) | Bare-ground elevation | `odm_dem/dtm.tif` |
   | Point cloud | Millions of 3D points | `odm_georeferencing/odm_georeferenced_model.laz` (+ `.copc.laz`) |
   | 3D textured model | Photo-textured mesh | `odm_texturing/*.obj` |

4. **Save results** — a name and a folder (filled in for you).

Click **Process photos**. You can keep working in QGIS; progress is shown in
the panel and in the QGIS status bar. When it finishes, the layers are added to
your project in a group, already styled (elevation colours, hillshade), and
the *Quality report* button opens ODM's PDF report.

If a job stops (an error, or QGIS was closed), open the panel and click
**Resume**. You can change the settings first: Resume uses what the panel
shows at that moment. With the built-in engine the job carries on from the last completed
step; with the Docker engine it kept running in the background and Resume
collects the results.

### More options

* **Pixel sizes** — leave on *Automatic* unless you need a specific resolution.
* **Ground control** — an ODM `gcp_list.txt` file for survey-grade accuracy.
* **Extra ODM options** — any [ODM option](https://docs.opendronemap.org/arguments/)
  as `name=value; other-name=value`.

### Quality presets

| Preset | ODM settings |
|---|---|
| Fast preview | feature-quality medium, pc-quality low, 5 cm orthophoto, 10 cm DEM |
| Balanced | feature-quality high, pc-quality medium, 5 cm orthophoto, 5 cm DEM |
| High detail | feature-quality high, pc-quality high, 2 cm orthophoto, 2 cm DEM |

ODM never produces pixels finer than the photos support, whatever is asked.
If only *Orthophoto* is ticked, ODM's `fast-orthophoto` method is used.

## How long will it take?

Once photos are chosen, the panel shows an estimate for this computer under
*Quality*, and how much unticking *3D textured model* or closing other programs
would save. While a job runs it shows the time left. Estimates start from a
real 123-photo survey and adjust to your computer after every finished job.

**Memory decides the speed.** Photo matching and texturing need about 2 GB of
free memory per processor core. With little free memory the plugin limits
those steps to fewer cores (so the job does not crash) and, when it starts,
offers to wait while you close programs, listing the ones using the most.
The 3D point cloud step needs much less memory, so it automatically runs with
more cores. Example: 125 photos on a 12-core, 16 GB laptop take about 2 hours
with 2.4 GB free, and about 40 minutes with other programs closed.

## Tips

* Fly with 70–80 % overlap, camera straight down, and use the original files
  from the drone.
* Photogrammetry is memory-hungry: roughly 16 GB of RAM for a few hundred
  photos. On smaller machines use *Fast preview*.
* The built-in engine uses all processor cores, so the computer will be busy
  while a job runs.
* A more powerful PC on your network can do the processing: run NodeODM there,
  choose the Docker / network engine in Settings and enter its address.
* Docker engine only: Docker Desktop is limited to half of your RAM by
  default; raise it in Docker Desktop → Settings → Resources if jobs run out
  of memory.

## Troubleshooting

| Message | What to do |
|---|---|
| "Strange values in the reconstruction" while making the terrain model | With OpenDroneMap 3.6 this happens when the folder path contains spaces. The plugin now gives OpenDroneMap a space-free name for the same folder; click **Resume** and only the last steps are redone. |
| OpenDroneMap crashed while starting (Windows error 0xC0000005) | Usually an old Microsoft Visual C++ runtime. The plugin lends OpenDroneMap QGIS's newer copy automatically; if it cannot, install the latest "Microsoft Visual C++ Redistributable (x64)" from Microsoft and restart QGIS. |
| OpenDroneMap is installed but will not start | The plugin checks the engine before every job and repairs it when it can. If it cannot, say Yes to the reinstall offer; the job then starts again by itself. |
| The download failed | Check the internet connection (or your office proxy settings in QGIS → Settings → Options → Network) and click *Install engine* again. |
| Docker is not installed yet | Only for the Docker engine: see *The processing engine* above. |
| Docker is installed but not running | Open Docker Desktop and wait until it reports it is running. |
| Port 3000 is already used | Another program (e.g. WebODM) uses it. Set the engine address to `http://localhost:3001` in Settings. |
| The computer ran out of memory | Close other programs (browsers especially), untick *3D textured model* unless you need it, keep plenty of free space on drive C:, then click **Resume** — it carries on from the last finished step with the settings shown in the panel. The built-in engine already limits how many photos it works on at once to suit the free memory. Docker engine: also give Docker more memory. |
| ODM could not stitch these photos | Usually too little overlap, too few photos, or featureless ground (water, sand). |

The **Details** section at the bottom of the panel shows the full ODM log.

## Licences

* This plugin: GNU GPL v3 or later (see `LICENSE`).
* OpenDroneMap and NodeODM: GNU AGPL v3 — free for any use, including
  commercial work. The plugin runs OpenDroneMap as a separate program (or
  talks to it over its public API); it does not include its code.
* The built-in Windows engine needs nothing else. If you use the Docker
  engine: Docker Engine (Linux) is open source; **Docker Desktop**
  (Windows/macOS) is free for personal use, education and businesses with
  fewer than 250 employees *and* under US$10 million annual revenue.

This plugin is an independent project and is not affiliated with or endorsed
by the OpenDroneMap project.
