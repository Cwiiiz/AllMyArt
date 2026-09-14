# All My Art

A Windows desktop app for managing every asset across all the games I work on, built for my Roblox game development pipeline.

## Why I built it

For years I set up folder structures for my 3D models by hand, which meant no two projects ever looked the same. That inconsistency turned into chaos: I constantly lost track of which files belonged to which asset, and the time I should have spent modelling went into hunting for files instead.

So I built something to automate the tedious parts, a desktop app that runs locally on my machine and keeps every project in the same shape.

## What it does

**Assets.** Every asset is created with its matching project, type and category, so the folder structure is identical every time. The app then shows which files are present and which are still missing, checks model stats before anything goes to Roblox, and keeps todos and revision requests attached to the asset itself.

**Views.** The **Overview** shows every asset across every game at a glance. The **Board** lets me drag assets between pipeline stages. The **Library** holds alphas, decals, faces, references and tilings. The **Journey** view tracks my Robux earnings alongside the hours I spend in Blender, Substance 3D Painter, ZBrush and Cinema 4D.

**Sync and settings.** One click on *Sync All* backs everything up to the cloud. The local root, cloud root and library folders are all configurable, and a rescan rebuilds the asset list when something changes outside the app.

**Integrations.** The **Pipeline Hub Export** add-on for Blender exports straight into the correct asset folder, and the **Pipeline Hub Painter** plugin does the same for textures in Substance 3D Painter. Both are installed from inside the app.

## Requirements

Windows, and [Python 3](https://www.python.org/downloads/) if you want to build the app yourself. Blender and Substance 3D Painter are only needed for the integrations.

## How to run

Download `pipeline_hub.py` and `Build_EXE.bat` into the same folder, then run the `.bat`. It installs the dependencies and produces `PipelineHub.exe`, which you can launch directly from then on.

To run it without building:

```
python pipeline_hub.py
```

## Still in progress

Review states and Discord notifications aren't finished yet.

Built with Python, pywebview and PyInstaller.