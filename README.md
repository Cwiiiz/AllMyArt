# All My Art

A Windows desktop app for managing every asset across all the games I work on — built for my Roblox game development pipeline.

## Why I built it

For years I set up folder structures for my 3D models by hand, which meant no two projects looked the same. That inconsistency turned into chaos: I constantly lost track of which files belonged to which asset, and the time I should have spent modelling went into hunting for files instead.

So I built something to automate the tedious parts — a desktop app that runs locally on my machine and keeps every project in the same shape.

## What it does

**Assets**
- Create assets with the matching project, type and category
- See which files are present and which are still missing
- Check model stats before exporting to Roblox
- Add todos and request revisions

**Views**
- **Overview** — every asset across every game at a glance
- **Board** — drag and drop assets between pipeline stages
- **Library** — store alphas, decals, faces, references and tilings
- **Journey** — track Robux earnings and hours spent in Blender, Substance 3D Painter, ZBrush and Cinema 4D

**Sync & settings**
- One-click *Sync All* to back everything up to the cloud
- Configurable local root, cloud root and library folders
- Rescan all assets for maintenance

**Integrations**
- **Pipeline Hub Export** — Blender add-on, exports straight into the right asset folder
- **Pipeline Hub Painter** — Substance 3D Painter plugin, same idea for textures

Both plugins are installed from inside the app.

## Requirements

- Windows
- [Python 3](https://www.python.org/downloads/) (only needed to build the app)
- Blender and Substance 3D Painter for the integrations

## How to run

Download `pipeline_hub.py` and `Build_EXE.bat` into the same folder, then run the `.bat`. It installs the dependencies and produces `PipelineHub.exe`, which you can launch directly from then on.

To run it without building:

    python pipeline_hub.py

## Still in progress

Review states and Discord notifications aren't finished yet.

---

Built with Python, pywebview and PyInstaller.