"""
Pipeline Hub v4 — local 3D asset workflow manager (cosmic web UI)
Blender 5.0 -> (ZBrush) -> FBX Export (HP/LP) -> Substance Painter

Structure:   ROOT / GAME / CATEGORY / ASSET / {1_Ref ... 7_Renders}
Dual roots:  Local drive + Cloud folder. New assets created in BOTH.
             "Sync -> Cloud" copies new/changed files (never deletes).

Run:  python pipeline_hub.py     (auto-installs pywebview on first run)
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ---- auto-install pywebview (never inside a frozen exe: sys.executable IS the app,
# so spawning it for pip would relaunch the app in an infinite loop) ------------
_FROZEN = getattr(sys, "frozen", False)
try:
    import webview
except ImportError:
    if _FROZEN:
        sys.exit(1)  # pywebview must be bundled at build time
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    subprocess.run([sys.executable, "-m", "pip", "install", "pywebview"], creationflags=flags)
    try:
        import webview
    except ImportError:
        print("Could not install 'pywebview'. Run:  pip install pywebview")
        sys.exit(1)

# optional: Pillow for asset thumbnails (degrade gracefully without it)
try:
    from PIL import Image
except ImportError:
    Image = None
    if not _FROZEN:
        try:
            _flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            subprocess.run([sys.executable, "-m", "pip", "install", "pillow"], creationflags=_flags)
            from PIL import Image
        except Exception:
            Image = None

# When frozen into an .exe (PyInstaller), __file__ points into a temp dir —
# keep config next to the executable instead.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "pipeline_hub_config.json"
APP_NAME = "ALL MY ART"
APP_VERSION = "3.2.0"

# shared handshake with the Blender add-on (fixed, well-known location)
STATE_DIR = Path.home() / ".pipeline_hub"
STATE_FILE = STATE_DIR / "state.json"

# Substance Painter plugin source, embedded for one-click install
PAINTER_SRC = r'''"""
Pipeline Hub — Substance 3D Painter plugin
Menu 'Pipeline Hub':
  · New Project from Hub       creates a project from  <asset>/4_Export/LP/*.fbx
                               (template picker — defaults to Roblox SurfaceAppearance)
  · Reload Mesh from Hub       swaps in the newest LP export, keeps paint strokes
  · Set HP Bake Mesh           points baking params at  <asset>/4_Export/HP/*.fbx
  · Save Project -> Hub        saves the .spp into  <asset>/5_Substance/
  · Export Textures -> Hub     exports PBR maps flat into  <asset>/6_Textures/  (+ ticks Texturing)

Target asset = whatever is selected in the Pipeline Hub app
(read from ~/.pipeline_hub/state.json).
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

import substance_painter.logging as log
import substance_painter.project
import substance_painter.export
import substance_painter.textureset
import substance_painter.ui

try:
    from PySide6 import QtWidgets  # Painter 10+
except ImportError:
    from PySide2 import QtWidgets  # older Painter

STATE_FILE = Path.home() / ".pipeline_hub" / "state.json"

plugin_widgets = []


# ---------------------------------------------------------------- hub state

def _active_asset():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        a = state.get("active")
        if not a:
            return None, ("No asset selected in Pipeline Hub.\n"
                          "Open the hub and click the asset you are texturing, then retry.")
        p = Path(a["path"])
        if not p.exists():
            return None, "Hub asset folder missing on disk."
        return (p, a["name"]), None
    except Exception as e:
        return None, "Could not read hub state: " + str(e)


def _newest_fbx(folder: Path):
    best, best_t = None, -1.0
    if folder.exists():
        for p in folder.rglob("*"):
            if "_versions" in p.parts:
                continue
            if p.is_file() and p.suffix.lower() in (".fbx", ".obj"):
                t = p.stat().st_mtime
                if t > best_t:
                    best, best_t = p, t
    return best


def _tick_stage(asset_path: Path, stage: str):
    f = asset_path / "pipeline.json"
    try:
        meta = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        meta = {}
    meta.setdefault("stages", {})
    meta["stages"][stage] = datetime.now().strftime("%d.%m.%Y %H:%M")
    try:
        f.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass


def _info(msg):
    log.info("[Pipeline Hub] " + msg)
    try:
        QtWidgets.QMessageBox.information(None, "Pipeline Hub", msg)
    except Exception:
        pass


def _warn(msg):
    log.warning("[Pipeline Hub] " + msg)
    try:
        QtWidgets.QMessageBox.warning(None, "Pipeline Hub", msg)
    except Exception:
        pass


# ---------------------------------------------------------------- templates

PREFS_FILE = Path.home() / ".pipeline_hub" / "painter_prefs.json"


def _load_prefs():
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(prefs):
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except Exception:
        pass


def _find_templates():
    """[(name, path)] — .spt project templates from Painter's install + user assets."""
    import os
    dirs = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Adobe" / "Adobe Substance 3D Painter" / "resources" / "starter_assets" / "templates",
        Path.home() / "Documents" / "Adobe" / "Adobe Substance 3D Painter" / "assets" / "templates",
    ]
    out, seen = [], set()
    for d in dirs:
        try:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.spt")):
                if p.stem.lower() in seen:
                    continue
                seen.add(p.stem.lower())
                out.append((p.stem, p))
        except Exception:
            continue
    return out


def _pick_template():
    """Ask which project template to use. Returns (path|None, cancelled: bool).
    Defaults to the last choice, else Roblox (SurfaceAppearance)."""
    templates = _find_templates()
    if not templates:
        return None, False          # no templates found -> plain PBR project
    no_tpl = "(no template — plain PBR)"
    names = [no_tpl] + [n for n, _ in templates]
    prefs = _load_prefs()
    last = prefs.get("template", "")
    if last in names:
        idx = names.index(last)
    else:
        idx = 0
        low = [n.lower().replace(" ", "") for n in names]
        for want in ("roblox(surfaceappearance", "roblox"):
            hit = next((i for i, n in enumerate(low) if want in n), None)
            if hit is not None:
                idx = hit
                break
    choice, ok = QtWidgets.QInputDialog.getItem(
        None, "Pipeline Hub", "Project template:", names, idx, False)
    if not ok:
        return None, True            # user cancelled
    prefs["template"] = choice
    _save_prefs(prefs)
    if choice == no_tpl:
        return None, False
    for n, p in templates:
        if n == choice:
            return p, False
    return None, False


# ---------------------------------------------------------------- actions

def new_project_from_hub():
    if substance_painter.project.is_open():
        _warn("A project is already open — close it first (File → Close).")
        return
    res, err = _active_asset()
    if err:
        _warn(err)
        return
    asset_path, asset_name = res
    lp = _newest_fbx(asset_path / "4_Export" / "LP")
    if lp is None:
        _warn("No LP mesh found in 4_Export/LP.\nExport one from Blender first (Pipeline panel → Export LP).")
        return
    template, cancelled = _pick_template()
    if cancelled:
        return
    try:
        kwargs = {"mesh_file_path": str(lp)}
        if template is not None:
            # template defines shader, channels and resolution — don't override
            kwargs["template_file_path"] = str(template)
            kwargs["settings"] = substance_painter.project.Settings(import_cameras=False)
        else:
            kwargs["settings"] = substance_painter.project.Settings(
                import_cameras=False, default_texture_resolution=2048)
        substance_painter.project.create(**kwargs)
    except Exception as e:
        _warn("Project creation failed: " + str(e))
        return
    # save straight into 5_Substance so it's never orphaned
    out_dir = asset_path / "5_Substance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / (asset_name + ".spp")
    try:
        substance_painter.project.save_as(
            str(out_file), substance_painter.project.ProjectSaveMode.Full)
        _info("Project created from  " + lp.name + "\nSaved:  " + str(out_file))
    except Exception as e:
        _warn("Project created, but saving failed: " + str(e))


def reload_mesh_from_hub():
    if not substance_painter.project.is_open():
        _warn("No project open.")
        return
    res, err = _active_asset()
    if err:
        _warn(err)
        return
    asset_path, _ = res
    lp = _newest_fbx(asset_path / "4_Export" / "LP")
    if lp is None:
        _warn("No LP mesh found in 4_Export/LP.")
        return

    def _done(status):
        try:
            ok = status == substance_painter.project.ReloadMeshStatus.SUCCESS
        except Exception:
            ok = True
        if ok:
            _info("Mesh reloaded:  " + lp.name + "\nPaint strokes were reprojected.")
        else:
            _warn("Mesh reload finished with status: " + str(status))

    try:
        settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False, preserve_strokes=True)
        substance_painter.project.reload_mesh(str(lp), settings, _done)
    except Exception as e:
        _warn("Mesh reload failed: " + str(e))


def set_hp_bake_mesh():
    if not substance_painter.project.is_open():
        _warn("No project open.")
        return
    res, err = _active_asset()
    if err:
        _warn(err)
        return
    asset_path, _ = res
    hp = _newest_fbx(asset_path / "4_Export" / "HP")
    if hp is None:
        _warn("No HP mesh found in 4_Export/HP.\nExport one from Blender first (Pipeline panel → Export HP).")
        return
    try:
        import substance_painter.baking as baking
    except ImportError:
        _warn("This Painter version does not expose the baking API.\n"
              "Set the high-poly manually: " + str(hp))
        return
    hp_url = hp.as_uri()
    done = []
    try:
        for ts in substance_painter.textureset.all_texture_sets():
            try:
                params = baking.BakingParameters.from_texture_set_name(ts.name())
                common = params.common()
                key = None
                for k in common:
                    if "hipoly" in str(k).lower() or "high" in str(k).lower():
                        key = k
                        break
                if key is not None:
                    baking.BakingParameters.set({common[key]: hp_url})
                    done.append(ts.name())
            except Exception:
                continue
    except Exception as e:
        _warn("Could not access texture sets: " + str(e))
        return
    if done:
        _info("HP bake mesh set to  " + hp.name + "\nTexture sets: " + ", ".join(done)
              + "\n\nOpen the bake dialog and hit Bake — the high-poly is pre-filled.")
    else:
        _warn("Baking API present but no HipolyMesh parameter found.\n"
              "Set the high-poly manually: " + str(hp))


def save_project_to_hub():
    if not substance_painter.project.is_open():
        _warn("No project open.")
        return
    res, err = _active_asset()
    if err:
        _warn(err)
        return
    asset_path, asset_name = res
    out_dir = asset_path / "5_Substance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / (asset_name + ".spp")
    try:
        substance_painter.project.save_as(
            str(out_file), substance_painter.project.ProjectSaveMode.Full)
        _info("Saved:  " + str(out_file))
    except Exception as e:
        _warn("Save failed: " + str(e))


def _find_pbr_preset():
    try:
        presets = substance_painter.export.list_resource_export_presets()
        for p in presets:
            name = p.resource_id.name.lower()
            if "pbr" in name and ("metal" in name or "rough" in name):
                return p.resource_id.url()
        if presets:
            return presets[0].resource_id.url()
    except Exception:
        pass
    return None


TEX_SUFFIX = (".png", ".jpg", ".jpeg", ".tga", ".exr", ".tif", ".tiff", ".bmp", ".psd")


def _flatten_into(out_dir: Path, textures):
    """Painter can nest exports in per-texture-set sub-folders. The hub expects a
    flat 6_Textures, so pull every map up and delete the empty folders. Covers the
    files just written AND anything older exports left behind in sub-folders."""
    moved = 0
    try:
        written = [Path(p) for paths in textures.values() for p in paths]
    except Exception:
        written = []
    try:
        for p in out_dir.rglob("*"):
            if p.is_file() and p.parent != out_dir and p.suffix.lower() in TEX_SUFFIX:
                written.append(p)
    except Exception:
        pass
    seen, uniq = set(), []
    for p in written:
        if str(p).lower() in seen:
            continue
        seen.add(str(p).lower())
        uniq.append(p)
    written = uniq
    for src in written:
        try:
            if not src.is_file() or src.parent == out_dir:
                continue
            dst = out_dir / src.name
            if dst.exists():                      # name clash across texture sets
                dst = out_dir / (src.parent.name + "_" + src.name)
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception:
            continue
    for d in sorted([p for p in out_dir.rglob("*") if p.is_dir()],
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()                             # only succeeds when empty
        except Exception:
            pass
    return moved


def export_textures_to_hub():
    if not substance_painter.project.is_open():
        _warn("No project open.")
        return
    res, err = _active_asset()
    if err:
        _warn(err)
        return
    asset_path, _ = res
    out_dir = asset_path / "6_Textures"
    out_dir.mkdir(parents=True, exist_ok=True)

    preset_url = _find_pbr_preset()
    if not preset_url:
        _warn("No export preset found.")
        return

    try:
        stacks = substance_painter.textureset.all_texture_sets()
        export_list = [{"rootPath": ts.name()} for ts in stacks]
    except Exception as e:
        _warn("Could not list texture sets: " + str(e))
        return

    config = {
        "exportShaderParams": False,
        "exportPath": str(out_dir),
        "defaultExportPreset": preset_url,
        "exportList": export_list,
        "exportParameters": [{
            "parameters": {
                "fileFormat": "png",
                "bitDepth": "8",
                "dithering": True,
                "paddingAlgorithm": "infinite",
            }
        }],
    }
    try:
        result = substance_painter.export.export_project_textures(config)
        if result.status == substance_painter.export.ExportStatus.Success:
            n = sum(len(v) for v in result.textures.values())
            moved = _flatten_into(out_dir, result.textures)
            _tick_stage(asset_path, "Texturing")
            extra = f"\n({moved} moved out of sub-folders)" if moved else ""
            _info(f"Exported {n} maps to:  {out_dir}{extra}\nHub stage 'Texturing' ticked ✓")
        else:
            _warn("Export finished with status: " + str(result.status)
                  + "\n" + (result.message or ""))
    except Exception as e:
        _warn("Export failed: " + str(e))


# ---------------------------------------------------------------- menu

def start_plugin():
    menu = QtWidgets.QMenu("Pipeline Hub")
    a0 = menu.addAction("New Project from Hub  (LP mesh)")
    a0.triggered.connect(new_project_from_hub)
    a1 = menu.addAction("Reload Mesh from Hub  (keep strokes)")
    a1.triggered.connect(reload_mesh_from_hub)
    a2 = menu.addAction("Set HP Bake Mesh  (4_Export/HP)")
    a2.triggered.connect(set_hp_bake_mesh)
    menu.addSeparator()
    a3 = menu.addAction("Save Project → Hub  (5_Substance)")
    a3.triggered.connect(save_project_to_hub)
    a4 = menu.addAction("Export Textures → Hub  (6_Textures)")
    a4.triggered.connect(export_textures_to_hub)
    substance_painter.ui.add_menu(menu)
    plugin_widgets.append(menu)
    log.info("[Pipeline Hub] plugin loaded")


def close_plugin():
    for w in plugin_widgets:
        substance_painter.ui.delete_ui_element(w)
    plugin_widgets.clear()


if __name__ == "__main__":
    start_plugin()
'''

# Blender add-on source, embedded so the hub (or exe) can install it directly
ADDON_SRC = r'''bl_info = {
    "name": "Pipeline Hub Export",
    "author": "Pipeline Hub",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > Pipeline",
    "description": "Export FBX straight into the active Pipeline Hub asset (LP / HP / Final) and tick its stages.",
    "category": "Import-Export",
}

import json
import shutil
from datetime import datetime
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Operator, Panel

# ---------------------------------------------------------------------------
# Shared state with the hub  (~/.pipeline_hub/state.json)
# ---------------------------------------------------------------------------

STATE_FILE = Path.home() / ".pipeline_hub" / "state.json"

UNCAT = "Uncategorized"
ASSET_MARKERS = ("pipeline.json", "1_Ref", "3_Blender", "4_Export", "5_Substance")

# keep enum item lists alive (Blender GC gotcha)
_enum_ref = {"games": [], "cats": [], "assets": []}


def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def local_root():
    r = read_state().get("local_root", "")
    p = Path(r) if r else None
    return p if (p and p.exists()) else None


def is_asset_dir(p: Path) -> bool:
    try:
        return any((p / m).exists() for m in ASSET_MARKERS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Resolve which asset folder we export into
# ---------------------------------------------------------------------------

def resolve_asset_path(context):
    """Returns (asset_path: Path|None, label: str, source: str)."""
    sc = context.scene
    if sc.ph_manual:
        root = local_root()
        if not root:
            return None, "no local root in hub", "manual"
        game = sc.ph_game
        cat = sc.ph_category
        asset = sc.ph_asset
        if not game or not asset:
            return None, "pick game / asset", "manual"
        if cat in ("", UNCAT, "· (root)"):
            p = root / game / asset
        else:
            p = root / game / cat / asset
        if p.exists():
            return p, f"{game} / {cat} / {asset}", "manual"
        return None, "selected asset not found", "manual"

    # auto: whatever is selected in the hub
    active = read_state().get("active")
    if not active:
        return None, "no asset selected in hub", "auto"
    p = Path(active["path"])
    if not p.exists():
        return None, "hub asset missing on disk", "auto"
    label = f"{active['game']} / {active['category']} / {active['name']}"
    return p, label, "auto"


# ---------------------------------------------------------------------------
# Enum callbacks for manual override
# ---------------------------------------------------------------------------

def game_items(self, context):
    root = local_root()
    items = []
    if root:
        for d in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if d.is_dir() and not d.name.startswith((".", "_")):
                items.append((d.name, d.name, ""))
    _enum_ref["games"] = items or [("", "<no games>", "")]
    return _enum_ref["games"]


def category_items(self, context):
    root = local_root()
    items = []
    game = context.scene.ph_game
    if root and game:
        gdir = root / game
        if gdir.exists():
            has_root_assets = False
            for d in sorted(gdir.iterdir(), key=lambda x: x.name.lower()):
                if not d.is_dir() or d.name.startswith((".", "_")):
                    continue
                if is_asset_dir(d):
                    has_root_assets = True
                else:
                    items.append((d.name, d.name, ""))
            if has_root_assets:
                items.insert(0, ("· (root)", "· (root)", "assets directly under the game"))
    _enum_ref["cats"] = items or [("", "<none>", "")]
    return _enum_ref["cats"]


def asset_items(self, context):
    root = local_root()
    items = []
    game = context.scene.ph_game
    cat = context.scene.ph_category
    if root and game:
        base = (root / game) if cat in ("", "· (root)") else (root / game / cat)
        if base.exists():
            for d in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if d.is_dir() and not d.name.startswith((".", "_")) and is_asset_dir(d):
                    items.append((d.name, d.name, ""))
    _enum_ref["assets"] = items or [("", "<no assets>", "")]
    return _enum_ref["assets"]


# ---------------------------------------------------------------------------
# Stage update in the asset's pipeline.json
# ---------------------------------------------------------------------------

def asset_kind(asset_path: Path):
    """'SFX' for sound assets, '3D' otherwise — a sound folder has no mesh slots."""
    try:
        meta = json.loads((asset_path / "pipeline.json").read_text(encoding="utf-8"))
        if meta.get("type") == "SFX":
            return "SFX"
    except Exception:
        pass
    if (asset_path / "3_Session").exists() or (asset_path / "2_Source").exists():
        return "SFX"
    return "3D"


def tick_stage(asset_path: Path, stage: str):
    f = asset_path / "pipeline.json"
    try:
        meta = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        meta = {}
    meta.setdefault("stages", {})
    meta["stages"][stage] = datetime.now().strftime("%d.%m.%Y %H:%M")
    try:
        f.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Export operator
# ---------------------------------------------------------------------------

TARGETS = {
    # key: (subfolder, suffix, stage to tick)
    "LP":    ("4_Export/LP", "_LP",  "Export"),
    "HP":    ("4_Export/HP", "_HP",  "Export"),
    "FINAL": ("8_Final",     "",     "Texturing"),
}

VERSIONS_KEEP = 3


def _rotate_versions(out_file: Path):
    """Before overwriting an export, stash the old one in _versions/ (keep last 3).
    copy2 preserves the original mtime, so 'newest file' logic is never confused."""
    if not out_file.exists():
        return
    vdir = out_file.parent / "_versions"
    vdir.mkdir(exist_ok=True)
    stamp = datetime.fromtimestamp(out_file.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
    dest = vdir / (out_file.stem + "_" + stamp + out_file.suffix)
    if not dest.exists():
        shutil.copy2(out_file, dest)
    old = sorted(vdir.glob(out_file.stem + "_*" + out_file.suffix))
    for p in old[:-VERSIONS_KEEP]:
        try:
            p.unlink()
        except Exception:
            pass


class PH_OT_export(Operator):
    bl_idname = "pipeline_hub.export"
    bl_label = "Pipeline Hub Export"
    bl_options = {"REGISTER"}

    target: EnumProperty(
        items=[("LP", "LP", ""), ("HP", "HP", ""), ("FINAL", "Final", "")],
        default="LP",
    )

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type in {"MESH", "ARMATURE", "EMPTY"}]
        if not sel:
            self.report({"ERROR"}, "Nothing selected to export. Select your mesh(es) first.")
            return {"CANCELLED"}

        asset_path, label, _ = resolve_asset_path(context)
        if asset_path is None:
            self.report({"ERROR"}, f"No target asset: {label}")
            return {"CANCELLED"}
        if asset_kind(asset_path) == "SFX":
            self.report({"ERROR"}, f"'{asset_path.name}' is a sound asset — pick a 3D asset in the hub.")
            return {"CANCELLED"}

        sub, suffix, stage = TARGETS[self.target]
        out_dir = asset_path / sub
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{asset_path.name}{suffix}.fbx"
        try:
            _rotate_versions(out_file)   # keep the last exports recoverable
        except Exception:
            pass

        kwargs = dict(
            filepath=str(out_file),
            use_selection=True,
            global_scale=1.0,
            apply_unit_scale=True,           # "Apply Unit"
            apply_scale_options="FBX_SCALE_NONE",   # "Apply Scalings: All Local"
            axis_forward="-Z",
            axis_up="Y",
            use_space_transform=True,        # "Use Space Transform"
            bake_space_transform=True,       # "Apply Transform" — engines get clean
                                             # transforms instead of a +90° rotated root
            object_types={"MESH", "ARMATURE", "EMPTY"},
            use_mesh_modifiers=True,
            mesh_smooth_type="FACE",
            use_tspace=True,           # tangent space -> clean normal-map bakes in Substance
            add_leaf_bones=False,
            bake_anim=False,
            path_mode="AUTO",
        )
        if self.target == "FINAL":
            # engine-ready: any textures wired into materials get copied next to
            # the fbx AND embedded inside it
            kwargs["path_mode"] = "COPY"
            kwargs["embed_textures"] = True
        try:
            bpy.ops.export_scene.fbx(**kwargs)
        except Exception as e:
            self.report({"ERROR"}, f"FBX export failed: {e}")
            return {"CANCELLED"}

        # Final export: also bundle the Painter maps from 6_Textures — Blender can
        # only embed what materials reference, but the engine needs the PBR set.
        tex_note = ""
        if self.target == "FINAL":
            tex_dir = asset_path / "6_Textures"
            tex_ext = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".tif", ".tiff"}
            copied = 0
            if tex_dir.exists():
                for t in tex_dir.rglob("*"):
                    if t.is_file() and t.suffix.lower() in tex_ext:
                        try:
                            shutil.copy2(t, out_dir / t.name)
                            copied += 1
                        except Exception:
                            pass
            tex_note = f"  ·  {copied} texture(s) bundled" if copied \
                else "  ·  no textures in 6_Textures yet"

        tick_stage(asset_path, stage)
        self.report({"INFO"}, f"{self.target} → {out_file.name}{tex_note}  ·  {label}  (stage: {stage} ✓)")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Viewport snapshot -> asset thumbnail (thumb.png beats 7_Renders in the hub)
# ---------------------------------------------------------------------------

class PH_OT_thumbnail(Operator):
    bl_idname = "pipeline_hub.thumbnail"
    bl_label = "Snapshot Hub Thumbnail"
    bl_description = "OpenGL-render the current viewport to <asset>/thumb.png — shown on the asset's card in Pipeline Hub"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return not bpy.app.background and context.area and context.area.type == "VIEW_3D"

    def execute(self, context):
        asset_path, label, _ = resolve_asset_path(context)
        if asset_path is None:
            self.report({"ERROR"}, f"No target asset: {label}")
            return {"CANCELLED"}
        out = asset_path / "thumb.png"
        rd = context.scene.render
        old_path, old_fmt = rd.filepath, rd.image_settings.file_format
        try:
            rd.filepath = str(out)
            rd.image_settings.file_format = "PNG"
            bpy.ops.render.opengl(write_still=True, view_context=True)
        except Exception as e:
            self.report({"ERROR"}, f"Snapshot failed: {e}")
            return {"CANCELLED"}
        finally:
            rd.filepath, rd.image_settings.file_format = old_path, old_fmt
        self.report({"INFO"}, f"Thumbnail saved → {out}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Sidebar panel
# ---------------------------------------------------------------------------

class PH_PT_panel(Panel):
    bl_label = "Pipeline Hub"
    bl_idname = "PH_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Pipeline"

    def draw(self, context):
        layout = self.layout
        sc = context.scene

        root = local_root()
        if not root:
            box = layout.box()
            box.label(text="Hub not detected", icon="ERROR")
            box.label(text="Open Pipeline Hub once,")
            box.label(text="then select an asset.")
            return

        asset_path, label, source = resolve_asset_path(context)

        box = layout.box()
        if asset_path:
            box.label(text="Target asset", icon="CHECKMARK")
            col = box.column(align=True)
            col.scale_y = 0.85
            col.label(text=label)
            box.label(text=f"({source})", icon="FILE_REFRESH" if source == "auto" else "GREASEPENCIL")
        else:
            box.label(text=label, icon="INFO")

        layout.prop(sc, "ph_manual", text="Manual override")
        if sc.ph_manual:
            col = layout.column(align=True)
            col.prop(sc, "ph_game", text="Game")
            col.prop(sc, "ph_category", text="Cat")
            col.prop(sc, "ph_asset", text="Asset")

        layout.separator()
        sel_n = len([o for o in context.selected_objects if o.type in {"MESH", "ARMATURE", "EMPTY"}])
        layout.label(text=f"{sel_n} object(s) selected", icon="OBJECT_DATA")

        col = layout.column(align=True)
        col.scale_y = 1.3
        enabled = asset_path is not None and sel_n > 0
        col.enabled = enabled
        op = col.operator("pipeline_hub.export", text="Export LP  (bake low-poly)", icon="EXPORT")
        op.target = "LP"
        op = col.operator("pipeline_hub.export", text="Export HP  (bake high-poly)", icon="EXPORT")
        op.target = "HP"
        op = col.operator("pipeline_hub.export", text="Export Final  (textured → Roblox)", icon="CHECKMARK")
        op.target = "FINAL"

        layout.separator()
        row = layout.row()
        row.enabled = asset_path is not None
        row.operator("pipeline_hub.thumbnail", text="Snapshot Hub Thumbnail", icon="RESTRICT_RENDER_OFF")


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

CLASSES = (PH_OT_export, PH_OT_thumbnail, PH_PT_panel)


def register():
    bpy.types.Scene.ph_manual = BoolProperty(
        name="Manual override", default=False,
        description="Export to a hand-picked asset instead of the one selected in the hub")
    bpy.types.Scene.ph_game = EnumProperty(name="Game", items=game_items)
    bpy.types.Scene.ph_category = EnumProperty(name="Category", items=category_items)
    bpy.types.Scene.ph_asset = EnumProperty(name="Asset", items=asset_items)
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.ph_manual
    del bpy.types.Scene.ph_game
    del bpy.types.Scene.ph_category
    del bpy.types.Scene.ph_asset


if __name__ == "__main__":
    register()
'''


# ----------------------------------------------------------------------------
# Asset types — an asset's type decides its folder tree, its pipeline stages and
# which checks apply. A sound effect has no business owning a 2_ZBrush folder.
# ----------------------------------------------------------------------------

ASSET_TYPES = {
    "3D": {
        "label": "3D Model", "icon": "◆",
        "folders": ["1_Ref", "2_ZBrush", "3_Blender", "4_Export/HP", "4_Export/LP",
                    "5_Substance", "6_Textures", "7_Renders", "8_Final"],
        "stages": ["Idea", "Sculpt", "LowPoly", "UV", "Export", "Bake", "Texturing", "Done"],
        "categories": ["Characters", "Weapons", "Props", "Materials", "Environment", "Vehicles"],
    },
    "SFX": {
        "label": "Sound FX", "icon": "♪",
        "folders": ["1_Ref", "2_Source", "3_Session", "4_Bounce", "5_Final"],
        "stages": ["Idea", "Source", "Design", "Edit", "Mix", "Master", "Done"],
        "categories": ["UI", "Weapons", "Footsteps", "Ambience", "Creatures",
                       "Impacts", "Music"],
    },
}
DEFAULT_TYPE = "3D"

STAGES = ASSET_TYPES["3D"]["stages"]        # legacy alias (Blender add-on state)
SUBFOLDERS = ASSET_TYPES["3D"]["folders"]   # legacy alias

# collaborative review workflow: pipeline.json carries the state, so it syncs
# with the asset and every team member sees the same review status
REVIEW_STATES = ["none", "pending", "approved", "changes"]

PRESET_CATEGORIES = ASSET_TYPES["3D"]["categories"]
GENERAL_GAME = "General"
UNCAT = "Uncategorized"

BLEND_EXT = {".blend"}
FBX_EXT = {".fbx", ".obj"}
SPP_EXT = {".spp"}
ZB_EXT = {".zpr", ".ztl"}

AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".aiff", ".aif"}
SESSION_EXT = {".rpp", ".als", ".flp", ".ptx", ".aup3", ".band", ".reapeaks",
               ".logicx", ".cpr", ".sesx", ".au3"}
ROBLOX_AUDIO_EXT = {".mp3", ".ogg"}          # what Roblox actually accepts
ROBLOX_AUDIO_MAX_MB = 20
ROBLOX_AUDIO_MAX_SEC = 420                   # 7 minutes

SYNC_IGNORE_DIRS = {".git", "__pycache__", "_versions"}
SYNC_IGNORE_SUFFIX = {".blend1", ".blend2", ".tmp", ".autosave"}

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
THUMB_DIR = Path.home() / ".pipeline_hub" / "thumbs"


# ----------------------------------------------------------------------------
# Library — flat image collections (alphas, tiling textures, Roblox face PNGs).
# Not projects: no stages, no pipeline. Just browse / rename / hand to other apps.
# Lives in <local_root>/_Library by default; the scanner already skips "_" dirs.
# ----------------------------------------------------------------------------

LIB_DIR_NAME = "_Library"
LIB_EXT = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp", ".tif", ".tiff",
           ".psd", ".exr", ".svg", ".gif"}
LIB_STARTER = ["Alphas", "Tiling", "Faces/Eyes", "Faces/Mouths", "Faces/Extras",
               "Decals", "Reference"]
LIB_THUMB_DIR = STATE_DIR / "libthumbs"
_LIB_MEM: dict = {}


def lib_thumb(path: Path, box=224):
    """Small data-URI preview for a library image (cached on mtime)."""
    if Image is None or path.suffix.lower() in {".exr", ".svg"}:
        return None
    try:
        m = path.stat().st_mtime
    except Exception:
        return None
    key = str(path).lower()
    hit = _LIB_MEM.get(key)
    if hit and hit[0] == m:
        return hit[1]
    import base64
    import hashlib
    uri = None
    try:
        LIB_THUMB_DIR.mkdir(parents=True, exist_ok=True)
        cache = LIB_THUMB_DIR / (hashlib.md5(key.encode()).hexdigest()[:16] + ".png")
        if not cache.exists() or cache.stat().st_mtime < m:
            im = Image.open(path)
            im = im.convert("RGBA") if im.mode not in ("RGB", "RGBA") else im
            im.thumbnail((box, box))
            im.save(cache, "PNG")           # PNG keeps the alpha of face/alpha art
        uri = "data:image/png;base64," + base64.b64encode(cache.read_bytes()).decode()
    except Exception:
        uri = None
    _LIB_MEM[key] = (m, uri)
    return uri


def lib_dims(path: Path):
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            return f"{im.size[0]}×{im.size[1]}"
    except Exception:
        return None


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def safe_name(name, keep_ext_from=None):
    """Sanitize a user-typed file name; re-attach the original extension if dropped."""
    name = (name or "").strip().strip(". ")
    if not name or any(c in name for c in '\\/:*?"<>|'):
        return None
    if keep_ext_from is not None:
        old = Path(keep_ext_from).suffix
        if old and not name.lower().endswith(old.lower()):
            name += old
    return name


def type_of(asset_path: Path, meta=None):
    """Asset's type — from pipeline.json, else inferred from its folders."""
    meta = meta if meta is not None else load_meta(asset_path)
    t = meta.get("type")
    if t in ASSET_TYPES:
        return t
    try:
        if (asset_path / "3_Session").exists() or (asset_path / "2_Source").exists():
            return "SFX"
    except Exception:
        pass
    return DEFAULT_TYPE


def type_info(t):
    return ASSET_TYPES.get(t, ASSET_TYPES[DEFAULT_TYPE])


def wav_peaks(path: Path, buckets=76):
    """Peak envelope of a .wav as floats 0..1 — stdlib only (wave + array).
    Returns None for unreadable files or formats we don't decode."""
    import wave
    from array import array as _arr
    try:
        with wave.open(str(path), "rb") as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if n <= 0 or ch <= 0:
                return None
            n = min(n, 4_000_000)               # cap work on long files
            raw = w.readframes(n)
    except Exception:
        return None
    try:
        if sw == 1:                              # unsigned 8-bit
            a = _arr("b", bytes((b - 128) & 0xFF for b in raw))
            peak = 128.0
        elif sw == 2:
            a = _arr("h")
            a.frombytes(raw[:len(raw) // 2 * 2])
            peak = 32768.0
        elif sw == 4:
            a = _arr("i")
            a.frombytes(raw[:len(raw) // 4 * 4])
            peak = 2147483648.0
        elif sw == 3:                            # 24-bit: take the top two bytes
            a = _arr("h", bytes(b for i in range(0, len(raw) - 2, 3)
                                for b in raw[i + 1:i + 3]))
            peak = 32768.0
        else:
            return None
    except Exception:
        return None
    if not len(a):
        return None
    mono = a[::ch] if ch > 1 else a
    step = max(1, len(mono) // buckets)
    out = []
    for i in range(0, len(mono), step):
        chunk = mono[i:i + step]
        if not len(chunk):
            continue
        out.append(min(1.0, max(abs(min(chunk)), abs(max(chunk))) / peak))
        if len(out) >= buckets:
            break
    if not out:
        return None
    top = max(out) or 1.0
    return [round(v / top, 3) for v in out]      # normalized so quiet clips still read


_WAVE_MEM: dict = {}


def waveform_of(asset_path: Path):
    """Peaks for the asset's newest final/bounce wav, cached on mtime."""
    src = newest(asset_path, {".wav"}, "5_Final", fallback=False) \
        or newest(asset_path, {".wav"}, "4_Bounce", fallback=False) \
        or newest(asset_path, {".wav"}, "2_Source", fallback=False)
    if src is None:
        return None
    try:
        key, m = str(src), src.stat().st_mtime
    except Exception:
        return None
    hit = _WAVE_MEM.get(key)
    if hit and hit[0] == m:
        return hit[1]
    peaks = wav_peaks(src)
    _WAVE_MEM[key] = (m, peaks)
    return peaks


def audio_duration(path: Path):
    """Seconds for a .wav (stdlib); None for compressed formats."""
    if path.suffix.lower() != ".wav":
        return None
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            fr = w.getframerate()
            return round(w.getnframes() / fr, 2) if fr else None
    except Exception:
        return None


def find_thumb_source(asset_path: Path):
    """thumb.* in asset root wins; else newest image in 7_Renders."""
    for p in asset_path.glob("thumb.*"):
        if p.suffix.lower() in IMG_EXT:
            return p
    rd = asset_path / "7_Renders"
    best, best_t = None, -1.0
    if rd.exists():
        for p in rd.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMG_EXT:
                t = p.stat().st_mtime
                if t > best_t:
                    best, best_t = p, t
    return best


# in-memory data-URI cache: get_state() runs on every UI action AND every 7 s
# in the background — without this, every call re-reads + re-base64s every thumb
_THUMB_MEM: dict = {}


def thumb_data_uri(asset_path: Path):
    """Return a small base64 jpeg data-URI for the asset, cached; None if no image/PIL."""
    if Image is None:
        return None
    src = find_thumb_source(asset_path)
    if src is None:
        return None
    try:
        src_m = src.stat().st_mtime
    except Exception:
        return None
    memkey = str(asset_path)
    hit = _THUMB_MEM.get(memkey)
    if hit and hit[0] == src_m:
        return hit[1]
    import base64
    import hashlib
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(str(asset_path).encode()).hexdigest()[:16]
    cache = THUMB_DIR / f"{key}.jpg"
    try:
        if not cache.exists() or cache.stat().st_mtime < src_m:
            im = Image.open(src).convert("RGB")
            im.thumbnail((192, 192))
            im.save(cache, "JPEG", quality=72)
        uri = "data:image/jpeg;base64," + base64.b64encode(cache.read_bytes()).decode()
        _THUMB_MEM[memkey] = (src_m, uri)
        return uri
    except Exception:
        return None

ASSET_MARKERS = {"pipeline.json", "1_Ref", "2_ZBrush", "3_Blender", "4_Export",
                 "5_Substance", "2_Source", "3_Session", "4_Bounce", "5_Final"}


# ----------------------------------------------------------------------------
# Config / metadata
# ----------------------------------------------------------------------------

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"local_root": "", "cloud_root": ""}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def default_meta():
    return {"type": DEFAULT_TYPE, "stages": {}, "notes": "", "created": "",
            "last_sync": "", "todos": [], "review": default_review()}


def default_review():
    return {"state": "none", "by": "", "note": "", "time": "", "history": []}


def review_of(meta):
    """Normalized review dict — tolerates old pipeline.json files without one."""
    r = default_review()
    saved = meta.get("review")
    if isinstance(saved, dict):
        r.update({k: saved[k] for k in r if k in saved})
    if r["state"] not in REVIEW_STATES:
        r["state"] = "none"
    if not isinstance(r["history"], list):
        r["history"] = []
    return r


def load_meta(asset_path: Path):
    f = asset_path / "pipeline.json"
    meta = default_meta()
    if f.exists():
        try:
            meta.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return meta


def save_meta(asset_path: Path, meta):
    (asset_path / "pipeline.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# File scanning / status
# ----------------------------------------------------------------------------

def newest(asset_path: Path, exts, subdir=None, fallback=True):
    best, best_t = None, -1.0
    root = asset_path / subdir if subdir else asset_path
    if not root.exists():
        if not fallback:
            return None
        root = asset_path
    for p in root.rglob("*"):
        if "_versions" in p.parts:      # rotated export backups don't count
            continue
        if p.is_file() and p.suffix.lower() in exts:
            t = p.stat().st_mtime
            if t > best_t:
                best, best_t = p, t
    return best


def file_info(p: Path | None):
    if p is None:
        return None
    return {"name": p.name,
            "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            "path": str(p)}


def export_status(asset_path: Path):
    blend = newest(asset_path, BLEND_EXT, "3_Blender")
    lp = newest(asset_path, FBX_EXT, "4_Export/LP")
    if blend is None:
        return ("no .blend yet", "dim")
    if lp is None:
        return ("no LP export yet", "warn")
    if blend.stat().st_mtime > lp.stat().st_mtime + 5:
        hrs = (blend.stat().st_mtime - lp.stat().st_mtime) / 3600
        t = f"{hrs:.1f} h" if hrs < 48 else f"{hrs/24:.1f} d"
        return (f"LP export out of date — blend newer by {t}", "bad")
    return ("LP export up to date", "ok")


def is_asset_dir(p: Path) -> bool:
    try:
        return any((p / m).exists() for m in ASSET_MARKERS)
    except Exception:
        return False


# PBR set expected in 6_Textures — keyword aliases per map
TEX_KEYWORDS = {
    "BaseColor": ("basecolor", "base_color", "albedo", "diffuse", "color"),
    "Normal":    ("normal", "nrm", "_nor"),
    "Roughness": ("roughness", "rough"),
    "Metallic":  ("metallic", "metalness", "metal"),
}
TEX_EXT = {".png", ".jpg", ".jpeg", ".tga", ".exr", ".tif", ".tiff"}


def check_textures(asset_path: Path):
    """Which PBR maps exist in 6_Textures? -> {found:[], missing:[], count:int}"""
    tex_dir = asset_path / "6_Textures"
    names = []
    if tex_dir.exists():
        names = [p.name.lower() for p in tex_dir.rglob("*")
                 if p.is_file() and p.suffix.lower() in TEX_EXT]
    found, missing = [], []
    for map_name, keys in TEX_KEYWORDS.items():
        (found if any(k in n for n in names for k in keys) else missing).append(map_name)
    return {"found": found, "missing": missing, "count": len(names),
            "complete": not missing and bool(names)}


# ----------------------------------------------------------------------------
# Roblox preflight — validate an asset against Roblox import limits without
# opening Blender: tri count straight from the binary FBX, texture sizes via PIL
# ----------------------------------------------------------------------------

ROBLOX_TRI_LIMIT = 10000     # MeshPart upload cap
ROBLOX_TEX_MAX = 1024        # Roblox downscales anything larger


def fbx_tri_count(path: Path):
    """Triangle count of a binary FBX (summed over all meshes), None if unparsable.
    Walks the node tree for PolygonVertexIndex arrays; a polygon's last index is
    bitwise-negated, so: tris = n_indices - 2 * n_polygons."""
    import struct
    import zlib
    from array import array as _array
    SCALAR = {b"Y": 2, b"C": 1, b"I": 4, b"F": 4, b"L": 8, b"D": 8}
    ELEM = {b"i": 4, b"f": 4, b"b": 1, b"l": 8, b"d": 8}
    try:
        data = path.read_bytes()
        if not data.startswith(b"Kaydara FBX Binary"):
            return None
        ver = struct.unpack_from("<I", data, 23)[0]
        head = "<QQQB" if ver >= 7500 else "<IIIB"
        hsize = struct.calcsize(head)
        tris = 0

        def walk(pos, end):
            nonlocal tris
            while pos < end and pos + hsize <= len(data):
                end_off, n_props, plen, nlen = struct.unpack_from(head, data, pos)
                if end_off == 0:          # NULL record: end of this child list
                    return
                name = data[pos + hsize:pos + hsize + nlen]
                p = pos + hsize + nlen
                if name == b"PolygonVertexIndex":
                    q = p
                    for _ in range(n_props):
                        t = data[q:q + 1]
                        q += 1
                        if t in ELEM:
                            alen, enc, clen = struct.unpack_from("<III", data, q)
                            q += 12
                            if t == b"i":
                                buf = zlib.decompress(data[q:q + clen]) if enc \
                                    else data[q:q + alen * 4]
                                idx = _array("i")
                                idx.frombytes(buf[:alen * 4])
                                negs = sum(1 for v in idx if v < 0)
                                tris += len(idx) - 2 * negs
                            q += clen if enc else alen * ELEM[t]
                        elif t in (b"S", b"R"):
                            q += 4 + struct.unpack_from("<I", data, q)[0]
                        elif t in SCALAR:
                            q += SCALAR[t]
                        else:
                            return    # unknown property type: bail out safely
                elif p + plen < end_off:
                    walk(p + plen, end_off)   # recurse into children
                pos = end_off
        walk(27, len(data))
        return tris if tris > 0 else None
    except Exception:
        return None


def audio_status(asset_path: Path):
    """SFX equivalent of export_status: is the final render behind the session?"""
    session = newest(asset_path, SESSION_EXT, "3_Session", fallback=False)
    final = newest(asset_path, AUDIO_EXT, "5_Final", fallback=False)
    bounce = newest(asset_path, AUDIO_EXT, "4_Bounce", fallback=False)
    if session is None and bounce is None and final is None:
        return ("no audio yet", "dim")
    if final is None:
        return ("no final render yet", "warn")
    if session is not None and session.stat().st_mtime > final.stat().st_mtime + 5:
        hrs = (session.stat().st_mtime - final.stat().st_mtime) / 3600
        t = f"{hrs:.1f} h" if hrs < 48 else f"{hrs/24:.1f} d"
        return (f"final render out of date — session newer by {t}", "bad")
    if final.suffix.lower() not in ROBLOX_AUDIO_EXT:
        return (f"final is {final.suffix} — Roblox needs .ogg/.mp3", "warn")
    return ("final render up to date", "ok")


def audio_preflight(asset_path: Path):
    """Check a sound against what Roblox actually accepts on upload."""
    checks = []

    def add(label, level, info):
        checks.append({"label": label, "level": level, "info": info})

    final = newest(asset_path, AUDIO_EXT, "5_Final", fallback=False)
    src = "5_Final"
    if final is None:
        final = newest(asset_path, AUDIO_EXT, "4_Bounce", fallback=False)
        src = "4_Bounce"
    if final is None:
        add("Audio file", "bad", "nothing in 5_Final or 4_Bounce — bounce your mix first")
        return {"ok": True, "level": "bad", "checks": checks}

    ext = final.suffix.lower()
    if ext in ROBLOX_AUDIO_EXT:
        add("Format", "ok", f"{final.name} ({src}) — {ext} uploads directly")
    else:
        add("Format", "warn",
            f"{final.name} is {ext} — Roblox accepts .ogg / .mp3, convert before upload")

    try:
        mb = final.stat().st_size / 1e6
        if mb > ROBLOX_AUDIO_MAX_MB:
            add("File size", "bad", f"{mb:.1f} MB — over the {ROBLOX_AUDIO_MAX_MB} MB limit")
        else:
            add("File size", "ok", f"{mb:.2f} MB (limit {ROBLOX_AUDIO_MAX_MB} MB)")
    except Exception:
        pass

    dur = audio_duration(final)
    if dur is None:
        add("Length", "ok", "not read for compressed formats — fine unless over 7 min")
    elif dur > ROBLOX_AUDIO_MAX_SEC:
        add("Length", "bad", f"{dur/60:.1f} min — over the 7 min limit")
    else:
        add("Length", "ok", f"{dur:.2f} s")

    sess = newest(asset_path, SESSION_EXT, "3_Session", fallback=False)
    if sess is None:
        add("Session", "warn", "no DAW session in 3_Session — edits won't be reproducible")
    else:
        add("Session", "ok", f"{sess.name} kept in 3_Session")

    worst = "ok"
    for c in checks:
        if c["level"] == "bad":
            worst = "bad"
            break
        if c["level"] == "warn":
            worst = "warn"
    return {"ok": True, "level": worst, "checks": checks}


def roblox_preflight(asset_path: Path):
    """Check mesh + textures against Roblox limits. Levels: ok / warn / bad."""
    checks = []

    def add(label, level, info):
        checks.append({"label": label, "level": level, "info": info})

    fbx = newest(asset_path, FBX_EXT, "8_Final", fallback=False)
    src = "8_Final"
    if fbx is None:
        fbx = newest(asset_path, FBX_EXT, "4_Export/LP", fallback=False)
        src = "4_Export/LP"
    if fbx is None:
        add("Mesh", "bad", "no FBX in 8_Final or 4_Export/LP — export one first")
    else:
        tris = fbx_tri_count(fbx)
        if tris is None:
            add("Mesh", "warn", f"{fbx.name} ({src}) — could not read triangle count")
        elif tris > ROBLOX_TRI_LIMIT:
            add("Mesh", "bad",
                f"{fbx.name} — {tris:,} tris, over the {ROBLOX_TRI_LIMIT:,} MeshPart cap")
        else:
            add("Mesh", "ok", f"{fbx.name} ({src}) — {tris:,} tris (cap {ROBLOX_TRI_LIMIT:,})")

    tex_dir, tex_src = asset_path / "8_Final", "8_Final"
    texs = [p for p in tex_dir.rglob("*") if p.is_file() and p.suffix.lower() in TEX_EXT] \
        if tex_dir.exists() else []
    if not texs:
        tex_dir, tex_src = asset_path / "6_Textures", "6_Textures"
        texs = [p for p in tex_dir.rglob("*") if p.is_file() and p.suffix.lower() in TEX_EXT] \
            if tex_dir.exists() else []
    if not texs:
        add("Textures", "bad", "no texture files found — export from Painter first")
    else:
        if Image is None:
            add("Texture size", "warn", "Pillow not available — sizes not checked")
        else:
            over = []
            for t in texs:
                try:
                    with Image.open(t) as im:
                        w, h = im.size
                    if w > ROBLOX_TEX_MAX or h > ROBLOX_TEX_MAX:
                        over.append(f"{t.name} ({w}×{h})")
                except Exception:
                    pass
            if over:
                add("Texture size", "warn",
                    f"{len(over)} over {ROBLOX_TEX_MAX}px (Roblox downscales): "
                    + ", ".join(over[:3]) + ("…" if len(over) > 3 else ""))
            else:
                add("Texture size", "ok",
                    f"{len(texs)} file(s) in {tex_src}, all ≤ {ROBLOX_TEX_MAX}px")
        names = [t.name.lower() for t in texs]
        missing = [m for m, keys in TEX_KEYWORDS.items()
                   if not any(k in n for n in names for k in keys)]
        if missing:
            add("PBR maps", "warn", "missing: " + ", ".join(missing))
        else:
            add("PBR maps", "ok", "BaseColor, Normal, Roughness, Metallic present")

    worst = "ok"
    for c in checks:
        if c["level"] == "bad":
            worst = "bad"
            break
        if c["level"] == "warn":
            worst = "warn"
    return {"ok": True, "level": worst, "checks": checks}


def final_pack_status(asset_path: Path):
    """Is 8_Final actually engine-ready? (fbx present + textures bundled next to it)"""
    d = asset_path / "8_Final"
    fbx = newest(asset_path, FBX_EXT, "8_Final", fallback=False)
    n_tex = 0
    if d.exists():
        n_tex = sum(1 for p in d.rglob("*")
                    if p.is_file() and p.suffix.lower() in TEX_EXT)
    return {"fbx": fbx is not None, "tex": n_tex}


# ----------------------------------------------------------------------------
# Roblox dev journey — lifetime Robux earned drives a rank ladder that ends at
# the milestone every Roblox dev chases: the first real DevEx payout.
# Stored in ~/.pipeline_hub/earnings.json (survives rebuilds, never cloud-synced).
# ----------------------------------------------------------------------------

RANKS = [
    ("Wood", 0), ("Stone", 500), ("Bronze", 2500), ("Silver", 10000),
    ("Gold", 30000), ("Platinum", 100000), ("Emerald", 250000),
    ("Diamond", 500000), ("Master", 1000000), ("Elite", 2000000),
    ("Champion", 3500000), ("Grandmaster", 5000000), ("Mythic", 8000000),
    ("Titan", 12000000), ("Legend", 20000000),
]

EARNINGS_FILE = STATE_DIR / "earnings.json"
TIMELOG_FILE = STATE_DIR / "timelog.json"

DEVEX_USD_PER_ROBUX = 0.0035          # Roblox DevEx rate: 100,000 R$ ≈ $350

# process name (lowercase) -> display name, for automatic time tracking
TRACKED_APPS = {
    "blender.exe": "Blender",
    "zbrush.exe": "ZBrush",
    "adobe substance 3d painter.exe": "Substance Painter",
    "substance 3d painter.exe": "Substance Painter",
    "painter.exe": "Substance Painter",
    "adobe substance 3d designer.exe": "Substance Designer",
    "cinema 4d.exe": "Cinema 4D",
}
TRACK_INTERVAL_S = 60                  # poll once a minute -> 1 minute credited


def running_process_names():
    """Lowercase names of running processes (Windows, via ctypes — no extra deps)."""
    if not sys.platform.startswith("win"):
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return set()
        names = set()
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = k32.Process32First(snap, ctypes.byref(pe))
            while ok:
                names.add(pe.szExeFile.decode("latin-1", "ignore").lower())
                ok = k32.Process32Next(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return names
    except Exception:
        return set()


def load_timelog():
    try:
        d = json.loads(TIMELOG_FILE.read_text(encoding="utf-8"))
        if isinstance(d.get("days"), dict):
            return d
    except Exception:
        pass
    return {"days": {}}


def save_timelog(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TIMELOG_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def rank_for(total):
    cur = RANKS[0][0]
    for name, thr in RANKS:
        if total >= thr:
            cur = name
    return cur


def load_earnings():
    try:
        d = json.loads(EARNINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(d.get("entries"), list):
            return d
    except Exception:
        pass
    return {"entries": []}


def save_earnings(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EARNINGS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def make_asset_tree(base: Path, game: str, category: str, asset: str,
                    atype: str = DEFAULT_TYPE) -> Path:
    target = base / game / category / asset
    target.mkdir(parents=True, exist_ok=False)
    for sf in type_info(atype)["folders"]:
        (target / sf).mkdir(parents=True, exist_ok=True)
    return target


def sync_asset_files(local_asset: Path, cloud_asset: Path):
    copied, skipped, errors = 0, 0, []
    for src in local_asset.rglob("*"):
        rel = src.relative_to(local_asset)
        if any(part in SYNC_IGNORE_DIRS for part in rel.parts):
            continue
        if src.suffix.lower() in SYNC_IGNORE_SUFFIX:
            continue
        dst = cloud_asset / rel
        try:
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime - 2:
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return copied, skipped, errors


def open_in_os(p: Path):
    if sys.platform.startswith("win"):
        os.startfile(str(p))  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


# ----------------------------------------------------------------------------
# API exposed to the web UI
# ----------------------------------------------------------------------------

class Api:
    def __init__(self):
        self.cfg = load_config()
        self._lock = threading.Lock()
        # restore last selected asset so the Blender/Painter plugins keep
        # working after a hub restart instead of seeing "active": null
        self._active = None
        la = self.cfg.get("last_active")
        if isinstance(la, dict) and la.get("path"):
            try:
                if Path(la["path"]).exists():
                    self._active = la
            except Exception:
                pass
        self._write_state()
        self._start_time_tracker()

    # ---------- helpers ----------

    def _write_state(self):
        """Publish roots + active asset so the Blender add-on can find them."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "local_root": self.cfg.get("local_root", ""),
                "cloud_root": self.cfg.get("cloud_root", ""),
                "active": self._active,
                "subfolders": SUBFOLDERS,
                "stages": STAGES,
                "updated": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            }
            STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _root(self) -> Path | None:
        r = self.cfg.get("local_root", "")
        p = Path(r) if r else None
        return p if p and p.exists() else None

    def _who(self):
        return (self.cfg.get("user_name", "") or "").strip() \
            or os.environ.get("USERNAME", "") or "anonymous"

    def _asset_path(self, game, category, name) -> Path | None:
        root = self._root()
        if not root:
            return None
        p = (root / game / name) if category == UNCAT else (root / game / category / name)
        return p if p.exists() else None

    def _asset_payload(self, path: Path, game: str, category: str):
        meta = load_meta(path)
        atype = type_of(path, meta)
        info = type_info(atype)
        steps = info["stages"]
        tex, wave, final_pack = None, None, None

        if atype == "SFX":
            st_text, st_level = audio_status(path)
            files = [
                {"label": "Reference", "sub": "1_Ref",
                 "file": file_info(newest(path, AUDIO_EXT, "1_Ref", fallback=False))},
                {"label": "Source audio", "sub": "2_Source",
                 "file": file_info(newest(path, AUDIO_EXT, "2_Source", fallback=False))},
                {"label": "DAW session", "sub": "3_Session",
                 "file": file_info(newest(path, SESSION_EXT, "3_Session", fallback=False))},
                {"label": "Bounce", "sub": "4_Bounce",
                 "file": file_info(newest(path, AUDIO_EXT, "4_Bounce", fallback=False))},
                {"label": "Final audio", "sub": "5_Final",
                 "file": file_info(newest(path, AUDIO_EXT, "5_Final", fallback=False))},
            ]
            wave = waveform_of(path)
        else:
            st_text, st_level = export_status(path)
            files = [
                {"label": "ZBrush sculpt", "sub": "2_ZBrush", "file": file_info(newest(path, ZB_EXT, "2_ZBrush"))},
                {"label": "Blender file", "sub": "3_Blender", "file": file_info(newest(path, BLEND_EXT, "3_Blender"))},
                {"label": "HP bake mesh", "sub": "4_Export/HP", "file": file_info(newest(path, FBX_EXT, "4_Export/HP"))},
                {"label": "LP bake mesh", "sub": "4_Export/LP", "file": file_info(newest(path, FBX_EXT, "4_Export/LP"))},
                {"label": "Substance file", "sub": "5_Substance", "file": file_info(newest(path, SPP_EXT, "5_Substance"))},
                {"label": "Textures", "sub": "6_Textures", "file": None},
                {"label": "Final FBX", "sub": "8_Final", "file": file_info(newest(path, FBX_EXT, "8_Final", fallback=False))},
            ]
            tex = check_textures(path)
            final_pack = final_pack_status(path)

        for f in files:
            f["folder"] = str(path / f["sub"]) if (path / f["sub"]).exists() else None

        cur = "—"
        for s in reversed(steps):
            if meta["stages"].get(s):
                cur = s
                break
        # auto-tick Texturing once the PBR set is complete (3D only, idempotent)
        if tex and tex["complete"] and not meta["stages"].get("Texturing"):
            with self._lock:
                meta["stages"]["Texturing"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                save_meta(path, meta)
            for s in reversed(steps):
                if meta["stages"].get(s):
                    cur = s
                    break
        return {
            "game": game, "category": category, "name": path.name, "path": str(path),
            "type": atype, "type_label": info["label"], "icon": info["icon"],
            "steps": steps,
            "stages": meta["stages"], "current": cur,
            "progress": sum(1 for s in steps if meta["stages"].get(s)) / len(steps),
            "status": st_text, "status_level": st_level, "tex": tex,
            "todos": meta.get("todos", []), "notes": meta.get("notes", ""),
            "last_sync": meta.get("last_sync", ""), "files": files,
            "review": review_of(meta),
            "final": final_pack, "wave": wave,
            "thumb": thumb_data_uri(path),
        }

    # ---------- state ----------

    def get_state(self):
        root = self._root()
        games = []
        if root:
            for game_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not game_dir.is_dir() or game_dir.name.startswith((".", "_")):
                    continue
                cats = {}
                for child in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
                    if not child.is_dir() or child.name.startswith((".", "_")):
                        continue
                    if is_asset_dir(child):  # legacy: asset directly under game
                        try:
                            cats.setdefault(UNCAT, []).append(
                                self._asset_payload(child, game_dir.name, UNCAT))
                        except Exception:
                            pass
                    else:  # category folder
                        assets = []
                        for a in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                            if a.is_dir() and not a.name.startswith((".", "_")):
                                try:
                                    assets.append(self._asset_payload(a, game_dir.name, child.name))
                                except Exception:
                                    pass
                        cats.setdefault(child.name, []).extend(assets)
                games.append({"name": game_dir.name,
                              "categories": [{"name": c, "assets": al} for c, al in cats.items()]})
        used = {c["name"] for g in games for c in g["categories"] if c["name"] != UNCAT}
        all_cats = sorted(used | set(PRESET_CATEGORIES), key=str.lower)
        cats_by_type = {t: sorted(set(v["categories"]) | {c["name"] for g in games
                                                          for c in g["categories"]
                                                          for a in c["assets"]
                                                          if a["type"] == t and c["name"] != UNCAT},
                                  key=str.lower)
                        for t, v in ASSET_TYPES.items()}
        lr = self.cfg.get("local_root", "").lower().replace("\\", "/")
        trap = any(k in lr for k in ("onedrive", "google drive", "googledrive", "dropbox", "icloud"))
        return {"local_root": self.cfg.get("local_root", ""),
                "cloud_root": self.cfg.get("cloud_root", ""),
                "local_trap": trap,
                "user_name": self.cfg.get("user_name", ""),
                "discord_webhook": self.cfg.get("discord_webhook", ""),
                "library_root": self.cfg.get("library_root", ""),
                "stages": STAGES, "games": games,
                "types": {t: {"label": v["label"], "icon": v["icon"],
                              "stages": v["stages"]} for t, v in ASSET_TYPES.items()},
                "categories_by_type": cats_by_type,
                "game_names": [g["name"] for g in games],
                "categories": all_cats}

    def set_active(self, game, category, name):
        """Record the clicked asset so the Blender add-on / Painter plugin target it."""
        p = self._asset_path(game, category, name)
        if not p:
            return False
        self._active = {"game": game, "category": category, "name": p.name, "path": str(p)}
        self.cfg["last_active"] = self._active
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._write_state()
        return True

    def get_asset(self, game, category, name):
        if not self.set_active(game, category, name):
            return None
        p = self._asset_path(game, category, name)
        return self._asset_payload(p, game, category)

    # ---------- roots ----------

    def pick_local_root(self):
        res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            self.cfg["local_root"] = res[0]
            save_config(self.cfg)
            self._write_state()
        return self.cfg["local_root"]

    def pick_cloud_root(self):
        res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            self.cfg["cloud_root"] = res[0]
            save_config(self.cfg)
            self._write_state()
        return self.cfg["cloud_root"]

    # ---------- actions ----------

    def create_asset(self, game, category, name, atype=DEFAULT_TYPE):
        atype = atype if atype in ASSET_TYPES else DEFAULT_TYPE
        fallback_cat = "Props" if atype == "3D" else "SFX"
        game = (game or GENERAL_GAME).strip().replace(" ", "_") or GENERAL_GAME
        category = (category or fallback_cat).strip().replace(" ", "_") or fallback_cat
        name = (name or "").strip().replace(" ", "_")
        if not name:
            return {"ok": False, "error": "Asset name is empty."}
        root = self._root()
        if not root:
            return {"ok": False, "error": "Local root not set."}
        if (root / game / category / name).exists():
            return {"ok": False, "error": f"{game}/{category}/{name} already exists."}
        try:
            target = make_asset_tree(root, game, category, name, atype)
            meta = default_meta()
            meta["type"] = atype
            meta["stages"]["Idea"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            meta["created"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_meta(target, meta)
        except Exception as e:
            return {"ok": False, "error": f"Local creation failed: {e}"}
        warn = ""
        cloud = self.cfg.get("cloud_root", "")
        if cloud and Path(cloud).exists():
            try:
                if not (Path(cloud) / game / category / name).exists():
                    make_asset_tree(Path(cloud), game, category, name, atype)
            except Exception as e:
                warn = f"Cloud creation failed: {e}"
        return {"ok": True, "warn": warn, "game": game, "category": category,
                "name": name, "type": atype}

    def convert_asset(self, game, category, name, atype):
        """Change an asset's type — adds the missing folders, keeps everything."""
        if atype not in ASSET_TYPES:
            return {"ok": False, "error": "Unknown asset type."}
        p = self._asset_path(game, category, name)
        if not p:
            return {"ok": False, "error": "Asset not found."}
        try:
            with self._lock:
                meta = load_meta(p)
                meta["type"] = atype
                meta["stages"] = {k: v for k, v in meta.get("stages", {}).items()
                                  if k in type_info(atype)["stages"]}
                save_meta(p, meta)
            for sf in type_info(atype)["folders"]:
                (p / sf).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"Could not convert: {e}"}
        return {"ok": True, "type": atype}

    def create_assets_batch(self, game, category, names_text, atype=DEFAULT_TYPE):
        """Create many assets at once — one per line. Lines may override the
        defaults: 'Name', 'Category/Name', or 'Game/Category/Name'."""
        lines = [l.strip() for l in (names_text or "").splitlines()]
        lines = [l for l in lines if l and not l.startswith("#")]
        if not lines:
            return {"ok": False, "error": "No names given."}
        created, failed = [], []
        for line in lines:
            parts = [p.strip() for p in line.replace("\\", "/").split("/") if p.strip()]
            if len(parts) == 1:
                g, c, n = game, category, parts[0]
            elif len(parts) == 2:
                g, c, n = game, parts[0], parts[1]
            else:
                g, c, n = parts[0], parts[1], "_".join(parts[2:])
            r = self.create_asset(g, c, n, atype)
            (created if r.get("ok") else failed).append(
                n if r.get("ok") else f"{n} ({r.get('error','?')})")
        fallback_cat = "Props" if atype == "3D" else "SFX"
        return {"ok": True, "created": created, "failed": failed, "type": atype,
                "game": (game or GENERAL_GAME).strip().replace(" ", "_") or GENERAL_GAME,
                "category": (category or fallback_cat).strip().replace(" ", "_") or fallback_cat}

    def toggle_stage(self, game, category, name, stage):
        p = self._asset_path(game, category, name)
        if not p or stage not in type_info(type_of(p))["stages"]:
            return False
        with self._lock:
            meta = load_meta(p)
            if meta["stages"].get(stage):
                meta["stages"].pop(stage, None)
                turned_on = False
            else:
                meta["stages"][stage] = datetime.now().strftime("%d.%m.%Y %H:%M")
                turned_on = True
            save_meta(p, meta)
        if stage == "Done" and turned_on:
            self._notify_discord(f"🏁 **{name}** reached DONE · by {self._who()}")
        return True

    def set_stage(self, game, category, name, stage):
        """Kanban drop: mark every stage up to `stage` done, clear the ones after.
        Existing timestamps are kept; only newly reached stages get stamped now."""
        p = self._asset_path(game, category, name)
        steps = type_info(type_of(p))["stages"] if p else []
        if not p or stage not in steps:
            return False
        idx = steps.index(stage)
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        try:
            with self._lock:
                meta = load_meta(p)
                was_done = bool(meta["stages"].get("Done"))
                for i, s in enumerate(steps):
                    if i <= idx:
                        meta["stages"].setdefault(s, now)
                    else:
                        meta["stages"].pop(s, None)
                save_meta(p, meta)
        except Exception:
            return False
        if stage == "Done" and not was_done:
            self._notify_discord(f"🏁 **{name}** reached DONE · by {self._who()}")
        return True

    def add_todo(self, game, category, name, text):
        p = self._asset_path(game, category, name)
        text = (text or "").strip()
        if not p or not text:
            return False
        with self._lock:
            meta = load_meta(p)
            meta.setdefault("todos", []).append(
                {"id": uuid.uuid4().hex[:8], "text": text, "done": False})
            save_meta(p, meta)
        return True

    def toggle_todo(self, game, category, name, todo_id):
        p = self._asset_path(game, category, name)
        if not p:
            return False
        with self._lock:
            meta = load_meta(p)
            for t in meta.get("todos", []):
                if t["id"] == todo_id:
                    t["done"] = not t["done"]
            save_meta(p, meta)
        return True

    def delete_todo(self, game, category, name, todo_id):
        p = self._asset_path(game, category, name)
        if not p:
            return False
        with self._lock:
            meta = load_meta(p)
            meta["todos"] = [t for t in meta.get("todos", []) if t["id"] != todo_id]
            save_meta(p, meta)
        return True

    def save_notes(self, game, category, name, notes):
        p = self._asset_path(game, category, name)
        if not p:
            return False
        with self._lock:
            meta = load_meta(p)
            meta["notes"] = notes or ""
            save_meta(p, meta)
        return True

    # ---------- library (alphas / tiling / face PNGs) ----------

    def _lib_root(self) -> Path | None:
        r = (self.cfg.get("library_root", "") or "").strip()
        if r:
            return Path(r)
        root = self._root()
        return (root / LIB_DIR_NAME) if root else None

    def _lib_resolve(self, rel):
        """Resolve a relative path inside the library, refusing to escape it."""
        base = self._lib_root()
        if base is None:
            return None, None
        try:
            base = base.resolve()
        except Exception:
            return None, None
        p = (base / (rel or "")).resolve()
        if p != base and base not in p.parents:
            return None, None            # path traversal attempt
        return base, p

    def library_list(self, rel=""):
        base, cur = self._lib_resolve(rel)
        if base is None:
            return {"ok": False, "error": "Set your Local root first (⚙ Settings)."}
        created = False
        try:
            if not base.exists():
                base.mkdir(parents=True, exist_ok=True)
                for c in LIB_STARTER:
                    (base / c).mkdir(parents=True, exist_ok=True)
                created = True
            if not cur.exists():
                cur = base
        except Exception as e:
            return {"ok": False, "error": f"Could not open library: {e}"}
        folders, files = [], []
        try:
            for p in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    try:
                        n = sum(1 for q in p.rglob("*")
                                if q.is_file() and q.suffix.lower() in LIB_EXT)
                    except Exception:
                        n = 0
                    folders.append({"name": p.name, "count": n,
                                    "rel": str(p.relative_to(base)).replace("\\", "/")})
                elif p.suffix.lower() in LIB_EXT:
                    try:
                        st = p.stat()
                    except Exception:
                        continue
                    files.append({
                        "name": p.name, "stem": p.stem, "ext": p.suffix.lower(),
                        "path": str(p), "size": human_size(st.st_size),
                        "bytes": st.st_size,
                        "time": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
                        "dim": lib_dims(p), "thumb": lib_thumb(p),
                    })
        except Exception as e:
            return {"ok": False, "error": f"Could not read folder: {e}"}
        rel_now = "" if cur == base else str(cur.relative_to(base)).replace("\\", "/")
        crumbs, acc = [], []
        for part in [p for p in rel_now.split("/") if p]:
            acc.append(part)
            crumbs.append({"name": part, "rel": "/".join(acc)})
        return {"ok": True, "root": str(base), "rel": rel_now, "crumbs": crumbs,
                "folders": folders, "files": files, "created": created}

    def library_data(self, path):
        """Full-resolution data URI — used to hand a real file to a drag-and-drop."""
        base, p = self._lib_resolve("")
        try:
            p = Path(path).resolve()
            if base is None or (p != base and base not in p.parents):
                return {"ok": False, "error": "Outside the library."}
            if not p.is_file() or p.suffix.lower() not in LIB_EXT:
                return {"ok": False, "error": "Not a library image."}
            if p.stat().st_size > 12_000_000:
                return {"ok": False, "error": "too big to drag — use Reveal instead"}
            import base64
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                    ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff",
                    }.get(p.suffix.lower(), "application/octet-stream")
            return {"ok": True, "name": p.name, "mime": mime,
                    "uri": f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def library_rename(self, path, new_name):
        base, _ = self._lib_resolve("")
        try:
            p = Path(path).resolve()
        except Exception:
            return {"ok": False, "error": "Bad path."}
        if base is None or (p != base and base not in p.parents):
            return {"ok": False, "error": "Outside the library."}
        if not p.exists():
            return {"ok": False, "error": "File not found."}
        nn = safe_name(new_name, keep_ext_from=p if p.is_file() else None)
        if not nn:
            return {"ok": False, "error": 'Invalid name (no \\ / : * ? " < > |).'}
        dst = p.with_name(nn)
        if dst == p:
            return {"ok": True, "name": nn, "path": str(dst)}
        if dst.exists():
            return {"ok": False, "error": f"“{nn}” already exists here."}
        try:
            p.rename(dst)
        except Exception as e:
            return {"ok": False, "error": f"Rename failed: {e}"}
        _LIB_MEM.pop(str(p).lower(), None)
        return {"ok": True, "name": nn, "path": str(dst)}

    def library_new_folder(self, rel, name):
        base, cur = self._lib_resolve(rel)
        if base is None:
            return {"ok": False, "error": "Library not available."}
        nn = safe_name(name)
        if not nn:
            return {"ok": False, "error": "Invalid folder name."}
        try:
            (cur / nn).mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return {"ok": False, "error": f"“{nn}” already exists."}
        except Exception as e:
            return {"ok": False, "error": f"Could not create: {e}"}
        return {"ok": True, "name": nn}

    def library_import(self, rel):
        """Pick files with the native dialog and copy them into this collection."""
        base, cur = self._lib_resolve(rel)
        if base is None:
            return {"ok": False, "error": "Library not available."}
        try:
            res = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("Images (*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.webp;*.tif;*.tiff;*.psd;*.exr)",
                            "All files (*.*)"))
        except Exception as e:
            return {"ok": False, "error": f"Dialog failed: {e}"}
        if not res:
            return {"ok": True, "copied": 0}
        copied, skipped = 0, 0
        for f in res:
            src = Path(f)
            if src.suffix.lower() not in LIB_EXT:
                skipped += 1
                continue
            dst = cur / src.name
            i = 2
            while dst.exists():
                dst = cur / f"{src.stem}_{i}{src.suffix}"
                i += 1
            try:
                shutil.copy2(src, dst)
                copied += 1
            except Exception:
                skipped += 1
        return {"ok": True, "copied": copied, "skipped": skipped}

    def library_reveal(self, path):
        """Open Explorer with the file selected — the reliable drag source."""
        try:
            p = Path(path)
            if not p.exists():
                return False
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
            return True
        except Exception:
            return False

    def library_copy_file(self, path):
        """Put the actual file on the clipboard (paste into Explorer / most apps)."""
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": "File not found."}
        if not sys.platform.startswith("win"):
            return {"ok": False, "error": "Clipboard copy is Windows-only."}
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Set-Clipboard -LiteralPath " + json.dumps(str(p))],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=15, check=True)
        except Exception as e:
            return {"ok": False, "error": f"Copy failed: {e}"}
        return {"ok": True}

    def set_library_root(self):
        try:
            res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if res:
            self.cfg["library_root"] = res[0]
            try:
                save_config(self.cfg)
            except Exception as e:
                return {"ok": False, "error": f"Could not save: {e}"}
        return {"ok": True, "root": self.cfg.get("library_root", "")}

    # ---------- time tracking (which program, how long) ----------

    def _start_time_tracker(self):
        """Background thread: every minute, credit a minute to each art app that
        is currently running. Daemon -> dies with the app, never blocks the UI."""
        def loop():
            while True:
                try:
                    time.sleep(TRACK_INTERVAL_S)
                    names = running_process_names()
                    active = {TRACKED_APPS[n] for n in names if n in TRACKED_APPS}
                    if not active:
                        continue
                    day = datetime.now().strftime("%Y-%m-%d")
                    with self._lock:
                        d = load_timelog()
                        slot = d["days"].setdefault(day, {})
                        for app in active:
                            slot[app] = slot.get(app, 0) + TRACK_INTERVAL_S / 60.0
                        save_timelog(d)
                except Exception:
                    continue
        threading.Thread(target=loop, daemon=True).start()

    def timelog_get(self):
        d = load_timelog()
        days = d.get("days", {})
        totals, per_day = {}, {}
        for day, apps in days.items():
            if not isinstance(apps, dict):
                continue
            per_day[day] = {}
            for app, mins in apps.items():
                try:
                    m = float(mins)
                except Exception:
                    continue
                totals[app] = totals.get(app, 0) + m
                per_day[day][app] = m
        today = datetime.now().strftime("%Y-%m-%d")
        week_start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        week = {}
        for day, apps in per_day.items():
            if day >= week_start:
                for app, m in apps.items():
                    week[app] = week.get(app, 0) + m
        since = min(per_day) if per_day else ""
        return {"ok": True,
                "totals": {k: round(v / 60, 2) for k, v in totals.items()},
                "today": {k: round(v / 60, 2) for k, v in per_day.get(today, {}).items()},
                "week": {k: round(v / 60, 2) for k, v in week.items()},
                "days": per_day, "since": since, "day_count": len(per_day),
                "tracked": sorted(set(TRACKED_APPS.values()))}

    def timelog_add(self, app, hours, day=""):
        """Manually add hours (for work done before tracking existed)."""
        app = (app or "").strip()
        if app not in set(TRACKED_APPS.values()):
            return {"ok": False, "error": "Unknown program."}
        try:
            h = float(str(hours).replace(",", ".").strip())
        except Exception:
            return {"ok": False, "error": "Enter hours as a number, e.g. 12.5"}
        if h == 0 or h < -10000 or h > 10000:
            return {"ok": False, "error": "Hours out of range."}
        day = (day or "").strip() or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return {"ok": False, "error": "Date must be YYYY-MM-DD."}
        with self._lock:
            d = load_timelog()
            slot = d["days"].setdefault(day, {})
            slot[app] = max(0.0, slot.get(app, 0) + h * 60)
            try:
                save_timelog(d)
            except Exception as e:
                return {"ok": False, "error": f"Could not save: {e}"}
        return {"ok": True}

    # ---------- roblox dev journey ----------

    def journey_get(self):
        d = load_earnings()
        entries = []
        for e in d["entries"]:
            if not (isinstance(e.get("amount"), (int, float)) and e.get("id")):
                continue
            e = dict(e)
            if not e.get("date"):     # legacy rows: derive ISO date from "dd.mm.yyyy HH:MM"
                try:
                    e["date"] = datetime.strptime(
                        e.get("time", "").split(" ")[0], "%d.%m.%Y").strftime("%Y-%m-%d")
                except Exception:
                    e["date"] = datetime.now().strftime("%Y-%m-%d")
            entries.append(e)
        entries.sort(key=lambda x: x["date"])
        total = int(sum(e["amount"] for e in entries))
        return {"ok": True, "total": total, "entries": entries,
                "usd": round(total * DEVEX_USD_PER_ROBUX, 2)}

    def journey_add(self, amount, note="", date=""):
        try:
            amt = int(round(float(str(amount).replace(",", "").replace("R$", "").strip())))
        except Exception:
            return {"ok": False, "error": "Enter a number, e.g. 250"}
        if amt <= 0:
            return {"ok": False, "error": "Amount must be positive."}
        if amt > 50_000_000:
            return {"ok": False, "error": "That looks too large — typo?"}
        day = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return {"ok": False, "error": "Date must be YYYY-MM-DD."}
        with self._lock:
            d = load_earnings()
            before = int(sum(e.get("amount", 0) for e in d["entries"]))
            entry = {"id": uuid.uuid4().hex[:8], "amount": amt,
                     "note": (note or "").strip(), "date": day,
                     "time": dt.strftime("%d.%m.%Y")
                     + datetime.now().strftime(" %H:%M")}
            d["entries"].append(entry)
            try:
                save_earnings(d)
            except Exception as e:
                return {"ok": False, "error": f"Could not save: {e}"}
        total = before + amt
        rb, ra = rank_for(before), rank_for(total)
        if rb != ra:
            self._notify_discord(f"🏆 **{self._who()}** ranked up on the Roblox dev journey: "
                                 f"{rb} → **{ra}**  ·  {total:,} R$ lifetime")
        return {"ok": True, "total": total, "before": before, "entry": entry}

    def journey_delete(self, entry_id):
        with self._lock:
            d = load_earnings()
            n = len(d["entries"])
            d["entries"] = [e for e in d["entries"] if e.get("id") != entry_id]
            if len(d["entries"]) == n:
                return {"ok": False, "error": "Entry not found."}
            try:
                save_earnings(d)
            except Exception as e:
                return {"ok": False, "error": f"Could not save: {e}"}
        return {"ok": True}

    # ---------- roblox preflight ----------

    def preflight(self, game, category, name):
        p = self._asset_path(game, category, name)
        if not p:
            return {"ok": False, "error": "Asset not found."}
        try:
            return audio_preflight(p) if type_of(p) == "SFX" else roblox_preflight(p)
        except Exception as e:
            return {"ok": False, "error": f"Preflight failed: {e}"}

    # ---------- discord pings ----------

    def _notify_discord(self, text):
        """Fire-and-forget webhook post — never blocks or breaks the caller."""
        url = (self.cfg.get("discord_webhook", "") or "").strip()
        if not url.startswith("https://"):
            return

        def _post():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url,
                    data=json.dumps({"content": text[:1900],
                                     "username": "Pipeline Hub"}).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "User-Agent": "PipelineHub"})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def set_discord_webhook(self, url):
        self.cfg["discord_webhook"] = (url or "").strip()
        try:
            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"Could not save config: {e}"}
        return {"ok": True}

    def test_discord(self):
        if not (self.cfg.get("discord_webhook", "") or "").strip().startswith("https://"):
            return {"ok": False, "error": "Paste a Discord webhook URL first."}
        self._notify_discord("👋 Pipeline Hub connected — review and done pings will appear here.")
        return {"ok": True}

    # ---------- review workflow ----------

    def set_user_name(self, name):
        """Who is signing review actions on this machine (stored in config)."""
        self.cfg["user_name"] = (name or "").strip()
        try:
            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"Could not save config: {e}"}
        return {"ok": True, "user_name": self.cfg["user_name"]}

    def set_review(self, game, category, name, state, note=""):
        if state not in REVIEW_STATES:
            return {"ok": False, "error": f"Unknown review state: {state}"}
        p = self._asset_path(game, category, name)
        if not p:
            return {"ok": False, "error": "Asset not found."}
        who = self._who()
        try:
            with self._lock:
                meta = load_meta(p)
                r = review_of(meta)
                entry = {"state": state, "by": who, "note": (note or "").strip(),
                         "time": datetime.now().strftime("%d.%m.%Y %H:%M")}
                r.update(entry)
                r["history"] = (r["history"] + [entry])[-20:]
                meta["review"] = r
                save_meta(p, meta)
        except Exception as e:
            return {"ok": False, "error": f"Could not write review state: {e}"}
        labels = {"pending": "🕐 ready for review", "approved": "✅ APPROVED",
                  "changes": "✏️ changes requested"}
        if state in labels:
            msg = f"**{name}** — {labels[state]} · by {who}"
            if entry["note"]:
                msg += f'\n> {entry["note"]}'
            self._notify_discord(msg)
        return {"ok": True, "review": r}

    def file_rename(self, path, new_name):
        """Rename a file inside an asset folder. Refuses anything outside the local root."""
        root = self._root()
        try:
            p = Path(path).resolve()
        except Exception:
            return {"ok": False, "error": "Bad path."}
        if not root or root.resolve() not in p.parents:
            return {"ok": False, "error": "That file is outside your local root."}
        if not p.is_file():
            return {"ok": False, "error": "File not found."}
        nn = safe_name(new_name, keep_ext_from=p)
        if not nn:
            return {"ok": False, "error": 'Invalid name (no \\ / : * ? " < > |).'}
        dst = p.with_name(nn)
        if dst == p:
            return {"ok": True, "name": nn}
        if dst.exists():
            return {"ok": False, "error": f"“{nn}” already exists in that folder."}
        try:
            p.rename(dst)
        except Exception as e:
            return {"ok": False, "error": f"Rename failed: {e}"}
        return {"ok": True, "name": nn, "path": str(dst)}

    def audio_data(self, path):
        """Return an audio file as a data URI so the UI can play it."""
        root = self._root()
        try:
            p = Path(path).resolve()
        except Exception:
            return {"ok": False, "error": "Bad path."}
        if not root or root.resolve() not in p.parents:
            return {"ok": False, "error": "That file is outside your local root."}
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            return {"ok": False, "error": "Not an audio file."}
        try:
            if p.stat().st_size > 30_000_000:
                return {"ok": False, "error": "Too large to preview here — use Open."}
            import base64
            mime = {".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
                    ".flac": "audio/flac", ".aiff": "audio/aiff",
                    ".aif": "audio/aiff"}.get(p.suffix.lower(), "audio/*")
            return {"ok": True,
                    "uri": f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def ensure_folder(self, game, category, name, sub):
        p = self._asset_path(game, category, name)
        if not p or sub not in SUBFOLDERS:
            return False
        try:
            (p / sub).mkdir(parents=True, exist_ok=True)
            return True
        except Exception:
            return False

    def open_path(self, path):
        p = Path(path)
        if p.exists():
            try:
                open_in_os(p)
                return True
            except Exception:
                return False
        return False

    # ---------- integrations ----------

    def _blender_addon_dirs(self):
        """[(version, addons_path)] for every installed Blender version."""
        if sys.platform.startswith("win"):
            base = Path(os.environ.get("APPDATA", "")) / "Blender Foundation" / "Blender"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Blender"
        else:
            base = Path.home() / ".config" / "blender"
        out = []
        if base.exists():
            for v in sorted(base.iterdir()):
                if v.is_dir() and v.name[:1].isdigit():
                    out.append((v.name, v / "scripts" / "addons"))
        return out

    def _painter_plugin_dirs(self):
        """[(plugins_dir, painter_dir_exists)] — Painter resolves 'Documents' via the
        Windows known-folder API, which can point anywhere (OneDrive, custom paths),
        so check the shell-resolved location first, then the literal fallbacks."""
        docs = []
        if sys.platform.startswith("win"):
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                    v, _ = winreg.QueryValueEx(k, "Personal")
                    docs.append(Path(os.path.expandvars(v)))
            except Exception:
                pass
        docs += [Path.home() / "Documents", Path.home() / "OneDrive" / "Documents"]
        out, seen = [], set()
        for d in docs:
            c = d / "Adobe" / "Adobe Substance 3D Painter" / "python" / "plugins"
            key = str(c).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((c, c.parent.parent.exists()))
        return out

    def integrations_status(self):
        """Detected apps + whether our add-on/plugin files are currently installed."""
        bl = self._blender_addon_dirs()
        bl_installed = [v for v, d in bl if (d / "pipeline_hub_export.py").exists()]
        sp_dirs = self._painter_plugin_dirs()
        sp_present = [c for c, ok in sp_dirs if ok]
        sp_installed = [c for c, ok in sp_dirs if ok and (c / "pipeline_hub_painter.py").exists()]
        sp_dir = (sp_installed or sp_present or [sp_dirs[0][0]])[0]
        sp_found = bool(sp_present)
        return {
            "blender": {
                "detected": bool(bl),
                "versions": [v for v, _ in bl],
                "installed_in": bl_installed,
                "installed": bool(bl_installed),
            },
            "painter": {
                "detected": sp_found,
                "path": str(sp_dir),
                "installed": bool(sp_installed),
            },
            "app_version": APP_VERSION,
        }

    def install_blender_addon(self):
        """Write the embedded add-on into every Blender version's addons folder."""
        dirs = self._blender_addon_dirs()
        if not dirs:
            return {"ok": False, "error": "Blender not detected — start Blender once, then retry."}
        versions, paths = [], []
        for v, dest in dirs:
            try:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "pipeline_hub_export.py").write_text(ADDON_SRC, encoding="utf-8")
                versions.append(v)
                paths.append(str(dest / "pipeline_hub_export.py"))
            except Exception:
                pass
        if not versions:
            return {"ok": False, "error": "Could not write to any Blender addons folder."}
        return {"ok": True, "versions": versions, "paths": paths}

    def install_painter_plugin(self):
        """Write the embedded plugin into every Painter plugins folder that exists
        (Documents may be cloud-redirected, so cover all candidates)."""
        dirs = self._painter_plugin_dirs()
        targets = [c for c, ok in dirs if ok] or [dirs[0][0]]
        written, err = [], ""
        for t in targets:
            try:
                t.mkdir(parents=True, exist_ok=True)
                (t / "pipeline_hub_painter.py").write_text(PAINTER_SRC, encoding="utf-8")
                written.append(str(t / "pipeline_hub_painter.py"))
            except Exception as e:
                err = str(e)
        if not written:
            return {"ok": False, "error": err or "Could not write plugin."}
        return {"ok": True, "path": " · ".join(written)}

    def install_integrations(self):
        return {"blender": self.install_blender_addon(),
                "painter": self.install_painter_plugin()}

    def sync_all(self):
        """Sync every asset local -> cloud. Returns totals."""
        cloud = self.cfg.get("cloud_root", "")
        if not cloud or not Path(cloud).exists():
            return {"ok": False, "error": "Cloud folder not set — click the cloud path at the top."}
        root = self._root()
        if not root:
            return {"ok": False, "error": "Local root not set."}
        total_c = total_s = 0
        errs = []
        n_assets = 0
        unapproved = 0
        for g in self.get_state()["games"]:
            for c in g["categories"]:
                for a in c["assets"]:
                    n_assets += 1
                    if a.get("review", {}).get("state") != "approved":
                        unapproved += 1
                    rel = (Path(a["game"]) / a["name"]) if a["category"] == UNCAT \
                        else (Path(a["game"]) / a["category"] / a["name"])
                    cp, sk, er = sync_asset_files(Path(a["path"]), Path(cloud) / rel)
                    total_c += cp
                    total_s += sk
                    errs.extend(er)
                    with self._lock:
                        meta = load_meta(Path(a["path"]))
                        meta["last_sync"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                        save_meta(Path(a["path"]), meta)
        return {"ok": True, "assets": n_assets, "copied": total_c,
                "skipped": total_s, "unapproved": unapproved, "errors": errs[:10]}

    def sync_asset(self, game, category, name, force=False):
        p = self._asset_path(game, category, name)
        cloud = self.cfg.get("cloud_root", "")
        if not p:
            return {"ok": False, "error": "Asset not found."}
        if not cloud or not Path(cloud).exists():
            return {"ok": False, "error": "Cloud folder not set — click the cloud path at the top."}
        review = review_of(load_meta(p))
        if not force and review["state"] != "approved":
            # gate: unapproved assets need an explicit second click in the UI
            return {"ok": False, "needs_review": True, "review": review}
        rel = (Path(game) / name) if category == UNCAT else (Path(game) / category / name)
        copied, skipped, errors = sync_asset_files(p, Path(cloud) / rel)
        with self._lock:
            meta = load_meta(p)
            meta["last_sync"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            save_meta(p, meta)
        return {"ok": True, "copied": copied, "skipped": skipped,
                "errors": errors[:10], "last_sync": meta["last_sync"]}


# ----------------------------------------------------------------------------
# Web UI
# ----------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>ALL MY ART</title>
<style>
:root{
  --bg:#0e0609; --panel:rgba(38,20,26,.55); --panel2:rgba(48,26,34,.65);
  --card:rgba(62,34,44,.55); --card-h:rgba(82,46,60,.65);
  --text:#f8ecef; --dim:#b38e9a; --line:rgba(220,130,155,.14);
  --accent:#ff4d64; --accent2:#ff8a5c; --ok:#46d68c; --warn:#ffb454; --bad:#ff5f6b;
  --game:#ff8fa8;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,sans-serif;color:var(--text);font-size:14px;
  background:radial-gradient(1200px 700px at 18% -10%,#33101a 0%,transparent 55%),
             radial-gradient(1000px 700px at 105% 110%,#30101f 0%,transparent 55%),
             radial-gradient(700px 500px at 55% 45%,#240a12 0%,transparent 60%),var(--bg)}
#stars{position:fixed;inset:0;z-index:0}
.nebula{position:fixed;border-radius:50%;filter:blur(110px);opacity:.38;z-index:0;pointer-events:none;
  animation:drift 40s ease-in-out infinite alternate}
.n1{width:560px;height:560px;left:-140px;top:-180px;background:#9a1f38}
.n2{width:480px;height:480px;right:-120px;bottom:-160px;background:#96235c;animation-delay:-14s}
.n3{width:380px;height:380px;left:38%;top:55%;background:#8e1730;animation-delay:-27s}
.n4{width:300px;height:300px;right:26%;top:4%;background:#663a0d;opacity:.22;animation-delay:-8s}
@keyframes drift{from{transform:translate(0,0) scale(1)}to{transform:translate(70px,40px) scale(1.15)}}

#app{position:relative;z-index:1;height:100%;display:flex;flex-direction:column}

/* ---------- top bar ---------- */
#top{display:flex;align-items:center;gap:10px;padding:12px 18px;
  background:linear-gradient(180deg,rgba(54,26,35,.72),rgba(32,15,21,.55));
  backdrop-filter:blur(18px) saturate(1.35);border-bottom:1px solid rgba(220,130,155,.2);
  box-shadow:0 1px 0 rgba(255,255,255,.04) inset}
#logo{font-weight:700;font-size:16px;letter-spacing:2px;margin-right:10px;white-space:nowrap;
  background:linear-gradient(90deg,var(--accent),var(--accent2),#ffb27f,var(--accent));
  background-size:300% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;
  animation:logoFlow 9s linear infinite;filter:drop-shadow(0 0 12px rgba(255,95,120,.35))}
@keyframes logoFlow{to{background-position:300% 0}}
.btn{border:1px solid rgba(220,130,155,.18);border-radius:10px;padding:9px 16px;
  font:600 13px 'Segoe UI';cursor:pointer;color:var(--text);
  background:linear-gradient(165deg,rgba(90,48,60,.6),rgba(58,30,39,.55));transition:all .18s ease}
.btn:hover{background:linear-gradient(165deg,rgba(116,62,78,.7),rgba(74,38,50,.65));
  border-color:rgba(255,95,120,.45);transform:translateY(-1px);
  box-shadow:0 5px 18px rgba(200,60,95,.28)}
.btn:active{transform:translateY(0)}
.btn.primary{background:linear-gradient(120deg,var(--accent),var(--accent2));color:#0a0c18;
  border-color:transparent;box-shadow:0 3px 14px rgba(255,95,120,.28)}
.btn.primary:hover{filter:brightness(1.12);box-shadow:0 6px 24px rgba(140,130,255,.45)}
.btn.sync{background:rgba(160,60,85,.45)}
.btn.sync:hover{background:rgba(190,70,100,.6)}
select,input[type=text]{background:var(--card);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:9px 12px;font:13px 'Segoe UI';outline:none;transition:border .18s}
select:focus,input[type=text]:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(255,95,120,.14)}
#search{width:180px}
#typef{max-width:130px}
.tico{color:var(--accent);margin-right:6px;font-size:11px;opacity:.9}
.badge.type{background:rgba(255,138,92,.13);color:var(--accent2);
  border-color:rgba(255,138,92,.35);box-shadow:0 0 14px rgba(255,138,92,.1)}
.wavemini{display:flex;align-items:center;justify-content:center;padding:3px 4px}
.wavebox{background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:13px;
  padding:10px 12px;animation:fadeUp .35s ease both}
.steady .wavebox{animation:none}
.board.lane{height:auto;min-height:210px;margin-bottom:6px}
.board.lane .kcol{min-height:180px}
#roots{display:flex;gap:26px;padding:7px 20px;font-size:11.5px;color:var(--dim)}
#roots span{cursor:pointer;transition:color .15s}
#roots span:hover{color:var(--text)}
#roots .unset{color:var(--warn)}

/* ---------- layout ---------- */
#main{flex:1;display:flex;min-height:0;padding:10px 14px 14px;gap:12px}
#side{width:330px;overflow-y:auto;padding-right:4px}
#detail{flex:1;overflow-y:auto;backdrop-filter:blur(16px);border-radius:18px;padding:26px 30px;
  background:linear-gradient(180deg,rgba(64,32,42,.62),rgba(42,21,28,.55));
  border:1px solid rgba(220,130,155,.18);
  box-shadow:0 22px 60px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.06)}
::-webkit-scrollbar{width:9px}
::-webkit-scrollbar-thumb{background:rgba(200,120,145,.25);border-radius:5px}
::-webkit-scrollbar-thumb:hover{background:rgba(200,120,145,.45)}
::-webkit-scrollbar-track{background:transparent}

/* ---------- sidebar tree ---------- */
.game-h{font:700 11px 'Segoe UI';letter-spacing:1.5px;color:var(--game);margin:16px 6px 4px}
.cat-h{font:600 10.5px 'Segoe UI';letter-spacing:1px;color:var(--dim);margin:9px 8px 3px;text-transform:uppercase}
.acard{position:relative;background:linear-gradient(165deg,rgba(74,40,50,.6),rgba(54,28,36,.5));
  border:1px solid rgba(220,130,155,.12);border-radius:13px;padding:11px 14px 11px 17px;
  margin:5px 2px;cursor:pointer;transition:all .18s ease;animation:fadeUp .35s ease both;
  overflow:hidden}
.acard::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:3px;
  background:rgba(220,130,155,.2);transition:all .2s}
.acard.lv-ok::before{background:linear-gradient(180deg,var(--ok),#2f9e68);box-shadow:0 0 8px rgba(70,214,140,.5)}
.acard.lv-warn::before{background:linear-gradient(180deg,var(--warn),#d18a2f);box-shadow:0 0 8px rgba(255,180,84,.5)}
.acard.lv-bad::before{background:linear-gradient(180deg,var(--bad),#c23540);box-shadow:0 0 8px rgba(255,95,107,.5)}
.acard:hover{background:linear-gradient(165deg,rgba(100,54,68,.7),rgba(70,36,46,.6));
  transform:translateX(3px);border-color:rgba(255,95,120,.35)}
.acard.fin::before{background:linear-gradient(180deg,#ffd76a,#e8a63a);
  box-shadow:0 0 10px rgba(255,205,90,.55)}
.acard.fin .nm{background:linear-gradient(90deg,#fff3cf,#ffce62);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.acard.sel{border:1px solid transparent;
  background:linear-gradient(165deg,rgba(118,52,70,.65),rgba(88,38,52,.6)) padding-box,
             linear-gradient(120deg,var(--accent),var(--accent2)) border-box}
.acard.sel::after{content:'';position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  box-shadow:0 0 22px rgba(255,95,120,.22) inset;animation:selPulse 3.2s ease-in-out infinite}
@keyframes selPulse{50%{box-shadow:0 0 34px rgba(150,130,255,.32) inset}}
.acard .r1{display:flex;justify-content:space-between;align-items:center}
.acard{display:flex;gap:11px;align-items:center}
.cthumb{width:46px;height:46px;border-radius:9px;object-fit:cover;flex-shrink:0;
  border:1px solid var(--line);background:rgba(255,255,255,.04)}
.cbody{flex:1;min-width:0}
.game-h .gstat{float:right;color:var(--dim);font-weight:600;letter-spacing:0}
.acard .nm{font-weight:700;font-size:13.5px}
.acard .stg{font-size:10.5px;color:var(--accent)}
.acard .st{font-size:11px;margin:3px 0 7px}
.pbar{height:4px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden}
.pbar i{display:block;position:relative;height:100%;border-radius:3px;overflow:hidden;
  background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s ease;
  box-shadow:0 0 8px rgba(255,95,120,.4)}
.pbar i.full{background:linear-gradient(90deg,#35b878,var(--ok));box-shadow:0 0 10px rgba(70,214,140,.5)}
.pbar i.full::after{content:'';position:absolute;inset:0;transform:translateX(-100%);
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
  animation:shine 3.2s ease infinite}
@keyframes shine{60%,100%{transform:translateX(100%)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.dim{color:var(--dim)}
.empty{color:var(--dim);padding:30px 12px;font-size:13px;line-height:1.6}

/* ---------- detail ---------- */
.dh{display:flex;align-items:center;gap:14px;animation:fadeUp .3s ease both}
.dh h1{font-size:25px;font-weight:700;background:linear-gradient(90deg,#fff 30%,#facdd3);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.badge{font:700 11px 'Segoe UI';padding:4px 13px;border-radius:20px;background:rgba(234,146,170,.14);
  color:var(--game);border:1px solid rgba(234,146,170,.3);box-shadow:0 0 14px rgba(234,146,170,.12)}
.badge.cat{background:rgba(255,95,120,.12);color:var(--accent);border-color:rgba(255,95,120,.3)}
.dstatus{font-weight:600;font-size:13px;margin:8px 0 20px;animation:fadeUp .35s ease both}
.sec{font:700 10.5px 'Segoe UI';letter-spacing:1.6px;color:var(--dim);margin:20px 0 8px;
  display:flex;align-items:center;gap:10px}
.sec::after{content:'';flex:1;height:1px;
  background:linear-gradient(90deg,rgba(220,130,155,.28),transparent)}
.chips{display:flex;flex-wrap:wrap;gap:7px;animation:fadeUp .4s ease both}
.chip{padding:7px 15px;border-radius:18px;font:600 12px 'Segoe UI';cursor:pointer;
  background:rgba(255,255,255,.05);color:var(--dim);border:1px solid var(--line);
  transition:all .2s ease;user-select:none}
.chip:hover{background:rgba(255,255,255,.1);transform:translateY(-1px)}
.chip.done{background:rgba(70,214,140,.16);color:var(--ok);border-color:rgba(70,214,140,.4)}
.frow{display:flex;align-items:center;gap:10px;background:rgba(255,255,255,.04);
  border:1px solid var(--line);border-radius:11px;padding:9px 14px;margin:5px 0;
  animation:fadeUp .45s ease both;transition:all .18s}
.frow:hover{background:rgba(255,255,255,.07);border-color:rgba(255,95,120,.3);
  transform:translateX(2px)}
.frow .fl{font-weight:700;font-size:12.5px;width:118px;flex-shrink:0}
.frow .fi{flex:1;font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow .fi.miss{color:var(--dim)}
.frow .fi.ok{color:var(--ok);font-weight:600}
.frow .fi.warn{color:var(--warn)}
.sbtn{border:none;border-radius:8px;padding:5px 12px;font:600 11.5px 'Segoe UI';cursor:pointer;
  color:var(--text);background:rgba(255,255,255,.08);transition:all .15s}
.sbtn:hover{background:rgba(255,255,255,.16)}
.sbtn.open{background:rgba(160,60,85,.5)}
.sbtn.open:hover{background:rgba(80,125,195,.65)}

/* ---------- todos ---------- */
#todoadd{display:flex;gap:8px;margin-bottom:8px}
#todoadd input{flex:1}
.todo{display:flex;align-items:center;gap:11px;padding:8px 13px;border-radius:10px;
  background:rgba(255,255,255,.04);border:1px solid var(--line);margin:4px 0;
  animation:fadeUp .3s ease both;transition:all .2s}
.todo:hover{background:rgba(255,255,255,.07)}
.todo .cb{width:18px;height:18px;border-radius:6px;border:2px solid var(--dim);cursor:pointer;
  display:flex;align-items:center;justify-content:center;font-size:11px;color:transparent;
  transition:all .2s;flex-shrink:0}
.todo.done .cb{background:var(--ok);border-color:var(--ok);color:#06281a}
.todo .tx{flex:1;font-size:13px;transition:all .25s}
.todo.done .tx{color:var(--dim);text-decoration:line-through}
.todo .del{opacity:0;border:none;background:none;color:var(--bad);cursor:pointer;font-size:15px;
  transition:opacity .15s}
.todo:hover .del{opacity:.85}
#notes{width:100%;min-height:96px;background:rgba(255,255,255,.04);border:1px solid var(--line);
  border-radius:11px;color:var(--text);padding:11px 13px;font:13px 'Segoe UI';outline:none;
  resize:vertical;transition:border .18s}
#notes:focus{border-color:var(--accent)}
#syncline{font-size:11px;color:var(--dim);margin-top:7px;transition:color .3s}

/* ---------- review ---------- */
.rvbox{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:12px;
  padding:13px 16px;margin:4px 0;animation:fadeUp .42s ease both}
.rvhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rvstate{font:700 11px 'Segoe UI';padding:4px 13px;border-radius:20px;letter-spacing:.5px}
.rvstate.none{background:rgba(255,255,255,.06);color:var(--dim);border:1px solid var(--line)}
.rvstate.pending{background:rgba(255,180,84,.14);color:var(--warn);border:1px solid rgba(255,180,84,.4)}
.rvstate.approved{background:rgba(70,214,140,.16);color:var(--ok);border:1px solid rgba(70,214,140,.4)}
.rvstate.changes{background:rgba(255,95,107,.14);color:var(--bad);border:1px solid rgba(255,95,107,.4)}
.rvmeta{font-size:11.5px;color:var(--dim)}
.rvnote{font-size:12.5px;margin-top:9px;padding:7px 11px;border-left:2px solid var(--line)}
.rvbtns{display:flex;gap:7px;margin-top:11px;flex-wrap:wrap;align-items:center}
.rvhist{margin-top:10px;font-size:11px;color:var(--dim);border-top:1px solid var(--line);padding-top:8px}
.rvhist div{margin:2px 0}
#rvnote{width:100%;margin-top:10px}

/* ---------- kanban board ---------- */
.board{display:flex;gap:10px;height:100%;overflow-x:auto;padding-bottom:4px}
.kcol{min-width:168px;flex:1;display:flex;flex-direction:column;background:rgba(255,255,255,.03);
  border:1px solid var(--line);border-radius:14px;padding:10px;transition:all .18s;
  animation:fadeUp .35s ease both}
.kcol.drag{border-color:var(--accent);background:rgba(255,95,120,.09);
  box-shadow:0 0 18px rgba(255,95,120,.15) inset}
.kcol h3{font:700 10.5px 'Segoe UI';letter-spacing:1.2px;color:var(--dim);margin:2px 4px 9px;
  display:flex;justify-content:space-between}
.kcol h3 .kn{color:var(--accent)}
.kcol.done-col{background:rgba(70,214,140,.04);border-color:rgba(70,214,140,.22)}
.kcol.done-col h3{color:var(--ok)}
.kcards{flex:1;overflow-y:auto;min-height:40px}
.kcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px;
  margin:0 0 7px;cursor:grab;font-size:12px;transition:all .15s;animation:fadeUp .3s ease both}
.kcard:hover{background:var(--card-h);transform:translateY(-1px);border-color:rgba(255,95,120,.4)}
.kcard:active{cursor:grabbing}
.kcard.ghost{opacity:.35}
.kcard .kt{font-weight:700;font-size:12.5px;display:flex;justify-content:space-between;gap:6px;align-items:center}
.kcard .kg{font-size:10px;color:var(--dim);margin-top:3px}
.kcard img{width:100%;height:54px;object-fit:cover;border-radius:7px;margin-bottom:6px;
  border:1px solid var(--line);pointer-events:none}
.actions{display:flex;gap:9px;margin-top:22px;animation:fadeUp .5s ease both}
.placeholder{display:flex;height:100%;align-items:center;justify-content:center;
  color:var(--dim);font-size:15px;flex-direction:column;gap:10px}
.placeholder .orb{width:72px;height:72px;border-radius:50%;
  background:radial-gradient(circle at 32% 30%,#ff7d94,#7a2a3f 65%,transparent 75%);
  box-shadow:0 0 50px rgba(110,140,255,.5);animation:pulse 3.2s ease-in-out infinite}
@keyframes pulse{0%,100%{transform:scale(1);opacity:.8}50%{transform:scale(1.12);opacity:1}}

/* ---------- pipeline stepper ---------- */
.stepper{display:flex;margin:10px 0 4px;animation:fadeUp .4s ease both}
.step{flex:1;position:relative;text-align:center;cursor:pointer;user-select:none;padding:2px 0}
.step::before{content:'';position:absolute;top:12px;left:-50%;width:100%;height:2px;
  background:rgba(255,255,255,.09);z-index:0;transition:background .3s}
.step:first-child::before{display:none}
.step.done::before{background:linear-gradient(90deg,#2f9e68,var(--ok))}
.sdot{width:24px;height:24px;border-radius:50%;margin:0 auto;position:relative;z-index:1;
  background:#2b2b33;border:2px solid rgba(255,255,255,.14);color:var(--dim);
  display:flex;align-items:center;justify-content:center;font:700 10.5px 'Segoe UI';
  transition:all .22s ease}
.step:hover .sdot{transform:scale(1.15);border-color:var(--accent);
  box-shadow:0 0 14px rgba(255,95,120,.45)}
.step.done .sdot{background:linear-gradient(150deg,#5fe3a4,var(--ok));border-color:var(--ok);
  color:#06281a;box-shadow:0 0 10px rgba(70,214,140,.35)}
.step.cur .sdot{border-color:var(--accent);animation:curPulse 2.6s ease-in-out infinite}
@keyframes curPulse{0%,100%{box-shadow:0 0 0 4px rgba(255,95,120,.22)}
  50%{box-shadow:0 0 0 7px rgba(255,95,120,.1),0 0 18px rgba(255,95,120,.5)}}
.slabel{font-size:9.5px;color:var(--dim);margin-top:5px;letter-spacing:.3px;transition:color .2s}
.step.done .slabel{color:var(--text)}
/* ---------- collapsible games ---------- */
.game-h{cursor:pointer;user-select:none;transition:color .15s}
.game-h:hover{color:#ffb7c8}
.game-h .chev{display:inline-block;width:14px;transition:transform .2s;font-size:9px}
.game-h.closed .chev{transform:rotate(-90deg)}
/* ---------- hero thumb ---------- */
.hthumb{width:64px;height:64px;border-radius:14px;object-fit:cover;border:1px solid rgba(220,130,155,.35);
  box-shadow:0 6px 22px rgba(0,0,0,.4),0 0 24px rgba(255,95,120,.18);animation:fadeUp .3s ease both}
/* ---------- radial gauge ---------- */
.radial{position:relative;width:96px;height:96px;flex-shrink:0}
.radial svg{transform:rotate(-90deg)}
.radial .rtxt{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;font-weight:700;font-size:19px}
.radial .rsub{font-size:9px;color:var(--dim);font-weight:600;letter-spacing:.6px}
/* ---------- command palette ---------- */
#kbar{position:fixed;inset:0;background:rgba(12,4,8,.55);backdrop-filter:blur(6px);
  display:none;align-items:flex-start;justify-content:center;z-index:70;padding-top:14vh}
#kbox{width:520px;background:rgba(46,22,30,.97);border:1px solid var(--line);border-radius:16px;
  overflow:hidden;box-shadow:0 24px 70px rgba(0,0,0,.55);animation:pop .22s cubic-bezier(.34,1.4,.5,1) both}
#kin{width:100%;background:transparent;border:none;border-bottom:1px solid var(--line);
  color:var(--text);padding:15px 18px;font:15px 'Segoe UI';outline:none}
#kres{max-height:320px;overflow-y:auto;padding:6px}
.kitem{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:10px;cursor:pointer;font-size:13px}
.kitem img{width:30px;height:30px;border-radius:7px;object-fit:cover}
.kitem .kpath{color:var(--dim);font-size:11px;margin-left:auto;white-space:nowrap}
.kitem.on,.kitem:hover{background:rgba(255,95,120,.14)}
.khint{padding:8px 14px;font-size:10.5px;color:var(--dim);border-top:1px solid var(--line)}
/* ---------- image library ---------- */
.crumb{cursor:pointer;transition:color .15s}
.crumb:hover{color:var(--accent)}
.csep{color:var(--dim);margin:0 2px}
.libfolders{display:flex;flex-wrap:wrap;gap:9px}
.libfolder{width:132px;padding:13px 12px;border-radius:13px;cursor:pointer;text-align:center;
  background:linear-gradient(165deg,rgba(90,48,60,.4),rgba(54,28,36,.4));
  border:1px solid rgba(220,130,155,.16);transition:all .18s}
.libfolder:hover{transform:translateY(-2px);border-color:rgba(255,95,120,.45);
  box-shadow:0 8px 22px rgba(0,0,0,.35)}
.lfico{font-size:23px;line-height:1.1;color:var(--accent2)}
.lfname{font-size:12.5px;font-weight:700;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lfcount{font-size:10.5px;color:var(--dim)}
.libgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:11px}
.libcard{border-radius:13px;padding:9px;background:rgba(255,255,255,.035);
  border:1px solid var(--line);transition:all .18s;cursor:grab}
.libcard:hover{border-color:rgba(255,95,120,.45);transform:translateY(-2px);
  box-shadow:0 10px 26px rgba(0,0,0,.4)}
.libcard:active{cursor:grabbing}
/* checkerboard so alphas and cut-out face PNGs read correctly */
.libthumb{height:118px;border-radius:9px;display:flex;align-items:center;justify-content:center;
  overflow:hidden;background-color:#d8d8d8;
  background-image:linear-gradient(45deg,#9c9c9c 25%,transparent 25%),
    linear-gradient(-45deg,#9c9c9c 25%,transparent 25%),
    linear-gradient(45deg,transparent 75%,#9c9c9c 75%),
    linear-gradient(-45deg,transparent 75%,#9c9c9c 75%);
  background-size:14px 14px;
  background-position:0 0,0 7px,7px -7px,-7px 0}
.libthumb img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.noprev{font:700 13px 'Segoe UI';color:#5a3540;letter-spacing:1px}
.libname{font-size:12px;font-weight:600;margin-top:7px;cursor:text;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.libname:hover{color:var(--accent)}
.libedit{width:100%;background:rgba(12,4,8,.8);border:1px solid var(--accent);border-radius:7px;
  color:var(--text);padding:4px 7px;font:12px 'Segoe UI';outline:none}
.libmeta{font-size:10.5px;color:var(--dim);margin-top:1px}
.libacts{display:flex;gap:4px;margin-top:7px;opacity:0;transition:opacity .15s;flex-wrap:wrap}
.libcard:hover .libacts{opacity:1}
.libacts .sbtn{padding:3px 8px;font-size:10.5px}

/* ---------- roblox dev journey ---------- */
.jhero{display:flex;gap:26px;align-items:center;padding:22px 26px;border-radius:16px;
  background:linear-gradient(165deg,rgba(90,48,60,.45),rgba(52,26,35,.45));
  border:1px solid rgba(220,130,155,.22);animation:fadeUp .35s ease both}
.jbig svg{filter:drop-shadow(0 10px 28px rgba(0,0,0,.55))}
.jbig.rankup svg{animation:bpop .9s cubic-bezier(.3,1.6,.4,1)}
@keyframes bpop{0%{transform:scale(.35) rotate(-14deg);filter:brightness(2.4)}
  60%{transform:scale(1.12) rotate(3deg)}100%{transform:scale(1)}}
.jrname{font-size:27px;font-weight:800;letter-spacing:.6px}
.jsub{font-size:12px;color:var(--dim);margin-top:2px}
.jtotal{font-size:36px;font-weight:800;margin-top:12px;
  background:linear-gradient(90deg,#fff,#facdd3);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.jsub2{font-size:10px;color:var(--dim);letter-spacing:1.6px;text-transform:uppercase}
.jbar{height:9px;border-radius:6px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:15px}
.jbar i{display:block;height:100%;border-radius:6px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  box-shadow:0 0 14px rgba(255,95,120,.55);transition:width .6s ease}
.jnext{font-size:12px;color:var(--dim);margin-top:7px}
#jadd,#hadd{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#jadd input[type=date],#hadd input[type=date],#hadd select{
  background:rgba(62,34,44,.55);border:1px solid rgba(220,130,155,.14);border-radius:10px;
  color:var(--text);padding:9px 11px;font:13px 'Segoe UI';outline:none;color-scheme:dark}
#hadd{margin-top:14px}
.sbtn.on{background:linear-gradient(120deg,var(--accent),var(--accent2));color:#1a0509;font-weight:700}
.jchartwrap{position:relative;background:rgba(255,255,255,.025);border:1px solid var(--line);
  border-radius:14px;padding:6px 8px 2px;animation:fadeUp .4s ease both}
.steady .jchartwrap{animation:none}
#jsvg{display:block;cursor:crosshair}
#jtip{position:absolute;display:none;pointer-events:none;z-index:5;font-size:11.5px;line-height:1.45;
  background:rgba(44,22,29,.97);border:1px solid rgba(255,95,120,.4);border-radius:9px;
  padding:7px 11px;box-shadow:0 10px 30px rgba(0,0,0,.5);white-space:nowrap}
.hrow{display:flex;align-items:center;gap:12px;margin:6px 0;animation:fadeUp .35s ease both}
.steady .hrow{animation:none}
.hname{width:150px;font-size:12.5px;font-weight:600;flex-shrink:0}
.hbarwrap{flex:1;height:14px;border-radius:7px;background:rgba(255,255,255,.06);overflow:hidden}
.hbarwrap i{display:block;height:100%;border-radius:7px;transition:width .5s ease;
  box-shadow:0 0 10px rgba(255,95,120,.25)}
.hval{width:62px;text-align:right;font-size:12px;font-weight:700;color:var(--dim)}
.jladder{display:flex;gap:7px;flex-wrap:wrap}
.jrank{flex:1;min-width:88px;text-align:center;padding:12px 6px 10px;border-radius:13px;
  background:rgba(255,255,255,.03);border:1px solid var(--line);transition:all .2s;cursor:default}
.jrank:hover{transform:translateY(-2px);border-color:rgba(255,95,120,.4)}
.jrank.cur{border-color:var(--accent);background:rgba(150,60,85,.28);
  box-shadow:0 0 24px rgba(255,95,120,.22)}
.jrank .jrn{font-size:11.5px;font-weight:700;margin-top:5px}
.jrank.locked .jrn{color:var(--dim)}
.jrank .jrt{font-size:10px;color:var(--dim);margin-top:1px}
.steady .jhero{animation:none}
#confetti{position:fixed;inset:0;pointer-events:none;z-index:90;overflow:hidden}
#confetti i{position:absolute;top:-24px;border-radius:2px;opacity:.95;
  animation:cfall linear forwards}
@keyframes cfall{to{transform:translateY(108vh) rotate(720deg);opacity:.65}}

/* ---------- dashboard tiles ---------- */
.tile{flex:1;border-radius:14px;padding:16px 18px;animation:fadeUp .35s ease both;
  background:linear-gradient(165deg,rgba(90,48,60,.4),rgba(54,28,36,.4));
  border:1px solid rgba(220,130,155,.16);transition:all .2s ease}
.tile:hover{transform:translateY(-2px);border-color:rgba(255,95,120,.4);
  box-shadow:0 8px 26px rgba(180,50,85,.25)}
.tile .tn{font-size:27px;font-weight:700;background:linear-gradient(90deg,#fff,#facdd3);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.tile .tn.ok{background:linear-gradient(90deg,#7fe8b5,var(--ok));-webkit-background-clip:text;background-clip:text}
.tile .tn.warn{background:linear-gradient(90deg,#ffd39a,var(--warn));-webkit-background-clip:text;background-clip:text}
.tile .tn.bad{background:linear-gradient(90deg,#ff9aa2,var(--bad));-webkit-background-clip:text;background-clip:text}
.tile .tl{font-size:11px;color:var(--dim);letter-spacing:.6px;margin-top:2px}

/* entrance animations only on app start — re-renders after that are instant,
   otherwise every click makes the whole UI "blink" as fadeUp replays */
.steady .acard,.steady .dh,.steady .dstatus,.steady .chips,.steady .stepper,.steady .frow,
.steady .todo,.steady .rvbox,.steady .actions,.steady .kcol,.steady .kcard,.steady .hthumb,
.steady .tile{animation:none}
/* ---------- misc polish ---------- */
.acard{box-shadow:0 1px 0 rgba(0,0,0,.25)}
.acard:hover{box-shadow:0 6px 20px rgba(0,0,0,.35)}
.btn:active{transform:translateY(1px) scale(.98)}

/* ---------- modal ---------- */
#overlay{position:fixed;inset:0;background:rgba(12,4,8,.6);backdrop-filter:blur(5px);
  display:none;align-items:center;justify-content:center;z-index:50}
#overlay.show{display:flex}
#modal{width:420px;background:linear-gradient(180deg,rgba(60,30,40,.96),rgba(44,22,29,.96));
  border:1px solid rgba(220,130,155,.3);border-radius:18px;padding:28px;
  box-shadow:0 30px 90px rgba(0,0,0,.6),0 0 40px rgba(255,95,120,.08);
  animation:pop .28s cubic-bezier(.34,1.4,.5,1) both}
@keyframes pop{from{opacity:0;transform:scale(.92) translateY(14px)}to{opacity:1;transform:none}}
#modal h2{font-size:18px;margin-bottom:18px}
#modal label{display:block;font:700 10.5px 'Segoe UI';letter-spacing:1.4px;color:var(--dim);margin:13px 0 6px}
#modal input,#modal select{width:100%}
.catpick{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.catpick .chip.on{background:rgba(255,95,120,.18);color:var(--accent);border-color:var(--accent)}
#merr{color:var(--bad);font-size:12px;margin-top:10px;min-height:16px}
.mbtns{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}
#toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(80px);
  background:linear-gradient(165deg,rgba(86,42,56,.96),rgba(54,26,35,.96));
  border:1px solid rgba(255,95,120,.35);border-radius:12px;padding:11px 22px;
  font-size:13px;z-index:60;transition:transform .35s cubic-bezier(.34,1.3,.5,1);
  backdrop-filter:blur(8px);box-shadow:0 12px 40px rgba(0,0,0,.5),0 0 22px rgba(255,95,120,.12)}
#toast.show{transform:translateX(-50%) translateY(0)}
</style></head>
<body>
<canvas id="stars"></canvas>
<div class="nebula n1"></div><div class="nebula n2"></div><div class="nebula n3"></div><div class="nebula n4"></div>

<div id="app">
  <div id="top">
    <div id="logo" onclick="goHome()" style="cursor:pointer" title="Overview">✦ ALL MY ART</div>
    <button class="btn" onclick="goHome()" title="Dashboard (Esc)">⌂ Overview</button>
    <button class="btn" onclick="goBoard()" title="Kanban board (Ctrl+B) — drag assets between stages">▦ Board</button>
    <button class="btn" onclick="goLibrary()" title="Image library (Ctrl+L) — alphas, tiling, face PNGs">🖼 Library</button>
    <button class="btn" onclick="goJourney()" title="Your Roblox dev journey (Ctrl+J) — log earnings, climb ranks">🏆 Journey</button>
    <button class="btn primary" onclick="openModal()" title="Ctrl+N">＋ New Asset</button>
    <button class="btn sync" id="syncall" onclick="doSyncAll()">☁ Sync All</button>
    <select id="typef" onchange="render();if(view!=='detail')renderMain()" style="margin-left:auto">
      <option value="">All types</option><option value="3D">◆ 3D Models</option><option value="SFX">♪ Sound FX</option></select>
    <select id="gamef" onchange="render();if(view!=='detail')renderMain()"><option>All games</option></select>
    <input type="text" id="search" placeholder="Search assets…  (/)" oninput="render();if(view==='board')renderBoard()">
    <button class="btn" onclick="openSettings()" title="Folders & integrations">⚙</button>
  </div>
  <div id="trapbanner" style="display:none;margin:8px 20px 2px;padding:8px 14px;border-radius:10px;
    background:rgba(255,95,107,.12);border:1px solid rgba(255,95,107,.4);color:var(--bad);
    font-size:12px;font-weight:600">
    ⚠ Your LOCAL folder is inside a cloud-synced directory (OneDrive/Drive/Dropbox). Big files will
    fight the sync client — open ⚙ Settings and point LOCAL to a plain hard-drive folder.
  </div>
  <div id="main">
    <div id="side"></div>
    <div id="detail"></div>
  </div>
</div>

<div id="overlay">
  <div id="modal">
    <h2>New Asset</h2>
    <label>GAME / PROJECT</label>
    <input type="text" id="mgame" list="gamelist" placeholder="e.g. PROJECTPLANET">
    <datalist id="gamelist"></datalist>
    <label>TYPE</label>
    <div class="catpick" id="mtypes"></div>
    <label>CATEGORY</label>
    <div class="catpick" id="mcats"></div>
    <input type="text" id="mcat" placeholder="…or type a custom category" style="margin-top:8px">
    <label>ASSET NAME</label>
    <input type="text" id="mname" placeholder="e.g. PlantMonster">
    <div style="margin-top:8px">
      <span id="batchtoggle" style="font-size:11px;color:var(--accent);cursor:pointer"
        onclick="toggleBatch()">⇢ Batch mode (create many at once)</span>
    </div>
    <textarea id="mbatch" placeholder="One asset name per line, e.g.&#10;Shroom_A1_T1_Puffcap&#10;Shroom_A1_T2_Glowveil&#10;Shroom_A1_T3_Lumenspore"
      style="display:none;width:100%;height:110px;margin-top:8px;background:var(--card);
      border:1px solid var(--line);border-radius:8px;color:var(--text);padding:9px 11px;
      font:12px 'Segoe UI';outline:none;resize:vertical"></textarea>
    <div id="merr"></div>
    <div class="mbtns">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn primary" onclick="createAsset()">Create</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<div id="kbar">
  <div id="kbox">
    <input id="kin" placeholder="Jump to asset…  (type to filter)" autocomplete="off">
    <div id="kres"></div>
    <div class="khint">↑↓ navigate &nbsp;·&nbsp; Enter open &nbsp;·&nbsp; Esc close &nbsp;·&nbsp; Ctrl+K anywhere</div>
  </div>
</div>

<div id="soverlay" style="position:fixed;inset:0;background:rgba(12,4,8,.6);backdrop-filter:blur(5px);
  display:none;align-items:center;justify-content:center;z-index:50">
  <div style="width:560px;max-height:84vh;overflow-y:auto;background:rgba(48,24,32,.95);
    border:1px solid var(--line);border-radius:18px;padding:28px" id="smodal">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <h2 style="font-size:18px">Settings</h2>
      <button class="btn" onclick="closeSettings()">✕</button>
    </div>
    <div id="sbody"><div class="dim" style="padding:20px 0">Loading…</div></div>
  </div>
</div>


<script>
/* ================= starfield ================= */
const cv=document.getElementById('stars'),cx=cv.getContext('2d');
let stars=[],shoot=null;
function sizeCanvas(){cv.width=innerWidth;cv.height=innerHeight;
  stars=Array.from({length:Math.floor(innerWidth*innerHeight/6500)},()=>({
    x:Math.random()*cv.width,y:Math.random()*cv.height,
    r:Math.random()*1.3+.3,p:Math.random()*Math.PI*2,
    s:.04+Math.random()*.12,v:.012+Math.random()*.03}));}
addEventListener('resize',sizeCanvas);sizeCanvas();
function tick(t){
  cx.clearRect(0,0,cv.width,cv.height);
  for(const s of stars){
    s.x-=s.s; if(s.x<-2)s.x=cv.width+2;
    const a=.35+.5*Math.abs(Math.sin(t*.001*s.v*60+s.p));
    cx.globalAlpha=a;cx.fillStyle='#ffd2cc';
    cx.beginPath();cx.arc(s.x,s.y,s.r,0,7);cx.fill();}
  if(!shoot&&Math.random()<.0035)
    shoot={x:Math.random()*cv.width*.7+cv.width*.2,y:Math.random()*cv.height*.3,l:0};
  if(shoot){shoot.l+=14;shoot.x+=14;shoot.y+=7;
    cx.globalAlpha=Math.max(0,1-shoot.l/240);
    const g=cx.createLinearGradient(shoot.x,shoot.y,shoot.x-46,shoot.y-23);
    g.addColorStop(0,'#fff');g.addColorStop(1,'transparent');
    cx.strokeStyle=g;cx.lineWidth=1.6;cx.beginPath();
    cx.moveTo(shoot.x,shoot.y);cx.lineTo(shoot.x-46,shoot.y-23);cx.stroke();
    if(shoot.l>240)shoot=null;}
  cx.globalAlpha=1;requestAnimationFrame(tick);}
requestAnimationFrame(tick);

/* ================= state ================= */
let S=null, sel=null;   // sel = {game,category,name}
const collapsed=new Set();
function toggleGame(name){collapsed.has(name)?collapsed.delete(name):collapsed.add(name);render();}
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(m,...a){return await window.pywebview.api[m](...a);}

async function refresh(keepSel=true){
  const s=await api('get_state');
  const h=JSON.stringify(s);
  const changed=h!==lastHash;
  lastHash=h;S=s;
  const gf=$('gamef'),cur=gf.value;
  gf.innerHTML='<option>All games</option>'+S.game_names.map(g=>`<option>${esc(g)}</option>`).join('');
  if([...gf.options].some(o=>o.value===cur))gf.value=cur;
  $('trapbanner').style.display=S.local_trap?'block':'none';
  if(keepSel&&sel&&!findAsset(sel))sel=null;
  if(changed){render();renderMain();}   // identical state -> leave the DOM alone
}
let view='detail';   // 'detail' | 'board' | 'journey' | 'library'
function renderMain(){view==='board'?renderBoard()
  :view==='journey'?renderJourney()
  :view==='library'?renderLibrary():renderDetail();}
function goHome(){view='detail';sel=null;render();renderDetail();}
function goBoard(){view='board';sel=null;render();renderBoard();}
function findAsset(ref){
  if(!S||!ref)return null;
  for(const g of S.games)if(g.name===ref.game)
    for(const c of g.categories)if(c.name===ref.category)
      for(const a of c.assets)if(a.name===ref.name)return a;
  return null;}

/* ================= sidebar ================= */
function typeFilter(){const el=$('typef');return el?el.value:'';}
function render(){
  const q=$('search').value.toLowerCase().trim(), gf=$('gamef').value, tf=typeFilter();
  let html='',count=0,delay=0;
  for(const g of S.games){
    if(gf!=='All games'&&g.name!==gf)continue;
    const cats=g.categories.map(c=>({...c,assets:c.assets.filter(a=>
        (!q||a.name.toLowerCase().includes(q))&&(!tf||a.type===tf))}))
      .filter(c=>c.assets.length);
    if(!cats.length)continue;
    const allG=g.categories.flatMap(c=>c.assets);
    const doneG=allG.filter(a=>a.current==='Done').length;
    const closed=collapsed.has(g.name);
    html+=`<div class="game-h${closed?' closed':''}" onclick="toggleGame('${escJs(g.name)}')">
      <span class="chev">▾</span>${esc(g.name.toUpperCase())}<span class="gstat">${doneG}/${allG.length} done</span></div>`;
    if(closed)continue;
    for(const c of cats){
      html+=`<div class="cat-h">${esc(c.name)}</div>`;
      for(const a of c.assets){
        count++;
        const isSel=sel&&sel.game===a.game&&sel.category===a.category&&sel.name===a.name;
        html+=`<div class="acard lv-${a.status_level}${a.progress===1?' fin':''}${isSel?' sel':''}" style="animation-delay:${delay}ms"
          data-key="${esc(a.game+'|'+a.category+'|'+a.name)}"
          onclick='selectAsset(${JSON.stringify({game:a.game,category:a.category,name:a.name})})'>
          ${a.thumb?`<img class="cthumb" src="${a.thumb}">`
            :a.wave?`<span class="cthumb wavemini">${miniWave(a.wave)}</span>`:''}
          <div class="cbody">
          <div class="r1"><span class="nm"><span class="tico">${a.icon||'◆'}</span>${esc(a.name)}</span><span class="stg">${esc(a.current)}${rvIcon(a)}</span></div>
          <div class="st ${a.status_level}">${esc(a.status)}</div>
          <div class="pbar"><i class="${a.progress===1?'full':''}" style="width:${a.progress*100}%"></i></div>
          </div>
        </div>`;
        delay+=28;}}}
  const sideEl=$('side'),sst=sideEl.scrollTop;
  sideEl.innerHTML=count?html:
    '<div class="empty">No assets found.<br>Use <b>＋ New Asset</b> to create your first one.</div>';
  sideEl.scrollTop=sst;
}

const keyOf=r=>r?r.game+'|'+r.category+'|'+r.name:'';
function markSel(){const k=keyOf(sel);
  document.querySelectorAll('.acard').forEach(el=>el.classList.toggle('sel',el.dataset.key===k));}
function selectAsset(ref){
  const fromBoard=view==='board';
  view='detail';sel=ref;
  if(fromBoard)render();else markSel();   // board -> rebuild sidebar sel; else just move highlight
  renderDetail();
  api('set_active',ref.game,ref.category,ref.name);  // fire & forget -> Blender/Painter plugins
}
function recalcAsset(a){
  const done=S.stages.filter(s=>a.stages[s]);
  a.current=done.length?[...S.stages].reverse().find(s=>a.stages[s]):'—';
  a.progress=done.length/S.stages.length;}
function updateCard(a){
  const el=document.querySelector(`.acard[data-key="${CSS.escape(keyOf(a))}"]`);
  if(!el)return;
  const stg=el.querySelector('.stg'),bar=el.querySelector('.pbar i');
  if(stg)stg.innerHTML=esc(a.current)+rvIcon(a);
  if(bar){bar.style.width=(a.progress*100)+'%';bar.classList.toggle('full',a.progress===1);}}

/* ================= waveform ================= */
function miniWave(p){
  if(!p||!p.length)return '';
  const n=Math.min(p.length,18),st=p.length/n;let b='';
  for(let i=0;i<n;i++){const v=Math.max(.08,p[Math.floor(i*st)]);
    b+=`<rect x="${(i*2.4).toFixed(1)}" y="${(11-v*10).toFixed(1)}" width="1.5"
      height="${(v*20).toFixed(1)}" rx=".7" fill="var(--accent)" opacity=".85"/>`;}
  return `<svg viewBox="0 0 ${n*2.4} 22" width="100%" height="100%">${b}</svg>`;
}
function bigWave(p){
  if(!p||!p.length)return '';
  const W=720,H=90;let b='';
  const bw=W/p.length;
  p.forEach((v,i)=>{const h=Math.max(2,v*(H-8));
    b+=`<rect x="${(i*bw).toFixed(2)}" y="${((H-h)/2).toFixed(2)}" width="${(bw*.62).toFixed(2)}"
      height="${h.toFixed(2)}" rx="${Math.min(2,bw*.3).toFixed(2)}" fill="url(#wg)"/>`;});
  return `<div class="wavebox"><svg viewBox="0 0 ${W} ${H}" width="100%">
    <defs><linearGradient id="wg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--accent2)"/><stop offset="1" stop-color="var(--accent)"/>
    </linearGradient></defs>
    <line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" stroke="rgba(220,130,155,.18)" stroke-width="1"/>
    ${b}</svg></div>`;
}

/* ================= review helpers ================= */
const RV_LABEL={none:'Not in review',pending:'Ready for review',approved:'Approved',changes:'Changes requested'};
function rvIcon(a){
  const st=a.review&&a.review.state;
  if(st==='pending')return ' <span title="ready for review" style="color:var(--warn)">⏳</span>';
  if(st==='approved')return ' <span title="approved" style="color:var(--ok)">✔</span>';
  if(st==='changes')return ' <span title="changes requested" style="color:var(--bad)">✗</span>';
  return '';}

/* ================= detail ================= */
function renderDashboard(){
  const all=[];
  for(const g of S.games)for(const c of g.categories)for(const a of c.assets)all.push(a);
  if(!all.length){
    const step=(n,t,d)=>`<div style="display:flex;gap:14px;align-items:flex-start;margin:12px 0;
      background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:12px;padding:14px 16px">
      <div style="width:26px;height:26px;border-radius:50%;background:var(--accent);color:#0a0c18;
        font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${n}</div>
      <div><div style="font-weight:700">${t}</div><div style="font-size:12px;color:var(--dim);margin-top:3px">${d}</div></div></div>`;
    $('detail').innerHTML=`
      <div class="dh"><h1>Welcome</h1></div>
      <div style="color:var(--dim);font-size:13px;margin:6px 0 14px">Three steps and your pipeline is live:</div>
      ${step(1,'Set your folders','⚙ Settings → pick a Local root (plain hard drive, e.g. D:/3DAssets) and a Cloud root (inside OneDrive/Drive).')}
      ${step(2,'Install the integrations','⚙ Settings → Install the Blender add-on and Painter plugin, enable them inside each app once.')}
      ${step(3,'Create your first asset','＋ New Asset (Ctrl+N) — or Batch mode to create a whole list at once.')}
      <div style="margin-top:14px"><button class="btn primary" onclick="openSettings()">⚙ Open Settings</button></div>`;
    return;}
  const stageCount={};
  let done=0,texOk=0,stale=0,unsynced=0,pending=0,approved=0,changes=0;
  for(const a of all){
    (stageCount[a.type]=stageCount[a.type]||{})[a.current]=
      ((stageCount[a.type]||{})[a.current]||0)+1;
    if(a.current==='Done')done++;
    if(a.tex&&a.tex.complete)texOk++;
    if(a.status_level==='bad')stale++;
    if(!a.last_sync)unsynced++;
    const rs=a.review&&a.review.state;
    if(rs==='pending')pending++;else if(rs==='approved')approved++;else if(rs==='changes')changes++;}
  const games={};for(const a of all)games[a.game]=(games[a.game]||0)+1;
  const big=(n,l,cls)=>`<div class="tile"><div class="tn ${cls||''}">${n}</div><div class="tl">${l}</div></div>`;
  const typesPresent=Object.keys(S.types||{'3D':1}).filter(t=>all.some(a=>a.type===t));
  const stageBars=typesPresent.map(t=>{
    const ti=(S.types||{})[t]||{label:t,icon:'◆',stages:S.stages};
    const inT=all.filter(a=>a.type===t),cnt=stageCount[t]||{};
    return (typesPresent.length>1
      ? `<div style="font-size:11px;color:var(--accent2);font-weight:700;margin:12px 0 4px">
           ${ti.icon} ${esc(ti.label.toUpperCase())} · ${inT.length}</div>`:'')
      + ti.stages.map(s=>{
        const n=cnt[s]||0,w=inT.length?n/inT.length*100:0;
        return `<div style="display:flex;align-items:center;gap:10px;margin:5px 0">
          <span style="width:80px;font-size:11.5px;color:var(--dim)">${esc(s)}</span>
          <div class="pbar" style="flex:1"><i style="width:${w}%"></i></div>
          <span style="width:26px;text-align:right;font-size:11.5px">${n}</span></div>`;}).join('');
  }).join('');
  const gameRows=Object.entries(games).sort((a,b)=>b[1]-a[1]).map(([g,n])=>
    `<div style="display:flex;justify-content:space-between;padding:7px 12px;margin:3px 0;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:9px;font-size:12.5px"><span style="color:var(--game);font-weight:700">${esc(g)}</span><span>${n} assets</span></div>`).join('');
  const staleList=all.filter(a=>a.status_level==='bad');
  const needs=[];
  for(const a of all){
    if(a.status_level==='bad'&&staleList.length===1)needs.push([a,esc(a.status),'bad']);
    if(a.review&&a.review.state==='changes')
      needs.push([a,'changes requested'+(a.review.by?` by ${esc(a.review.by)}`:'')+
        (a.review.note?` — “${esc(a.review.note)}”`:''),'bad']);
    if(a.tex&&!a.tex.complete&&a.tex.count>0)
      needs.push([a,`textures incomplete — missing ${a.tex.missing.join(', ')}`,'warn']);}
  const staleRow=staleList.length>1?`
    <div class="frow" style="cursor:pointer" onclick="goBoard()">
      <span class="fl" style="width:auto">⚠ ${staleList.length} stale LP exports</span>
      <span class="fi dim">${esc(staleList.slice(0,4).map(a=>a.name).join(', '))}${staleList.length>4?` +${staleList.length-4} more`:''} — flagged on the Board</span></div>`:'';
  const needRows=staleRow+needs.map(([a,msg,lvl])=>
    `<div class="frow" style="cursor:pointer" onclick='selectAsset(${JSON.stringify({game:a.game,category:a.category,name:a.name})})'>
      <span class="fl" style="width:auto">${esc(a.name)}</span>
      <span class="fi ${lvl}">${msg}</span>
      <span style="font-size:11px;color:var(--dim);white-space:nowrap">${esc(a.game)} / ${esc(a.category)}</span></div>`).join('');
  const queueRows=all.filter(a=>a.review&&a.review.state==='pending').map(a=>
    `<div class="frow" style="cursor:pointer" onclick='selectAsset(${JSON.stringify({game:a.game,category:a.category,name:a.name})})'>
      <span class="fl" style="width:auto">${esc(a.name)}</span>
      <span class="fi warn">awaiting review${a.review.by?` · marked by ${esc(a.review.by)} · ${esc(a.review.time)}`:''}</span>
      <span style="font-size:11px;color:var(--dim);white-space:nowrap">${esc(a.game)} / ${esc(a.category)}</span></div>`).join('');
  const pct=all.length?Math.round(done/all.length*100):0;
  const R=40,CIRC=2*Math.PI*R;
  $('detail').innerHTML=`
    <div class="dh" style="justify-content:space-between">
      <h1>Overview</h1>
      <div class="radial">
        <svg width="96" height="96">
          <circle cx="48" cy="48" r="${R}" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="8"/>
          <circle cx="48" cy="48" r="${R}" fill="none" stroke="${pct===100?'var(--ok)':'var(--accent)'}"
            stroke-width="8" stroke-linecap="round"
            style="filter:drop-shadow(0 0 6px ${pct===100?'rgba(70,214,140,.7)':'rgba(255,95,120,.7)'})"
            stroke-dasharray="${CIRC*pct/100} ${CIRC}"/>
        </svg>
        <div class="rtxt">${pct}%<span class="rsub">COMPLETE</span></div>
      </div>
    </div>
    <div style="display:flex;gap:10px;margin:16px 0">
      ${big(all.length,'TOTAL ASSETS')}${big(done,'DONE','ok')}
      ${big(texOk,'PBR COMPLETE','ok')}${big(stale,'STALE EXPORTS',stale?'bad':'')}
      ${big(unsynced,'NEVER SYNCED',unsynced?'warn':'')}
    </div>
    <div style="display:flex;gap:10px;margin:0 0 16px">
      ${big(pending,'AWAITING REVIEW',pending?'warn':'')}
      ${big(approved,'APPROVED','ok')}
      ${big(changes,'CHANGES REQUESTED',changes?'bad':'')}
    </div>
    ${needRows?`<div class="sec">NEEDS ATTENTION</div>${needRows}`:''}
    ${queueRows?`<div class="sec">REVIEW QUEUE</div>${queueRows}`:''}
    <div class="sec">BY STAGE</div>${stageBars}
    <div class="sec">BY GAME</div>${gameRows}`;}

let lastDetailKey='';
function renderDetail(){
  const a=findAsset(sel);
  if(!a){renderDashboard();lastDetailKey='home';return;}
  const steps=a.steps||S.stages;
  const lastDone=[...steps].reverse().find(s=>a.stages[s]);
  let chips=steps.map((s,i)=>{
    const done=!!a.stages[s];
    return `<div class="step${done?' done':''}${s===lastDone?' cur':''}" onclick="toggleStage('${esc(s)}')"
      title="${done?'done '+esc(a.stages[s]):'click to mark done'}">
      <div class="sdot">${done?'✓':i+1}</div><div class="slabel">${esc(s)}</div></div>`;}).join('');
  let files=a.files.map(f=>{
    let fi, cls='';
    if(f.label==='Textures'&&a.tex){
      if(a.tex.complete){fi=`PBR set complete ✓ &nbsp;(${a.tex.count} files)`;cls=' ok';}
      else if(a.tex.count===0){fi='no maps yet';cls=' miss';}
      else{fi=`missing: ${a.tex.missing.join(', ')} &nbsp;(${a.tex.count} files)`;cls=' warn';}
    } else if(f.label==='Final FBX'&&f.file){
      const nt=a.final?a.final.tex:0;
      fi=`${esc(f.file.name)} &nbsp;·&nbsp; ${f.file.time}`+
        (nt?` &nbsp;·&nbsp; ${nt} texture${nt>1?'s':''} bundled ✓`:' &nbsp;·&nbsp; no textures bundled — re-run Export Final');
      cls=nt?' ok':' warn';
    } else {
      fi=f.file?`${esc(f.file.name)} &nbsp;·&nbsp; ${f.file.time}`:'not found';
      cls=f.file?'':' miss';
    }
    const isAudio=f.file&&/\.(wav|ogg|mp3|flac|aiff?)$/i.test(f.file.name);
    return `<div class="frow"><span class="fl">${esc(f.label)}</span>
      <span class="fi${cls}">${fi}</span>
      ${isAudio?`<button class="sbtn play" onclick="playFile(this,'${escJs(f.file.path)}')">▶</button>`:''}
      ${f.file?`<button class="sbtn open" onclick="openP('${escJs(f.file.path)}')">Open</button>`:''}
      ${f.file?`<button class="sbtn" onclick="renameFile('${escJs(f.file.path)}','${escJs(f.file.name)}')">Rename</button>`:''}
      ${f.folder?`<button class="sbtn" onclick="openP('${escJs(f.folder)}')">Folder</button>`
                :`<button class="sbtn" onclick="mkFolder('${esc(f.sub)}')">＋ Create</button>`}
    </div>`;}).join('');
  const todos=(a.todos||[]).map(t=>`
    <div class="todo${t.done?' done':''}">
      <div class="cb" onclick="toggleTodo('${t.id}')">✓</div>
      <span class="tx">${esc(t.text)}</span>
      <button class="del" onclick="delTodo('${t.id}')">✕</button>
    </div>`).join('');
  const open=(a.todos||[]).filter(t=>!t.done).length;
  const dEl=$('detail'),dKey=keyOf(sel),sameAsset=dKey===lastDetailKey,dst=dEl.scrollTop;
  const rv=a.review||{state:'none',by:'',note:'',time:'',history:[]};
  const rvBtn=(st,label,cls)=>rv.state===st?'':
    `<button class="btn ${cls||''}" onclick="setReview('${st}')">${label}</button>`;
  const rvHist=(rv.history||[]).slice(0,-1).slice(-3).reverse().map(h=>
    `<div>${esc(RV_LABEL[h.state]||h.state)} — ${esc(h.by)} · ${esc(h.time)}${h.note?` · “${esc(h.note)}”`:''}</div>`).join('');
  const rvSec=`
    <div class="sec">REVIEW</div>
    <div class="rvbox">
      <div class="rvhead">
        <span class="rvstate ${rv.state}">${RV_LABEL[rv.state]||esc(rv.state)}</span>
        ${rv.by?`<span class="rvmeta">${esc(rv.by)} · ${esc(rv.time)}</span>`
               :`<span class="rvmeta">Mark it ready once the asset is engine-ready — approval unlocks Sync → Cloud.</span>`}
      </div>
      ${rv.note?`<div class="rvnote">“${esc(rv.note)}”</div>`:''}
      <input type="text" id="rvnote" placeholder="Optional note — what to check / what to fix… ">
      <div class="rvbtns">
        ${rvBtn('pending','🕐 Ready for Review')}
        ${rvBtn('approved','✔ Approve','primary')}
        ${rvBtn('changes','✗ Request Changes')}
        ${rv.state!=='none'?`<button class="sbtn" onclick="setReview('none')">Reset</button>`:''}
      </div>
      ${rvHist?`<div class="rvhist">${rvHist}</div>`:''}
    </div>`;
  $('detail').innerHTML=`
    <div class="dh">${a.thumb?`<img class="hthumb" src="${a.thumb}">`:''}<h1>${esc(a.name)}</h1>
      <span class="badge type">${a.icon||'◆'} ${esc(a.type_label||'3D Model')}</span>
      <span class="badge">${esc(a.game)}</span><span class="badge cat">${esc(a.category)}</span></div>
    <div class="dstatus ${a.status_level}">● ${esc(a.status)}</div>
    ${a.wave?`<div class="sec">WAVEFORM</div>${bigWave(a.wave)}`:''}
    <div class="sec">PIPELINE</div><div class="stepper">${chips}</div>
    <div class="sec">FILES</div>${files}
    <div class="sec">ROBLOX PREFLIGHT</div>
    <div id="pfbox">${pfCache[dKey]||`<button class="btn" onclick="runPreflight()">🚀 Run Preflight</button>
      <span class="dim" style="font-size:11.5px;margin-left:8px">tri count + texture caps, read straight from the files</span>`}</div>
    ${rvSec}
    <div class="sec">TODO ${open?`<span style="color:var(--accent)">· ${open} open</span>`:''}</div>
    <div id="todoadd"><input type="text" id="todoin" placeholder="Add a task… (Enter)"
      onkeydown="if(event.key==='Enter')addTodo()">
      <button class="btn" onclick="addTodo()">＋</button></div>
    ${todos||'<div class="dim" style="font-size:12px;padding:4px 2px">No tasks yet.</div>'}
    <div class="actions">
      <button class="btn primary" onclick="openP('${escJs(a.path)}')">🗀 Open Asset Folder</button>
      <button class="btn sync" id="syncbtn" onclick="doSync()">☁ Sync → Cloud</button>
    </div>
    <div id="syncline">Last sync: ${a.last_sync?esc(a.last_sync):'never'}</div>
    <div class="sec">NOTES</div>
    <textarea id="notes" placeholder="Notes…" onblur="saveNotes()">${esc(a.notes)}</textarea>`;
  if(sameAsset)dEl.scrollTop=dst;   // re-render of the same asset keeps scroll position
  lastDetailKey=dKey;
}
function escJs(s){return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");}

/* ================= kanban board ================= */
let dragRef=null;
function renderBoard(){
  const q=$('search').value.toLowerCase().trim(), gf=$('gamef').value, tf=typeFilter();
  const pool=allAssets().filter(a=>
    (gf==='All games'||a.game===gf)&&(!q||a.name.toLowerCase().includes(q))&&(!tf||a.type===tf));
  const types=Object.keys(S.types||{'3D':1}).filter(t=>pool.some(a=>a.type===t));
  if(!types.length){$('detail').innerHTML=
    '<div class="empty">No assets match this filter.</div>';return;}
  // one lane per type — the two pipelines have different stages, so they can't share columns
  $('detail').innerHTML=types.map(t=>{
    const ti=S.types[t]||{label:t,icon:'◆',stages:S.stages};
    return (types.length>1?`<div class="sec">${ti.icon} ${esc(ti.label.toUpperCase())}</div>`:'')
      +`<div class="board${types.length>1?' lane':''}">${boardCols(pool.filter(a=>a.type===t),ti.stages)}</div>`;
  }).join('');
}
function boardCols(all,stages){
  const cols=stages.map((s,i)=>{
    const cards=all.filter(a=>(a.current==='—'?stages[0]:a.current)===s);
    const cardHtml=cards.map((a,j)=>`
      <div class="kcard" draggable="true" style="animation-delay:${j*24}ms"
        ondragstart='kDragStart(event,${JSON.stringify({game:a.game,category:a.category,name:a.name})})'
        ondragend="kDragEnd(event)"
        onclick='selectAsset(${JSON.stringify({game:a.game,category:a.category,name:a.name})})'>
        ${a.thumb?`<img src="${a.thumb}">`:''}
        <div class="kt"><span>${esc(a.name)}</span><span>${rvIcon(a)}</span></div>
        <div class="kg">${esc(a.game)} / ${esc(a.category)}</div>
        ${a.status_level==='bad'?`<div class="kg bad">⚠ stale export</div>`:''}
      </div>`).join('');
    return `<div class="kcol${s==='Done'?' done-col':''}" data-stage="${esc(s)}"
      ondragover="kDragOver(event)" ondragleave="kDragLeave(event)" ondrop="kDrop(event,'${esc(s)}')">
      <h3><span>${i+1} · ${esc(s.toUpperCase())}</span><span class="kn">${cards.length}</span></h3>
      <div class="kcards">${cardHtml}</div>
    </div>`;}).join('');
  return cols;
}
function kDragStart(e,ref){dragRef=ref;e.target.classList.add('ghost');
  e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',ref.name);}catch(_){}}
function kDragEnd(e){e.target.classList.remove('ghost');
  document.querySelectorAll('.kcol.drag').forEach(c=>c.classList.remove('drag'));}
function kDragOver(e){e.preventDefault();e.dataTransfer.dropEffect='move';
  e.currentTarget.classList.add('drag');}
function kDragLeave(e){e.currentTarget.classList.remove('drag');}
async function kDrop(e,stage){
  e.preventDefault();e.currentTarget.classList.remove('drag');
  if(!dragRef)return;
  const ref=dragRef;dragRef=null;
  const ok=await api('set_stage',ref.game,ref.category,ref.name,stage);
  if(!ok){toast('Could not move '+ref.name);return;}
  toast(`${ref.name} → ${stage}`);
  await refresh();
}

/* ================= image library ================= */
let L=null,libRel='',libQ='',libDrag={};
async function goLibrary(rel){
  view='library';sel=null;render();
  if(rel!==undefined)libRel=rel;
  if(!L)$('detail').innerHTML='<div class="dim" style="padding:30px">Loading library…</div>';
  const r=await api('library_list',libRel);
  if(!r.ok){$('detail').innerHTML=`<div class="empty">${esc(r.error)}</div>`;return;}
  L=r;libRel=r.rel;
  if(r.created)toast('Library created — starter collections added');
  renderLibrary();
}
function libFilter(v){libQ=v;renderLibrary();}
function renderLibrary(){
  if(!L){goLibrary();return;}
  const q=libQ.toLowerCase().trim();
  const folders=L.folders.filter(f=>!q||f.name.toLowerCase().includes(q));
  const files=L.files.filter(f=>!q||f.name.toLowerCase().includes(q));
  const crumbs=`<span class="crumb" onclick="goLibrary('')">🖼 Library</span>`+
    L.crumbs.map(c=>` <span class="csep">›</span> <span class="crumb"
      onclick="goLibrary('${escJs(c.rel)}')">${esc(c.name)}</span>`).join('');
  const fold=folders.map(f=>`
    <div class="libfolder" ondblclick="goLibrary('${escJs(f.rel)}')" onclick="goLibrary('${escJs(f.rel)}')">
      <div class="lfico">🗀</div>
      <div class="lfname">${esc(f.name)}</div>
      <div class="lfcount">${f.count} image${f.count===1?'':'s'}</div>
    </div>`).join('');
  const grid=files.map(f=>`
    <div class="libcard" draggable="true" data-path="${esc(f.path)}"
      onmouseenter="libPrefetch('${escJs(f.path)}')"
      ondragstart="libDragStart(event,'${escJs(f.path)}','${escJs(f.name)}')"
      ondblclick="libOpen('${escJs(f.path)}')">
      <div class="libthumb">${f.thumb?`<img src="${f.thumb}" draggable="false">`
        :`<span class="noprev">${esc(f.ext.replace('.','').toUpperCase())}</span>`}</div>
      <div class="libname" title="${esc(f.name)}"
        onclick="libRename(event,'${escJs(f.path)}','${escJs(f.name)}')">${esc(f.name)}</div>
      <div class="libmeta">${f.dim?esc(f.dim):''}${f.dim?' · ':''}${esc(f.size)}</div>
      <div class="libacts">
        <button class="sbtn" onclick="event.stopPropagation();libOpen('${escJs(f.path)}')">Open</button>
        <button class="sbtn" onclick="event.stopPropagation();libReveal('${escJs(f.path)}')" title="Show in Explorer — drag from there into any app">Reveal</button>
        <button class="sbtn" onclick="event.stopPropagation();libCopy('${escJs(f.path)}')" title="Copy the file to the clipboard">Copy</button>
        <button class="sbtn" onclick="event.stopPropagation();libRename(event,'${escJs(f.path)}','${escJs(f.name)}')">Rename</button>
      </div>
    </div>`).join('');
  $('detail').innerHTML=`
    <div class="dh" style="justify-content:space-between;flex-wrap:wrap;gap:10px">
      <div><h1 style="font-size:21px">${crumbs}</h1>
        <div class="dim" style="font-size:11px;margin-top:3px">${esc(L.root)}</div></div>
      <div style="display:flex;gap:7px;align-items:center">
        <input type="text" id="libq" placeholder="Filter…" value="${esc(libQ)}"
          style="width:140px" oninput="libFilter(this.value)">
        <button class="btn" onclick="libNewFolder()">＋ Collection</button>
        <button class="btn primary" onclick="libImport()">⭳ Add images</button>
      </div>
    </div>
    <div class="dim" style="font-size:12px;margin:2px 0 14px">
      Drag a tile straight into Blender, Painter or Photoshop — or hit <b>Reveal</b> and drag from Explorer.
      Click a name to rename it.</div>
    ${fold?`<div class="sec">COLLECTIONS</div><div class="libfolders">${fold}</div>`:''}
    ${files.length?`<div class="sec">IMAGES <span style="color:var(--accent)">· ${files.length}</span></div>
      <div class="libgrid">${grid}</div>`
      :`<div class="dim" style="font-size:12.5px;padding:10px 2px">
        ${q?'Nothing matches that filter.':'No images here yet — use <b>⭳ Add images</b> to bring some in.'}</div>`}`;
}
async function libPrefetch(p){
  if(libDrag[p]!==undefined)return;
  libDrag[p]=null;
  const r=await api('library_data',p);
  libDrag[p]=r.ok?r:null;
}
function libDragStart(ev,p,name){
  const d=libDrag[p];
  try{
    ev.dataTransfer.effectAllowed='copy';
    ev.dataTransfer.setData('text/plain',p);
    ev.dataTransfer.setData('text/uri-list','file:///'+p.replace(/\\/g,'/'));
    if(d&&d.uri)ev.dataTransfer.setData('DownloadURL',`${d.mime}:${name}:${d.uri}`);
  }catch(_){}
}
async function libOpen(p){await api('open_path',p);}
async function libReveal(p){await api('library_reveal',p);}
async function libCopy(p){
  const r=await api('library_copy_file',p);
  toast(r.ok?'File copied to clipboard ✓':r.error);
}
function libRename(ev,p,cur){
  if(ev)ev.stopPropagation();
  const card=document.querySelector(`.libcard[data-path="${CSS.escape(p)}"]`);
  if(!card)return;
  const el=card.querySelector('.libname');
  if(el.querySelector('input'))return;
  const dot=cur.lastIndexOf('.');
  el.innerHTML=`<input class="libedit" type="text" value="${esc(cur)}"
    onkeydown="libRenameKey(event,'${escJs(p)}')" onblur="renderLibrary()">`;
  const inp=el.querySelector('input');
  inp.focus();
  inp.setSelectionRange(0,dot>0?dot:cur.length);   // select the stem, keep the extension
}
async function libRenameKey(ev,p){
  if(ev.key==='Escape'){renderLibrary();return;}
  if(ev.key!=='Enter')return;
  const v=ev.target.value;
  ev.target.onblur=null;
  const r=await api('library_rename',p,v);
  if(!r.ok){toast(r.error);renderLibrary();return;}
  delete libDrag[p];
  toast('Renamed → '+r.name);
  await goLibrary(libRel);
}
async function libNewFolder(){
  const name=prompt('New collection name:');
  if(!name)return;
  const r=await api('library_new_folder',libRel,name);
  if(!r.ok){toast(r.error);return;}
  await goLibrary(libRel);
}
async function libImport(){
  const r=await api('library_import',libRel);
  if(!r.ok){toast(r.error);return;}
  if(r.copied)toast(`Added ${r.copied} image${r.copied===1?'':'s'}`+(r.skipped?` · ${r.skipped} skipped`:''));
  await goLibrary(libRel);
}

/* ================= roblox dev journey ================= */
const RANKS=[
 {name:'Wood',       thr:0,        c1:'#8a5a2b',c2:'#4e3012',c3:'#d29a5b',sub:'The journey begins'},
 {name:'Stone',      thr:500,      c1:'#a7adb6',c2:'#565d66',c3:'#e8edf3',sub:'First sales — it actually works'},
 {name:'Bronze',     thr:2500,     c1:'#cd7f32',c2:'#7a431a',c3:'#f0b06a',sub:'Real income now'},
 {name:'Silver',     thr:10000,    c1:'#c8cfd9',c2:'#78818e',c3:'#ffffff',sub:'The shop is rolling'},
 {name:'Gold',       thr:30000,    c1:'#f2c94c',c2:'#a4721c',c3:'#ffe9a8',sub:'DevEx eligible — 30k minimum reached'},
 {name:'Platinum',   thr:100000,   c1:'#dff3f5',c2:'#7fa7ad',c3:'#ffffff',sub:'First payout territory — ≈ $350'},
 {name:'Emerald',    thr:250000,   c1:'#4fd483',c2:'#1e6f45',c3:'#c5f5d8',sub:'≈ $875 — steady income'},
 {name:'Diamond',    thr:500000,   c1:'#8fe4ff',c2:'#2e6fd9',c3:'#e6fbff',sub:'≈ $1,750 cashed out'},
 {name:'Master',     thr:1000000,  c1:'#b48cff',c2:'#552b96',c3:'#e9dcff',sub:'Robux millionaire — ≈ $3,500'},
 {name:'Elite',      thr:2000000,  c1:'#37d6c5',c2:'#116e63',c3:'#c8fff5',sub:'2M — ≈ $7,000'},
 {name:'Champion',   thr:3500000,  c1:'#ff9c42',c2:'#9c4c0e',c3:'#ffd9b0',sub:'≈ $12,250 — proper side income'},
 {name:'Grandmaster',thr:5000000,  c1:'#ff6b81',c2:'#8e2130',c3:'#ffd0d6',sub:'≈ $17,500 — this is a job now'},
 {name:'Mythic',     thr:8000000,  c1:'#ff6bd8',c2:'#8e1f6e',c3:'#ffd0f2',sub:'≈ $28,000 — top-creator territory'},
 {name:'Titan',      thr:12000000, c1:'#8fa3d9',c2:'#303f6e',c3:'#dfe7ff',sub:'≈ $42,000 — studios know your name'},
 {name:'Legend',     thr:20000000, c1:'#ffe27a',c2:'#b8860b',c3:'#fffbe8',sub:'20M — ≈ $70,000. Full-time Roblox dev.'},
];
const RNUM=['I','II','III','IV','V','VI','VII','VIII','IX','X','XI','XII','XIII','XIV','★'];
function rankIdx(total){let i=0;RANKS.forEach((r,j)=>{if(total>=r.thr)i=j;});return i;}
function badgeSvg(i,size,locked){
  const r=RANKS[i],k='bd'+i+'_'+size;
  return `<svg width="${size}" height="${Math.round(size*1.1)}" viewBox="0 0 100 110"
    style="${locked?'filter:grayscale(1);opacity:.3':''}">
   <defs>
    <linearGradient id="${k}a" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="${r.c1}"/><stop offset="1" stop-color="${r.c2}"/></linearGradient>
    <linearGradient id="${k}b" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="${r.c2}"/><stop offset=".5" stop-color="${r.c3}"/><stop offset="1" stop-color="${r.c2}"/></linearGradient>
   </defs>
   <path d="M50 4 L91 19 V57 C91 81 50 105 50 105 C50 105 9 81 9 57 V19 Z"
     fill="url(#${k}a)" stroke="url(#${k}b)" stroke-width="2.6"/>
   <path d="M50 12 L84 24.5 V56 C84 75 50 96 50 96 C50 96 16 75 16 56 V24.5 Z"
     fill="none" stroke="rgba(255,255,255,.28)" stroke-width="1.2"/>
   <path d="M50 4 L91 19 V30 L50 17 L9 30 V19 Z" fill="rgba(255,255,255,.13)"/>
   <g transform="rotate(45 50 54)">
     <rect x="33" y="37" width="34" height="34" rx="8" fill="rgba(6,8,18,.4)" stroke="${r.c3}" stroke-width="2"/>
   </g>
   <text x="50" y="${'$★'.includes(RNUM[i])?62:59}" text-anchor="middle" font-family="Segoe UI"
     font-weight="700" font-size="${'$★'.includes(RNUM[i])?24:RNUM[i].length>3?9.5:RNUM[i].length>2?11.5:15}"
     fill="${r.c3}">${RNUM[i]}</text>
  </svg>`;
}
let J=null,T=null,jRange=90,jScope='totals';
const DEVEX_USD=0.0035;
async function goJourney(){
  view='journey';sel=null;render();
  if(!J)$('detail').innerHTML='<div class="dim" style="padding:30px">Loading…</div>';
  const [r,t]=await Promise.all([api('journey_get'),api('timelog_get')]);
  if(r.ok)J=r;
  if(t.ok)T=t;
  renderJourney();
}
function setRange(d){jRange=d;renderJourney();}
function setScope(s){jScope=s;renderJourney();}
const fmtUSD=n=>'$'+Math.round(n).toLocaleString('en-US');
const fmtH=h=>(h>=10?Math.round(h):Math.round(h*10)/10)+' h';

/* ---- cumulative earnings chart: one series, step line + area, hover crosshair ---- */
function jSvg(entries){
  if(!entries.length)return `<div class="dim" style="padding:26px 4px;font-size:12.5px">
    No earnings logged yet — add your first entry above and the graph starts drawing.</div>`;
  const W=720,H=232,PL=62,PR=16,PT=16,PB=28;
  const now=new Date();now.setHours(23,59,0,0);
  const t1=now.getTime();
  let cum=0;const all=entries.map(e=>{cum+=e.amount;
    return {t:new Date(e.date+'T12:00:00').getTime(),v:cum,a:e.amount,note:e.note,date:e.date};});
  const total=cum;
  const t0=jRange?Math.min(t1-jRange*864e5,t1-864e5):Math.min(all[0].t,t1-864e5);
  let base=0;for(const p of all)if(p.t<t0)base=p.v;
  const inR=all.filter(p=>p.t>=t0);
  const pts=[{t:t0,v:base,edge:1},...inR];
  if(pts[pts.length-1].t<t1)pts.push({t:t1,v:total,edge:1});
  const vmax=Math.max(total,1),span=Math.max(1,t1-t0);
  const X=t=>PL+((t-t0)/span)*(W-PL-PR), Y=v=>PT+(1-v/vmax)*(H-PT-PB);
  let d='';pts.forEach((p,i)=>{d+=i?` H${X(p.t).toFixed(1)} V${Y(p.v).toFixed(1)}`
    :`M${X(p.t).toFixed(1)} ${Y(p.v).toFixed(1)}`;});
  const area=d+` V${Y(0).toFixed(1)} H${X(t0).toFixed(1)} Z`;
  let grid='';for(let i=0;i<=3;i++){const v=vmax*i/3,y=Y(v);
    grid+=`<line x1="${PL}" y1="${y.toFixed(1)}" x2="${W-PR}" y2="${y.toFixed(1)}"
      stroke="rgba(220,130,155,.13)" stroke-width="1"/>
      <text x="${PL-8}" y="${(y+3.5).toFixed(1)}" text-anchor="end" font-size="9.5"
        fill="var(--dim)" font-family="Segoe UI">${fmtC(Math.round(v))}</text>`;}
  const dfmt=t=>{const x=new Date(t);return ('0'+x.getDate()).slice(-2)+'.'+('0'+(x.getMonth()+1)).slice(-2)+'.';};
  let xl='';for(let i=0;i<=3;i++){const t=t0+span*i/3;
    xl+=`<text x="${X(t).toFixed(1)}" y="${H-8}" text-anchor="${i===0?'start':i===3?'end':'middle'}"
      font-size="9.5" fill="var(--dim)" font-family="Segoe UI">${dfmt(t)}</text>`;}
  const dots=inR.map(p=>`<circle cx="${X(p.t).toFixed(1)}" cy="${Y(p.v).toFixed(1)}" r="4"
     fill="var(--accent)" stroke="rgba(12,4,8,.9)" stroke-width="2"/>`).join('');
  const hot=inR.map(p=>`${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)},${p.v},${p.a},${p.date}`).join(';');
  return `<div class="jchartwrap">
   <svg viewBox="0 0 ${W} ${H}" width="100%" id="jsvg" data-pts="${hot}"
     onmousemove="jHover(event)" onmouseleave="jHide()">
    <defs><linearGradient id="jg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--accent)" stop-opacity=".34"/>
      <stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    ${grid}${xl}
    <path d="${area}" fill="url(#jg)"/>
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <line id="jcross" x1="0" y1="${PT}" x2="0" y2="${H-PB}" stroke="var(--accent)"
      stroke-width="1" stroke-dasharray="3 3" opacity="0"/>
    ${dots}
   </svg>
   <div id="jtip"></div></div>`;
}
function jHover(ev){
  const svg=$('jsvg'),tip=$('jtip'),cross=$('jcross');
  if(!svg||!svg.dataset.pts)return;
  const r=svg.getBoundingClientRect(),sx=720/r.width;
  const mx=(ev.clientX-r.left)*sx;
  let best=null,bd=1e9;
  for(const s of svg.dataset.pts.split(';')){
    if(!s)continue;const[x,y,v,a,dt]=s.split(',');
    const dd=Math.abs(parseFloat(x)-mx);
    if(dd<bd){bd=dd;best={x:parseFloat(x),y:parseFloat(y),v:+v,a:+a,dt};}}
  if(!best||bd>34){jHide();return;}
  cross.setAttribute('x1',best.x);cross.setAttribute('x2',best.x);cross.setAttribute('opacity','.8');
  const px=best.x/sx,py=best.y/sx;
  tip.innerHTML=`<b>${esc(best.dt.split('-').reverse().join('.'))}</b><br>
    +${best.a.toLocaleString('en-US')} R$<br>
    <span class="dim">total ${fmtR(best.v)} · ${fmtUSD(best.v*DEVEX_USD)}</span>`;
  tip.style.display='block';
  tip.style.left=Math.max(4,Math.min(px-tip.offsetWidth/2,r.width-tip.offsetWidth-4))+'px';
  tip.style.top=Math.max(0,py-tip.offsetHeight-12)+'px';
}
function jHide(){const t=$('jtip'),c=$('jcross');if(t)t.style.display='none';
  if(c)c.setAttribute('opacity','0');}
const fmtR=n=>'R$ '+(n||0).toLocaleString('en-US');
const fmtC=n=>n>=1e6?(Math.round(n/1e5)/10)+'M':n>=1e4?Math.round(n/1e3)+'k'
  :n>=1000?(Math.round(n/100)/10)+'k':''+Math.round(n);
function renderJourney(){
  if(!J){goJourney();return;}
  const total=J.total,i=rankIdx(total),r=RANKS[i],next=RANKS[i+1]||null;
  const pct=next?Math.min(100,((total-r.thr)/(next.thr-r.thr))*100):100;
  const ladder=RANKS.map((x,j)=>`
    <div class="jrank${j===i?' cur':''}${j>i?' locked':''}" title="${esc(x.sub)}">
      ${badgeSvg(j,52,j>i)}
      <div class="jrn" ${j===i?`style="color:${x.c3}"`:''}>${x.name}</div>
      <div class="jrt">${fmtC(x.thr)} R$</div>
    </div>`).join('');
  // ---- stat tiles: lifetime, DevEx value, this month, monthly average
  const nowD=new Date(),mKey=nowD.toISOString().slice(0,7);
  const monthSum=J.entries.filter(e=>e.date.slice(0,7)===mKey)
    .reduce((s,e)=>s+e.amount,0);
  const first=J.entries.length?new Date(J.entries[0].date):nowD;
  const months=Math.max(1,(nowD.getFullYear()-first.getFullYear())*12+nowD.getMonth()-first.getMonth()+1);
  const statTiles=`<div style="display:flex;gap:10px;margin:14px 0 2px">
    <div class="tile"><div class="tn">${fmtC(total)}</div><div class="tl">LIFETIME R$</div></div>
    <div class="tile"><div class="tn ok">${fmtUSD(total*DEVEX_USD)}</div><div class="tl">DEVEX VALUE</div></div>
    <div class="tile"><div class="tn">${fmtC(monthSum)}</div><div class="tl">THIS MONTH R$</div></div>
    <div class="tile"><div class="tn">${fmtC(Math.round(total/months))}</div><div class="tl">AVG / MONTH</div></div>
  </div>`;
  const rangeBtns=[[30,'30d'],[90,'90d'],[365,'1y'],[0,'All']].map(([d,l])=>
    `<button class="sbtn${jRange===d?' on':''}" onclick="setRange(${d})">${l}</button>`).join('');
  const scopeBtns=[['today','Today'],['week','7 days'],['totals','All time']].map(([s,l])=>
    `<button class="sbtn${jScope===s?' on':''}" onclick="setScope('${s}')">${l}</button>`).join('');
  // ---- hours: labelled bars, identity carried by the text not the colour
  const HC={'Blender':'#ff9c42','ZBrush':'#c8cfd9','Substance Painter':'#ff5f78',
            'Substance Designer':'#b48cff','Cinema 4D':'#4fd8d8'};
  const hrs=(T&&T[jScope])||{};
  const hkeys=Object.keys(hrs).filter(k=>hrs[k]>0.01).sort((a,b)=>hrs[b]-hrs[a]);
  const hmax=Math.max(...hkeys.map(k=>hrs[k]),0.1);
  const totH=hkeys.reduce((s,k)=>s+hrs[k],0);
  const hoursBlock=hkeys.length?`
    <div style="font-size:12px;color:var(--dim);margin-bottom:8px">
      ${fmtH(totH)} total${jScope==='totals'?' tracked':jScope==='week'?' in the last 7 days':' today'}${
      jScope==='totals'&&T&&T.since?` &nbsp;·&nbsp; tracking since ${esc(T.since.split('-').reverse().join('.'))}
        (${T.day_count} day${T.day_count===1?'':'s'})`:''}</div>
    ${hkeys.map(k=>`<div class="hrow">
      <span class="hname">${esc(k)}</span>
      <div class="hbarwrap"><i style="width:${(hrs[k]/hmax*100).toFixed(1)}%;background:${HC[k]||'var(--accent)'}"></i></div>
      <span class="hval">${fmtH(hrs[k])}</span></div>`).join('')}
    <div id="hadd">
      <select id="happ">${(T?T.tracked:[]).map(a=>`<option>${esc(a)}</option>`).join('')}</select>
      <input type="text" id="hhrs" placeholder="hours, e.g. 12.5" style="width:130px"
        onkeydown="if(event.key==='Enter')addHours()">
      <input type="date" id="hdate" value="${todayISO()}">
      <button class="btn" onclick="addHours()">＋ Add manually</button>
    </div>`
    :`<div class="dim" style="font-size:12.5px;padding:4px 2px">
       Nothing tracked yet. ALL MY ART counts time automatically while Blender, ZBrush or
       Substance Painter are running — just leave it open in the background.
       <div id="hadd" style="margin-top:10px">
         <select id="happ">${(T?T.tracked:[]).map(a=>`<option>${esc(a)}</option>`).join('')}</select>
         <input type="text" id="hhrs" placeholder="hours, e.g. 12.5" style="width:130px"
           onkeydown="if(event.key==='Enter')addHours()">
         <input type="date" id="hdate" value="${todayISO()}">
         <button class="btn" onclick="addHours()">＋ Add past hours</button>
       </div></div>`;
  const hist=[...J.entries].reverse().map(e=>`
    <div class="frow">
      <span class="fl ok" style="width:110px">+${e.amount.toLocaleString('en-US')} R$</span>
      <span class="fi${e.note?'':' miss'}">${e.note?esc(e.note):'—'}</span>
      <span style="font-size:11px;color:var(--dim);white-space:nowrap">${esc(e.time)}</span>
      <button class="sbtn" style="color:var(--bad)" onclick="delEarning('${e.id}')">✕</button>
    </div>`).join('');
  $('detail').innerHTML=`
    <div class="jhero">
      <div class="jbig">${badgeSvg(i,128,false)}</div>
      <div style="flex:1;min-width:0">
        <div class="jrname" style="color:${r.c3}">${r.name}</div>
        <div class="jsub">${esc(r.sub)}</div>
        <div class="jtotal" id="jtotal">${fmtR(total)}</div>
        <div class="jsub2">lifetime robux earned</div>
        ${next?`<div class="jbar"><i style="width:${pct}%"></i></div>
          <div class="jnext">${(next.thr-total).toLocaleString('en-US')} R$ to <b style="color:${next.c3}">${next.name}</b> &nbsp;·&nbsp; ${Math.floor(pct)}%</div>`
        :`<div class="jnext ok" style="margin-top:12px;font-weight:700">🏆 Top rank reached — you cashed out real money. Legend.</div>`}
      </div>
    </div>
    <div class="sec">LOG EARNINGS</div>
    <div id="jadd">
      <input type="text" id="jamt" placeholder="Amount in R$, e.g. 250" style="width:150px"
        onkeydown="if(event.key==='Enter')addEarning()">
      <input type="date" id="jdate" value="${todayISO()}" title="When did you earn it?">
      <input type="text" id="jnote" placeholder="From what? (game pass, UGC, commission…)" style="flex:1"
        onkeydown="if(event.key==='Enter')addEarning()">
      <button class="btn primary" onclick="addEarning()">＋ Log</button>
    </div>
    ${statTiles}
    <div class="sec">EARNINGS OVER TIME
      <span style="margin-left:auto;display:flex;gap:5px">${rangeBtns}</span></div>
    ${jSvg(J.entries)}
    <div class="sec">HOURS BY PROGRAM
      <span style="margin-left:auto;display:flex;gap:5px">${scopeBtns}</span></div>
    ${hoursBlock}
    <div class="sec">RANK LADDER</div>
    <div class="jladder">${ladder}</div>
    <div class="sec">HISTORY${J.entries.length?` <span style="color:var(--accent)">· ${J.entries.length} entr${J.entries.length===1?'y':'ies'}</span>`:''}</div>
    ${hist||'<div class="dim" style="font-size:12px;padding:4px 2px">Nothing logged yet — add your first sale above. Every empire starts with one game pass.</div>'}`;
}
function todayISO(){const d=new Date();
  return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);}
async function addHours(){
  const app=$('happ').value,h=$('hhrs').value,d=$('hdate').value;
  if(!h.trim())return;
  const r=await api('timelog_add',app,h,d);
  if(!r.ok){toast(r.error);return;}
  const t=await api('timelog_get');if(t.ok)T=t;
  renderJourney();
  toast(`Added ${h} h to ${app} ✓`);}
async function addEarning(){
  const amt=$('jamt').value,note=$('jnote').value,date=$('jdate').value;
  if(!amt.trim())return;
  const r=await api('journey_add',amt,note,date);
  if(!r.ok){toast(r.error);return;}
  const oldIdx=rankIdx(r.before),newIdx=rankIdx(r.total);
  const g=await api('journey_get');
  if(g.ok)J=g;
  renderJourney();
  if(newIdx>oldIdx)rankUp(newIdx);
  else{toast(`Logged +${r.entry.amount.toLocaleString('en-US')} R$ ✓`);countUp('jtotal',r.before,r.total);}
}
async function delEarning(id){
  const r=await api('journey_delete',id);
  if(!r.ok){toast(r.error);return;}
  const g=await api('journey_get');
  if(g.ok)J=g;
  renderJourney();
}
function countUp(id,from,to){
  const el=$(id);if(!el)return;
  const t0=performance.now(),d=700;
  (function f(t){const p=Math.min(1,(t-t0)/d),v=Math.round(from+(to-from)*(1-Math.pow(1-p,3)));
    el.textContent=fmtR(v);if(p<1)requestAnimationFrame(f);})(t0);
}
function rankUp(i){
  const r=RANKS[i];
  confetti([r.c1,r.c3,'#ff5f78','#ff8a5c','#ffffff']);
  toast(`🏆 RANK UP — ${r.name} unlocked: ${r.sub}`);
  const el=document.querySelector('.jbig');
  if(el)el.classList.add('rankup');
}
function confetti(colors){
  const h=document.createElement('div');h.id='confetti';document.body.appendChild(h);
  for(let k=0;k<80;k++){
    const s=document.createElement('i');
    s.style.left=(4+Math.random()*92)+'vw';
    s.style.background=colors[k%colors.length];
    s.style.animationDelay=(Math.random()*.5)+'s';
    s.style.animationDuration=(1.6+Math.random()*1.6)+'s';
    s.style.width=(5+Math.random()*7)+'px';
    s.style.height=(8+Math.random()*9)+'px';
    s.style.transform='rotate('+(Math.random()*360)+'deg)';
    h.appendChild(s);}
  setTimeout(()=>h.remove(),3800);
}

/* ================= actions ================= */
function tsNow(){const d=new Date(),p=n=>('0'+n).slice(-2);
  return p(d.getDate())+'.'+p(d.getMonth()+1)+'.'+d.getFullYear()+' '+p(d.getHours())+':'+p(d.getMinutes());}
async function toggleStage(s){
  const a=findAsset(sel);if(!a)return;
  // optimistic: update the UI instantly, write to disk in the background
  if(a.stages[s])delete a.stages[s];else a.stages[s]=tsNow();
  recalcAsset(a);renderDetail();updateCard(a);
  await api('toggle_stage',sel.game,sel.category,sel.name,s);await refresh();}
async function addTodo(){const v=$('todoin').value;if(!v.trim())return;
  await api('add_todo',sel.game,sel.category,sel.name,v);await refresh();
  const i=$('todoin');if(i){i.focus();}}
async function toggleTodo(id){
  const a=findAsset(sel),t=a&&(a.todos||[]).find(t=>t.id===id);
  if(t){t.done=!t.done;renderDetail();}
  await api('toggle_todo',sel.game,sel.category,sel.name,id);await refresh();}
async function delTodo(id){
  const a=findAsset(sel);
  if(a){a.todos=(a.todos||[]).filter(t=>t.id!==id);renderDetail();}
  await api('delete_todo',sel.game,sel.category,sel.name,id);await refresh();}
async function saveNotes(){const n=$('notes');if(n)await api('save_notes',sel.game,sel.category,sel.name,n.value);}
let pfCache={};
async function runPreflight(){
  const box=$('pfbox');if(!box)return;
  box.innerHTML='<span class="dim">Running checks…</span>';
  const r=await api('preflight',sel.game,sel.category,sel.name);
  if(!r.ok){box.innerHTML=`<span class="bad">${esc(r.error)}</span>`;return;}
  const icon={ok:'✓',warn:'⚠',bad:'✗'};
  const rows=r.checks.map(c=>`<div class="frow">
      <span class="fl ${c.level}" style="width:120px">${icon[c.level]} ${esc(c.label)}</span>
      <span class="fi${c.level==='ok'?' ok':c.level==='warn'?' warn':''}" ${c.level==='bad'?'style="color:var(--bad)"':''}>${esc(c.info)}</span></div>`).join('');
  const head=r.level==='ok'?'<span class="ok">Ready for Roblox ✓</span>'
    :r.level==='warn'?'<span class="warn">Importable — with warnings</span>'
    :'<span class="bad">Will fail or degrade on import</span>';
  const html=`<div style="font-weight:700;font-size:12.5px;margin-bottom:6px">${head}
      <button class="sbtn" style="float:right" onclick="runPreflight()">↻ Re-run</button></div>${rows}`;
  pfCache[keyOf(sel)]=html;
  box.innerHTML=html;
}
async function setReview(state){
  const n=$('rvnote'),note=n?n.value:'';
  const r=await api('set_review',sel.game,sel.category,sel.name,state,note);
  if(!r.ok){toast(r.error);return;}
  toast(state==='approved'?'Approved ✓':state==='pending'?'Marked ready for review':
       state==='changes'?'Changes requested':'Review reset');
  await refresh();}
async function openP(p){await api('open_path',p);}
async function renameFile(path,cur){
  const v=prompt('New file name:',cur);
  if(!v||v===cur)return;
  const r=await api('file_rename',path,v);
  if(!r.ok){toast(r.error);return;}
  toast('Renamed → '+r.name);
  await refresh();
}
let _audio=null,_audioBtn=null;
function stopAudio(){
  if(_audio){_audio.pause();_audio=null;}
  if(_audioBtn){_audioBtn.textContent='▶';_audioBtn=null;}
}
async function playFile(btn,path){
  const wasThis=(_audioBtn===btn);
  stopAudio();
  if(wasThis)return;                      // clicking the playing button stops it
  const r=await api('audio_data',path);
  if(!r.ok){toast(r.error);return;}
  _audio=new Audio(r.uri);_audioBtn=btn;btn.textContent='⏸';
  _audio.onended=stopAudio;
  _audio.play().catch(()=>{toast('Playback failed');stopAudio();});
}
async function mkFolder(sub){await api('ensure_folder',sel.game,sel.category,sel.name,sub);await refresh();}
async function doSyncAll(){
  const b=$('syncall');b.disabled=true;const t=b.textContent;b.textContent='☁ Syncing…';
  const r=await api('sync_all');
  b.disabled=false;b.textContent=t;
  if(!r.ok){toast(r.error);return;}
  toast(`Synced ${r.assets} assets · ${r.copied} files copied, ${r.skipped} unchanged`+
    (r.unapproved?` · ⚠ ${r.unapproved} not approved`:'')+
    (r.errors.length?` · ${r.errors.length} errors`:' ✓'));
  await refresh();}
/* ================= settings ================= */
function openSettings(){$('soverlay').style.display='flex';renderSettings();}
function closeSettings(){$('soverlay').style.display='none';}
async function renderSettings(){
  const st=await api('integrations_status');
  const row=(label,path,btn,hint)=>`
    <div style="background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:12px;
      padding:13px 16px;margin:6px 0">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>${label}</div><div>${btn}</div>
      </div>
      ${path?`<div style="font-size:11px;color:var(--dim);margin-top:5px;word-break:break-all">${path}</div>`:''}
      ${hint?`<div style="font-size:11px;color:var(--dim);margin-top:5px">${hint}</div>`:''}
    </div>`;
  const dot=ok=>`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;
    background:${ok?'var(--ok)':'var(--warn)'};margin-right:8px"></span>`;
  const b=st.blender, p=st.painter;
  $('sbody').innerHTML=`
    <div class="sec">FOLDERS</div>
    ${row('⛁ <b>Local root</b> — where you work',
      esc(S.local_root||'not set'),
      `<button class="btn" onclick="pickLocal().then(renderSettings)">Change</button>`,
      S.local_trap?'<span style="color:var(--bad)">⚠ inside a cloud-synced folder — move it to a plain drive</span>':'')}
    ${row('☁ <b>Cloud root</b> — backup target for Sync',
      esc(S.cloud_root||'not set — sync disabled'),
      `<button class="btn" onclick="pickCloud().then(renderSettings)">Change</button>`,'')}
    ${row('🖼 <b>Library</b> — alphas, tiling, face PNGs',
      esc(S.library_root||((S.local_root||'…')+'\\_Library')+'  (default)'),
      `<button class="btn" onclick="pickLibrary()">Change</button>`,
      'Flat image collections, kept out of the asset scanner. Ctrl+L opens it.')}
    <div class="sec" style="margin-top:18px">REVIEW</div>
    ${row('👤 <b>Your name</b> — signed on review actions',
      '',
      `<input type="text" id="uname" value="${esc(S.user_name||'')}" placeholder="e.g. Tim / Donna"
        style="width:170px" onchange="saveUserName(this.value)">`,
      'Review states are stored in each asset (pipeline.json) and travel with it on sync — teammates see who approved what.')}
    <div class="sec" style="margin-top:18px">DISCORD</div>
    ${row('🔔 <b>Team pings</b> — review + done events in your channel',
      '',
      `<input type="text" id="dwh" value="${esc(S.discord_webhook||'')}"
         placeholder="https://discord.com/api/webhooks/…" style="width:220px">
       <button class="btn" onclick="saveWebhook()">Save</button>
       <button class="btn" onclick="testWebhook()">Test</button>`,
      'In Discord: channel → Edit → Integrations → Webhooks → New Webhook → Copy URL, paste here, Save, then Test.')}
    <div class="sec" style="margin-top:18px">INTEGRATIONS</div>
    ${row(dot(b.installed)+'<b>Blender add-on</b> — '+(b.installed
        ?'installed in '+b.installed_in.join(', ')
        :(b.detected?'detected ('+b.versions.join(', ')+'), not installed':'Blender not detected')),
      '',
      b.detected?`<button class="btn primary" id="ibl" onclick="installBlender()">${b.installed?'Reinstall / Update':'Install'}</button>`:'',
      b.detected?'Then in Blender: Edit → Preferences → Add-ons → enable “Pipeline Hub Export”. Panel: N-sidebar → Pipeline.'
                :'Start Blender once, then reopen Settings.')}
    ${row(dot(p.installed)+'<b>Substance Painter plugin</b> — '+(p.installed
        ?'installed':(p.detected?'detected, not installed':'Painter not detected')),
      esc(p.path),
      `<button class="btn primary" id="isp" onclick="installPainter()">${p.installed?'Reinstall / Update':'Install'}</button>`,
      'Then in Painter: Python menu → enable “pipeline_hub_painter” (restart Painter after first install).')}
    <div class="sec" style="margin-top:18px">MAINTENANCE</div>
    <div style="display:flex;gap:8px">
      <button class="btn" onclick="refresh().then(()=>toast('Rescanned ✓'))">⟳ Rescan assets</button>
    </div>
    <div style="margin-top:20px;font-size:11px;color:var(--dim)">Pipeline Hub v${st.app_version} · state: ~/.pipeline_hub</div>`;
}
async function installBlender(){
  const btn=$('ibl');btn.disabled=true;btn.textContent='Installing…';
  const r=await api('install_blender_addon');
  toast(r.ok?`Blender add-on installed for ${r.versions.join(', ')} ✓`:r.error);
  renderSettings();}
async function installPainter(){
  const btn=$('isp');btn.disabled=true;btn.textContent='Installing…';
  const r=await api('install_painter_plugin');
  toast(r.ok?'Painter plugin installed ✓ — restart Painter':r.error);
  renderSettings();}
$('soverlay').addEventListener('click',e=>{if(e.target.id==='soverlay')closeSettings();});

async function saveWebhook(){
  const r=await api('set_discord_webhook',$('dwh').value);
  if(r.ok){S.discord_webhook=$('dwh').value.trim();toast('Webhook saved ✓');}
  else toast(r.error);}
async function testWebhook(){
  await api('set_discord_webhook',$('dwh').value);
  const r=await api('test_discord');
  toast(r.ok?'Test ping sent — check your Discord channel':r.error);}
async function saveUserName(v){
  const r=await api('set_user_name',v);
  if(r.ok){S.user_name=r.user_name;toast('Name saved ✓');}else toast(r.error);}
async function pickLibrary(){
  const r=await api('set_library_root');
  if(!r.ok){toast(r.error);return;}
  L=null;libRel='';await refresh();renderSettings();}
async function pickLocal(){await api('pick_local_root');await refresh(false);}
async function pickCloud(){await api('pick_cloud_root');await refresh();}
async function doSync(force=false){
  const b=$('syncbtn'),l=$('syncline');b.disabled=true;b.textContent='☁ Syncing…';
  l.textContent='Syncing…';l.style.color='var(--accent)';
  const r=await api('sync_asset',sel.game,sel.category,sel.name,force);
  if(!r.ok&&r.needs_review){
    const st=(r.review&&r.review.state)||'none';
    const why=st==='changes'?'changes requested':st==='pending'?'still awaiting review':'not reviewed yet';
    l.textContent=`Not approved (${why}) — “⚠ Sync Anyway” skips the review gate`;
    l.style.color='var(--warn)';
    b.disabled=false;b.textContent='⚠ Sync Anyway';b.onclick=()=>doSync(true);
    return;}
  if(!r.ok){toast(r.error);l.textContent='Sync failed';l.style.color='var(--bad)';}
  else{l.textContent=`Last sync: ${r.last_sync} · ${r.copied} copied, ${r.skipped} unchanged`+
    (r.errors.length?` · ${r.errors.length} errors`:'');
    l.style.color=r.errors.length?'var(--bad)':'var(--ok)';
    toast(r.errors.length?'Sync finished with errors':'Synced to cloud ✓');}
  b.disabled=false;b.textContent='☁ Sync → Cloud';b.onclick=()=>doSync();}

/* ================= modal ================= */
let mcat='',mtype='3D';
function openModal(){
  $('gamelist').innerHTML=S.game_names.map(g=>`<option value="${esc(g)}">`).join('');
  mtype=typeFilter()||mtype||'3D';
  renderTypePick();
  $('merr').textContent='';$('mname').value='';
  $('overlay').classList.add('show');
  setTimeout(()=>$('mname').focus(),120);}
function renderTypePick(){
  $('mtypes').innerHTML=Object.entries(S.types||{}).map(([t,v])=>
    `<div class="chip${t===mtype?' on':''}" onclick="pickType('${t}')">${v.icon} ${esc(v.label)}</div>`).join('');
  const cats=(S.categories_by_type&&S.categories_by_type[mtype])||S.categories;
  $('mcats').innerHTML=cats.map(c=>
    `<div class="chip${c===mcat?' on':''}" onclick="pickCat('${esc(c)}')">${esc(c)}</div>`).join('');
  if(cats.length&&!cats.includes(mcat))pickCat(cats[0]);
  $('mname').placeholder=mtype==='SFX'?'e.g. Sword_Swing_Heavy':'e.g. PlantMonster';
  $('mbatch').placeholder=mtype==='SFX'
    ?'One sound per line, e.g.\nUI_Click_Soft\nFootstep_Grass_01\nSword_Impact_Metal'
    :'One asset name per line, e.g.\nShroom_A1_T1_Puffcap\nShroom_A1_T2_Glowveil';}
function pickType(t){mtype=t;renderTypePick();}
function pickCat(c){mcat=c;$('mcat').value='';
  document.querySelectorAll('#mcats .chip').forEach(el=>
    el.classList.toggle('on',el.textContent===c));}
function closeModal(){$('overlay').classList.remove('show');}
$('overlay').addEventListener('click',e=>{if(e.target.id==='overlay')closeModal();});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openK();return;}
  if(e.key==='Escape'){
    if($('kbar').style.display==='flex'){closeK();return;}
    if($('soverlay').style.display==='flex'){closeSettings();return;}
    if($('overlay').classList.contains('show')){closeModal();return;}
    goHome();return;}
  if(e.key==='Enter'&&$('overlay').classList.contains('show')&&document.activeElement.id==='mname'){createAsset();return;}
  const typing=document.activeElement&&(document.activeElement.tagName==='INPUT'||document.activeElement.tagName==='TEXTAREA');
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='n'){e.preventDefault();openModal();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='b'){e.preventDefault();goBoard();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='j'){e.preventDefault();goJourney();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='l'){e.preventDefault();goLibrary();return;}
  if(e.key==='/'&&!typing){e.preventDefault();$('search').focus();return;}
});

/* ================= command palette ================= */
let kIdx=0,kItems=[];
function allAssets(){const out=[];if(!S)return out;
  for(const g of S.games)for(const c of g.categories)for(const a of c.assets)out.push(a);return out;}
function openK(){$('kbar').style.display='flex';$('kin').value='';kFilter('');
  setTimeout(()=>$('kin').focus(),60);}
function closeK(){$('kbar').style.display='none';}
function kFilter(q){
  q=q.toLowerCase().trim();
  kItems=allAssets().filter(a=>!q||a.name.toLowerCase().includes(q)
    ||a.game.toLowerCase().includes(q)||a.category.toLowerCase().includes(q)).slice(0,40);
  kIdx=0;kRender();}
function kRender(){
  $('kres').innerHTML=kItems.length?kItems.map((a,i)=>`
    <div class="kitem${i===kIdx?' on':''}" onclick="kGo(${i})">
      ${a.thumb?`<img src="${a.thumb}">`:'<span style="width:30px;text-align:center;color:var(--dim)">◆</span>'}
      <span>${esc(a.name)}</span>
      <span class="kpath">${esc(a.game)} / ${esc(a.category)}</span>
    </div>`).join('')
    :'<div class="kitem" style="color:var(--dim);cursor:default">No matches</div>';
  const on=$('kres').querySelector('.kitem.on');
  if(on)on.scrollIntoView({block:'nearest'});}
function kGo(i){const a=kItems[i];if(!a)return;closeK();
  $('gamef').value='All games';
  selectAsset({game:a.game,category:a.category,name:a.name});}
$('kin').addEventListener('input',e=>kFilter(e.target.value));
$('kin').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();kIdx=Math.min(kIdx+1,kItems.length-1);kRender();}
  else if(e.key==='ArrowUp'){e.preventDefault();kIdx=Math.max(kIdx-1,0);kRender();}
  else if(e.key==='Enter'){e.preventDefault();kGo(kIdx);}});
$('kbar').addEventListener('click',e=>{if(e.target.id==='kbar')closeK();});
$('mcat')&&$('mcat').addEventListener('input',()=>{mcat='';
  document.querySelectorAll('#mcats .chip').forEach(el=>el.classList.remove('on'));});
function toggleBatch(){
  const t=$('mbatch'),on=t.style.display==='none';
  t.style.display=on?'block':'none';
  $('mname').style.opacity=on?.4:1;$('mname').disabled=on;
  $('batchtoggle').textContent=on?'⇠ Single mode':'⇢ Batch mode (create many at once)';
  if(on)t.focus();}
async function createAsset(){
  const game=$('mgame').value||'General';
  const cat=$('mcat').value.trim()||mcat||'Props';
  const batchOn=$('mbatch').style.display!=='none';
  if(batchOn){
    const txt=$('mbatch').value;
    if(!txt.trim()){$('merr').textContent='Batch list is empty.';return;}
    const r=await api('create_assets_batch',game,cat,txt,mtype);
    if(!r.ok){$('merr').textContent=r.error;return;}
    closeModal();
    await refresh();
    toast(`Created ${r.created.length} ${mtype==='SFX'?'sounds':'assets'} in ${r.game}/${r.category}`+
      (r.failed.length?` · ${r.failed.length} failed (already exist?)`:' ✓'));
    return;}
  const name=$('mname').value;
  const r=await api('create_asset',game,cat,name,mtype);
  if(!r.ok){$('merr').textContent=r.error;return;}
  closeModal();
  if(r.warn)toast(r.warn);
  sel={game:r.game,category:r.category,name:r.name};
  await refresh();
  toast(`Created ${r.game} / ${r.category} / ${r.name} ✓`);}

function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),3200);}

/* ================= boot ================= */
window.addEventListener('pywebviewready',()=>refresh(false)
  .then(()=>setTimeout(()=>document.body.classList.add('steady'),700)));

/* ================= live refresh ================= */
let lastHash='';
setInterval(async()=>{
  if(document.hidden||!S)return;
  try{
    const s=await api('get_state');
    const h=JSON.stringify(s);
    if(h===lastHash)return;
    lastHash=h;S=s;
    render();
    const ae=document.activeElement;
    const typing=ae&&(ae.tagName==='TEXTAREA'||ae.tagName==='INPUT');
    if(!typing)renderMain();
  }catch(e){}
},7000);
</script>
</body></html>
"""


def main():
    api = Api()
    webview.create_window(APP_NAME, html=HTML, js_api=api,
                          width=1320, height=820, min_size=(1040, 640),
                          maximized=True,   # fills the screen, keeps the title bar
                          background_color="#0e0609")  # so minimize still works
    webview.start()


if __name__ == "__main__":
    main()
