# kicad-ai.py
# KiCAD Plugin — General-purpose AI assistant for PCB design

import base64
import copy
import datetime
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from typing import Any, Optional, Union
import urllib.error
import urllib.request

import wx  # type: ignore
import pcbnew  # type: ignore

_ACTIVE_DIALOG = None


# ---------------------------------------------------------------------------
# In-process debug log (surfaced in the dialog's Log tab)
# ---------------------------------------------------------------------------

_LOG_ENTRIES: list[str] = []
_LOG_MAX = 4000
_LOG_LISTENERS: list = []


def kicad_ai_log(msg: str) -> None:
    """Append a timestamped entry to the in-memory log and notify UI listeners."""
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        _LOG_ENTRIES.append(entry)
        if len(_LOG_ENTRIES) > _LOG_MAX:
            del _LOG_ENTRIES[: len(_LOG_ENTRIES) - _LOG_MAX]
        for fn in list(_LOG_LISTENERS):
            try:
                fn(entry)
            except Exception:
                pass
    except Exception:
        pass


def get_log_text() -> str:
    return "\n".join(_LOG_ENTRIES)


def clear_log() -> None:
    _LOG_ENTRIES.clear()


def add_log_listener(fn) -> None:
    if fn not in _LOG_LISTENERS:
        _LOG_LISTENERS.append(fn)


def remove_log_listener(fn) -> None:
    try:
        _LOG_LISTENERS.remove(fn)
    except ValueError:
        pass


def mm(iu: int) -> float:
    return float(pcbnew.ToMM(iu))


BOARD_OUTLINE_ANCHOR_DISPLAY = "EDGE_CUT"


def board_outline_bbox(board):
    """Bounding box of Edge.Cuts (board outline), or None if unavailable."""
    try:
        if hasattr(board, "UpdateBoardOutline"):
            board.UpdateBoardOutline()
    except Exception:
        pass

    bb = None
    try:
        if hasattr(board, "GetBoardEdgesBoundingBox"):
            bb = board.GetBoardEdgesBoundingBox()
    except Exception:
        bb = None

    def _nonempty(b):
        if b is None:
            return False
        try:
            return int(b.GetWidth()) > 0 or int(b.GetHeight()) > 0
        except Exception:
            return False

    if _nonempty(bb):
        return bb

    merged = None
    try:
        edge = pcbnew.Edge_Cuts
        for item in board.GetDrawings():
            try:
                if item.GetLayer() != edge:
                    continue
                ib = item.GetBoundingBox()
                if merged is None:
                    merged = ib
                elif hasattr(merged, "Merge"):
                    merged.Merge(ib)
            except Exception:
                continue
    except Exception:
        merged = None

    if _nonempty(merged):
        return merged

    try:
        if hasattr(board, "ComputeBoundingBox"):
            bb2 = board.ComputeBoundingBox(True)
            if _nonempty(bb2):
                return bb2
    except Exception:
        pass

    return None


def footprint_name(fp) -> str:
    ref = fp.GetReference() if hasattr(fp, "GetReference") else ""
    val = fp.GetValue() if hasattr(fp, "GetValue") else ""
    if ref and val:
        return f"{ref} ({val})"
    return ref or val or "Footprint"


def footprint_row(fp):
    bbox = fp.GetBoundingBox()

    left = bbox.GetLeft()
    right = bbox.GetRight()
    top = bbox.GetTop()
    bottom = bbox.GetBottom()
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    width = bbox.GetWidth()
    height = bbox.GetHeight()

    return {
        "footprint": fp,
        "ref": fp.GetReference() if hasattr(fp, "GetReference") else "",
        "display_name": footprint_name(fp),
        "top": top,
        "left": left,
        "center": (center_x, center_y),
        "values": [
            footprint_name(fp),
            "0",
            f"{mm(width):.3f}",
            f"{mm(height):.3f}",
            f"{mm(left):.3f}",
            f"{mm(right):.3f}",
            f"{mm(top):.3f}",
            f"{mm(bottom):.3f}",
            f"{mm(center_x):.3f}",
            f"{mm(center_y):.3f}",
        ],
    }


def edge_cut_row(board):
    """One synthetic table row for the Edge.Cuts bounding box."""
    bbox = board_outline_bbox(board)
    if bbox is None:
        return None

    left = bbox.GetLeft()
    right = bbox.GetRight()
    top = bbox.GetTop()
    bottom = bbox.GetBottom()
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    width = bbox.GetWidth()
    height = bbox.GetHeight()

    width_mm = mm(width)
    height_mm = mm(height)

    return {
        "footprint": None,
        "ref": BOARD_OUTLINE_ANCHOR_DISPLAY,
        "display_name": BOARD_OUTLINE_ANCHOR_DISPLAY,
        "is_edge_cut": True,
        "top": top,
        "left": left,
        "center": (center_x, center_y),
        "values": [
            BOARD_OUTLINE_ANCHOR_DISPLAY,
            "0",
            f"{width_mm:.3f}",
            f"{height_mm:.3f}",
            f"{mm(left):.3f}",
            f"{mm(right):.3f}",
            f"{mm(top):.3f}",
            f"{mm(bottom):.3f}",
            f"{mm(center_x):.3f}",
            f"{mm(center_y):.3f}",
        ],
    }


def sort_rows_top_to_bottom_then_left_to_right(rows, x_bucket_mm=0.05, y_bucket_mm=0.05):
    x_bucket_iu = pcbnew.FromMM(x_bucket_mm)
    y_bucket_iu = pcbnew.FromMM(y_bucket_mm)

    def key(r):
        center_x, center_y = r["center"]
        y_bucket = int(round(center_y / y_bucket_iu)) if y_bucket_iu else center_y
        x_bucket = int(round(center_x / x_bucket_iu)) if x_bucket_iu else center_x
        return (y_bucket, x_bucket, r["ref"])

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# OpenAI settings
# ---------------------------------------------------------------------------

def get_openai_settings() -> tuple[str, str]:
    api_key = PLACEHOLDER_FOR_OPENAI_API_KEY  # type: ignore
    model = PLACEHOLDER_FOR_OPENAI_MODEL  # type: ignore
    return api_key, model


# ---------------------------------------------------------------------------
# Vision helpers
# ---------------------------------------------------------------------------

def model_supports_openai_vision(model: str) -> bool:
    """Best-effort heuristic.

    Default to True for modern OpenAI models. Only return False when the model name
    matches a family that is known NOT to accept image_url parts.
    """
    m = (model or "").lower().strip()
    if not m:
        return False
    # Known non-vision families
    non_vision_substrings = (
        "gpt-3.5",
        "text-davinci", "text-curie", "text-babbage", "text-ada",
        "davinci-", "curie-", "babbage-", "ada-",
        "text-embedding", "embedding-",
        "whisper", "tts-", "dall-e", "dalle",
        "moderation",
        "text-moderation",
    )
    for s in non_vision_substrings:
        if s in m:
            return False
    # Everything else (gpt-4*, gpt-4o*, gpt-4.1*, gpt-5*, chatgpt-*, o1*, o3*, o4*,
    # etc.) is assumed to accept image_url content parts.
    return True


def _downscale_png_bytes_if_large(png_bytes: bytes, max_side: int = 1536) -> bytes:
    try:
        stream_in = io.BytesIO(png_bytes)
        img = wx.Image(stream_in)
        if not img.IsOk():
            return png_bytes
        w, h = img.GetWidth(), img.GetHeight()
        if w <= 0 or h <= 0:
            return png_bytes
        side = max(w, h)
        if side <= max_side:
            return png_bytes
        if w >= h:
            nw = max_side
            nh = max(1, int(round(h * (max_side / float(w)))))
        else:
            nh = max_side
            nw = max(1, int(round(w * (max_side / float(h)))))
        img = img.Scale(nw, nh, wx.IMAGE_QUALITY_HIGH)
        out = io.BytesIO()
        if not img.SaveFile(out, wx.BITMAP_TYPE_PNG):
            return png_bytes
        return out.getvalue()
    except Exception:
        return png_bytes


def try_capture_kicad_window_png_bytes(target_window=None) -> Optional[bytes]:
    """Grab a KiCad window's client area as PNG.

    If target_window is provided, capture that window. Otherwise fall back to the
    app's top window (historical behaviour).
    """
    try:
        top = target_window
        if top is None:
            app = wx.GetApp()
            if app is None:
                return None
            top = app.GetTopWindow()
        if top is None:
            return None
        try:
            if not top.IsShown():
                return None
        except Exception:
            pass
        top.Update()
        rect = top.GetClientRect()
        w, h = rect.width, rect.height
        if w < 48 or h < 48 or w > 12000 or h > 12000:
            return None
        bmp = wx.Bitmap(w, h)
        mdc = wx.MemoryDC()
        mdc.SelectObject(bmp)
        wdc = wx.WindowDC(top)
        mdc.Blit(0, 0, w, h, wdc, 0, 0)
        del wdc
        mdc.SelectObject(wx.NullBitmap)
        img = bmp.ConvertToImage()
        stream = io.BytesIO()
        if not img.SaveFile(stream, wx.BITMAP_TYPE_PNG):
            return None
        data = stream.getvalue()
        if len(data) < 80:
            return None
        return _downscale_png_bytes_if_large(data)
    except Exception:
        return None


def try_export_board_svg_snippet(board, max_chars: int = 18000) -> str:
    """Export F.Cu to SVG via plotter; empty string on failure."""
    if not hasattr(pcbnew, "PLOT_FORMAT_SVG"):
        return ""
    tmp = tempfile.mkdtemp(prefix="kicad_ai_svg_")
    try:
        pc = pcbnew.PLOT_CONTROLLER(board)
        po = pc.GetPlotOptions()
        if hasattr(po, "SetPlotFrameRef"):
            po.SetPlotFrameRef(False)
        if hasattr(po, "SetOutputDirectory"):
            po.SetOutputDirectory(tmp)
        elif hasattr(po, "SetPlotDirectory"):
            po.SetPlotDirectory(tmp)
        pc.SetLayer(pcbnew.F_Cu)
        pc.OpenPlotfile("ai_export", pcbnew.PLOT_FORMAT_SVG, "export")
        pc.PlotLayer()
        pc.ClosePlot()
        for name in os.listdir(tmp):
            if name.lower().endswith(".svg"):
                path = os.path.join(tmp, name)
                with open(path, encoding="utf-8", errors="replace") as f:
                    data = f.read()
                if len(data) > max_chars:
                    return data[:max_chars] + "\n<!-- truncated -->"
                return data
    except Exception:
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ""


def _layer_name_safe(board, layer_id) -> str:
    try:
        if hasattr(board, "GetLayerName"):
            return str(board.GetLayerName(layer_id))
    except Exception:
        pass
    try:
        if hasattr(pcbnew, "GetStandardLayerName"):
            return str(pcbnew.GetStandardLayerName(layer_id))
    except Exception:
        pass
    return str(layer_id)


def vector2i_xy(pos) -> tuple[int, int]:
    if hasattr(pos, "x") and hasattr(pos, "y"):
        return int(pos.x), int(pos.y)
    if hasattr(pos, "Get"):
        try:
            value = pos.Get()
            if isinstance(value, (tuple, list)) and len(value) >= 2:
                return int(value[0]), int(value[1])
        except Exception:
            pass
    if hasattr(pos, "__getitem__"):
        try:
            return int(pos[0]), int(pos[1])
        except Exception:
            pass
    raise AttributeError("Could not read VECTOR2I coordinates.")


# ---------------------------------------------------------------------------
# Board context for the AI
# ---------------------------------------------------------------------------

def gather_board_context_for_ai(board, component_rows: list) -> dict:
    """Structured board data sent to the model."""
    path = ""
    try:
        path = board.GetFileName() if hasattr(board, "GetFileName") else ""
    except Exception:
        pass

    outline_mm = None
    bb = board_outline_bbox(board)
    if bb:
        outline_mm = {
            "left_mm": round(mm(bb.GetLeft()), 4),
            "right_mm": round(mm(bb.GetRight()), 4),
            "top_mm": round(mm(bb.GetTop()), 4),
            "bottom_mm": round(mm(bb.GetBottom()), 4),
            "width_mm": round(mm(bb.GetWidth()), 4),
            "height_mm": round(mm(bb.GetHeight()), 4),
        }

    edge_cut_summary = None
    components = []
    for row in component_rows:
        if row.get("is_edge_cut"):
            vals = row.get("values") or []
            edge_cut_summary = {
                "label": BOARD_OUTLINE_ANCHOR_DISPLAY,
                "width_mm": vals[2] if len(vals) > 2 else "",
                "height_mm": vals[3] if len(vals) > 3 else "",
                "bbox_left_mm": vals[4] if len(vals) > 4 else "",
                "bbox_right_mm": vals[5] if len(vals) > 5 else "",
                "bbox_top_mm": vals[6] if len(vals) > 6 else "",
                "bbox_bottom_mm": vals[7] if len(vals) > 7 else "",
                "x_center_mm": vals[8] if len(vals) > 8 else "",
                "y_center_mm": vals[9] if len(vals) > 9 else "",
            }
            continue

        fp = row.get("footprint")
        vals = row.get("values") or []
        comp = {
            "display_name": row.get("display_name", vals[0] if vals else ""),
            "ref": row.get("ref", ""),
            "width_mm": vals[2] if len(vals) > 2 else "",
            "height_mm": vals[3] if len(vals) > 3 else "",
            "bbox_left_mm": vals[4] if len(vals) > 4 else "",
            "bbox_right_mm": vals[5] if len(vals) > 5 else "",
            "bbox_top_mm": vals[6] if len(vals) > 6 else "",
            "bbox_bottom_mm": vals[7] if len(vals) > 7 else "",
            "x_center_mm": vals[8] if len(vals) > 8 else "",
            "y_center_mm": vals[9] if len(vals) > 9 else "",
        }
        if fp is not None:
            try:
                pos = fp.GetPosition()
                x, y = vector2i_xy(pos)
                comp["position_mm"] = {"x": round(mm(x), 4), "y": round(mm(y), 4)}
            except Exception:
                pass
            try:
                if hasattr(fp, "GetOrientationDegrees"):
                    comp["rotation_deg"] = float(fp.GetOrientationDegrees())
                else:
                    o = fp.GetOrientation()
                    if hasattr(o, "AsDegrees"):
                        comp["rotation_deg"] = float(o.AsDegrees())
                    elif hasattr(o, "AsTenthsOfADegree"):
                        comp["rotation_deg"] = float(o.AsTenthsOfADegree()) / 10.0
                    else:
                        comp["rotation_deg"] = float(o) / 10.0
            except Exception:
                pass
            try:
                comp["value"] = fp.GetValue() if hasattr(fp, "GetValue") else ""
            except Exception:
                pass
            try:
                if hasattr(fp, "GetFPIDAsString"):
                    comp["footprint_id"] = fp.GetFPIDAsString()
            except Exception:
                pass
            try:
                ly = fp.GetLayer()
                comp["layer"] = _layer_name_safe(board, ly)
            except Exception:
                pass
        components.append(comp)

    return {
        "board_file": path,
        "units": "mm",
        "axes": "+X right, +Y down (KiCad PCB space, mm below).",
        "board_outline_bbox_mm": outline_mm,
        "edge_cut_summary": edge_cut_summary,
        "footprint_count": len(components),
        "components": components,
    }


# ---------------------------------------------------------------------------
# AI chat — general-purpose
# ---------------------------------------------------------------------------

RULE_SECTION_LABELS = [
    ("general_rules", "General Rules"),
    ("schematics", "Schematics"),
    ("footprints", "Footprints"),
    ("pcb_editor_routing", "PCB Editor and Routing"),
]


def _empty_user_rules() -> dict[str, str]:
    return {key: "" for key, _label in RULE_SECTION_LABELS}


_BASE_GUARDRAILS = (
    "You are an expert KiCad PCB/schematic design assistant embedded in a KiCad plugin.\n"
    "You have access to structured context about the current KiCad project and optional user attachments "
    "(images, text files, and rasterized PDF pages from datasheets).\n\n"
    "RULES:\n"
    "- Be concise, specific, and technically precise.\n"
    "- When you need to change the KiCad project, call one of the provided tools instead of just describing it.\n"
    "- Do not call tools speculatively. Only call tools when the user's request requires a concrete change.\n"
    "- Reference actual component references, pin numbers, net names, and mm coordinates from the context.\n"
    "- If a request is out of scope for the active mode, explain and suggest switching mode.\n"
)


def _user_rules_block(user_rules: dict[str, str]) -> str:
    sections = []
    for key, label in RULE_SECTION_LABELS:
        value = (user_rules.get(key, "") or "").strip()
        if value:
            sections.append(f"{label}:\n{value}")
    if sections:
        return "\nUSER RULES:\n" + "\n\n".join(sections) + "\n"
    return ""


# ---------------------------------------------------------------------------
# Modes + tools
# ---------------------------------------------------------------------------

def _sanitize_lib_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-\.]+", "_", (name or "").strip())
    s = s.strip("._-")
    return s or "unnamed"


def _coerce_tool_string(value) -> str:
    """Normalize JSON tool fields that may be str or list (e.g. tags as an array)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(x) for x in value)
    return str(value)


def _project_dir(board) -> str:
    try:
        path = board.GetFileName() if hasattr(board, "GetFileName") else ""
    except Exception:
        path = ""
    if path and os.path.isdir(os.path.dirname(path)):
        return os.path.dirname(path)
    return _generated_dir()


def _layer_id_from_name(board, layer_name: str) -> int:
    name = (layer_name or "").strip()
    if not name:
        return pcbnew.F_Cu
    try:
        if hasattr(board, "GetLayerID"):
            lid = board.GetLayerID(name)
            if isinstance(lid, int) and lid >= 0:
                return lid
    except Exception:
        pass
    mapping = {
        "F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu,
        "F.SilkS": pcbnew.F_SilkS, "B.SilkS": pcbnew.B_SilkS,
        "F.Mask": pcbnew.F_Mask, "B.Mask": pcbnew.B_Mask,
        "F.Paste": pcbnew.F_Paste, "B.Paste": pcbnew.B_Paste,
        "F.Fab": pcbnew.F_Fab, "B.Fab": pcbnew.B_Fab,
        "F.CrtYd": pcbnew.F_CrtYd, "B.CrtYd": pcbnew.B_CrtYd,
        "Edge.Cuts": pcbnew.Edge_Cuts,
    }
    return mapping.get(name, pcbnew.F_Cu)


def _vector2i(x_iu: int, y_iu: int):
    if hasattr(pcbnew, "VECTOR2I"):
        return pcbnew.VECTOR2I(int(x_iu), int(y_iu))
    return pcbnew.wxPoint(int(x_iu), int(y_iu))


def gather_nets_summary(board, max_nets: int = 80, max_pads_per_net: int = 24) -> dict:
    """Lightweight nets summary: net names + per-pad anchor coordinates."""
    nets: dict[str, list[dict]] = {}
    try:
        for fp in board.Footprints():
            ref = fp.GetReference() if hasattr(fp, "GetReference") else ""
            layer = _layer_name_safe(board, fp.GetLayer()) if hasattr(fp, "GetLayer") else ""
            for pad in fp.Pads():
                try:
                    net_name = pad.GetNetname() if hasattr(pad, "GetNetname") else ""
                except Exception:
                    net_name = ""
                if not net_name:
                    continue
                pad_num = ""
                try:
                    pad_num = pad.GetNumber() if hasattr(pad, "GetNumber") else pad.GetName()
                except Exception:
                    pad_num = ""
                try:
                    x, y = vector2i_xy(pad.GetPosition())
                except Exception:
                    continue
                nets.setdefault(net_name, []).append({
                    "ref": ref,
                    "pad": pad_num,
                    "layer": layer,
                    "x_mm": round(mm(x), 4),
                    "y_mm": round(mm(y), 4),
                })
    except Exception:
        pass

    trimmed: dict[str, Any] = {}
    for i, (name, pads) in enumerate(sorted(nets.items(), key=lambda kv: (-len(kv[1]), kv[0]))):
        if i >= max_nets:
            break
        trimmed[name] = pads[:max_pads_per_net]

    existing_tracks = []
    try:
        for i, track in enumerate(board.GetTracks()):
            if i >= 200:
                break
            try:
                is_via = isinstance(track, pcbnew.PCB_VIA)
            except Exception:
                is_via = False
            try:
                net = track.GetNetname() if hasattr(track, "GetNetname") else ""
            except Exception:
                net = ""
            entry: dict[str, Any] = {
                "index": i,
                "is_via": bool(is_via),
                "net": net,
                "layer": _layer_name_safe(board, track.GetLayer()) if hasattr(track, "GetLayer") else "",
            }
            try:
                sx, sy = vector2i_xy(track.GetStart())
                ex, ey = vector2i_xy(track.GetEnd())
                entry["start_mm"] = [round(mm(sx), 4), round(mm(sy), 4)]
                entry["end_mm"] = [round(mm(ex), 4), round(mm(ey), 4)]
            except Exception:
                pass
            try:
                entry["width_mm"] = round(mm(track.GetWidth()), 4)
            except Exception:
                pass
            existing_tracks.append(entry)
    except Exception:
        pass

    layers = []
    try:
        for lid in (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu):
            try:
                layers.append(_layer_name_safe(board, lid))
            except Exception:
                continue
    except Exception:
        pass

    return {
        "nets": trimmed,
        "copper_layers": layers,
        "existing_tracks": existing_tracks,
    }


# ---------- s-expression generators ----------

def _kicad_sym_file(symbol_name: str, description: str, pins: list, properties: dict) -> str:
    """Generate a single-symbol .kicad_sym library file."""
    sn = _sanitize_lib_name(symbol_name)
    props = {
        "Reference": properties.get("Reference", "U"),
        "Value": properties.get("Value", sn),
        "Footprint": properties.get("Footprint", ""),
        "Datasheet": properties.get("Datasheet", "~"),
        "Description": properties.get("Description", description or ""),
    }
    # split pins by side if provided; otherwise half/half left/right
    sided: dict[str, list[dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        side = (p.get("side") or "").lower()
        if side not in sided:
            side = ""
        if side:
            sided[side].append(p)
    unassigned = [p for p in pins if (p.get("side") or "").lower() not in ("left", "right", "top", "bottom")]
    if unassigned:
        half = (len(unassigned) + 1) // 2
        sided["left"].extend(unassigned[:half])
        sided["right"].extend(unassigned[half:])

    # box sizing
    pin_spacing = 2.54
    v_pins = max(len(sided["left"]), len(sided["right"]), 1)
    h_pins = max(len(sided["top"]), len(sided["bottom"]), 1)
    box_h = max(v_pins + 1, 4) * pin_spacing
    box_w = max(h_pins + 1, 4) * pin_spacing
    left_x = -box_w / 2.0
    right_x = box_w / 2.0
    top_y = box_h / 2.0
    bot_y = -box_h / 2.0

    def _pin_block(p: dict, at_x: float, at_y: float, rot: int) -> str:
        etype = (p.get("electrical_type") or p.get("type") or "passive").lower()
        allowed = {"input", "output", "bidirectional", "tri_state", "passive", "free",
                   "unspecified", "power_in", "power_out", "open_collector", "open_emitter", "no_connect"}
        if etype not in allowed:
            etype = "passive"
        number = str(p.get("number", "")) or "?"
        name = str(p.get("name", "")) or "~"
        length = p.get("length_mm", 2.54)
        return (
            f"    (pin {etype} line (at {at_x:.3f} {at_y:.3f} {rot}) (length {length})\n"
            f"      (name \"{name}\" (effects (font (size 1.27 1.27))))\n"
            f"      (number \"{number}\" (effects (font (size 1.27 1.27))))\n"
            f"    )"
        )

    pin_lines = []
    for i, p in enumerate(sided["left"]):
        y = top_y - pin_spacing * (i + 1)
        pin_lines.append(_pin_block(p, left_x - 2.54, y, 0))
    for i, p in enumerate(sided["right"]):
        y = top_y - pin_spacing * (i + 1)
        pin_lines.append(_pin_block(p, right_x + 2.54, y, 180))
    for i, p in enumerate(sided["top"]):
        x = left_x + pin_spacing * (i + 1)
        pin_lines.append(_pin_block(p, x, top_y + 2.54, 270))
    for i, p in enumerate(sided["bottom"]):
        x = left_x + pin_spacing * (i + 1)
        pin_lines.append(_pin_block(p, x, bot_y - 2.54, 90))

    rect = (
        f"    (rectangle (start {left_x:.3f} {top_y:.3f}) (end {right_x:.3f} {bot_y:.3f})\n"
        f"      (stroke (width 0.254) (type default)) (fill (type background))\n"
        f"    )"
    )

    def _prop(key: str, val: str, at_y: float, hide: bool) -> str:
        hide_s = " hide" if hide else ""
        return (
            f"  (property \"{key}\" \"{val}\" (at 0 {at_y:.3f} 0)\n"
            f"    (effects (font (size 1.27 1.27)){hide_s})\n"
            f"  )"
        )

    prop_lines = [
        _prop("Reference", props["Reference"], top_y + 2.54, False),
        _prop("Value", props["Value"], bot_y - 2.54, False),
        _prop("Footprint", props["Footprint"], 0.0, True),
        _prop("Datasheet", props["Datasheet"], 0.0, True),
        _prop("Description", props["Description"].replace('"', "'"), 0.0, True),
    ]

    out = [
        "(kicad_symbol_lib (version 20231120) (generator kicad_ai)",
        f"  (symbol \"{sn}\"",
        "    (pin_names (offset 1.016))",
        "    (in_bom yes) (on_board yes)",
    ]
    out.extend(prop_lines)
    out.append(f"    (symbol \"{sn}_0_1\"")
    out.append(rect)
    out.extend(pin_lines)
    out.append("    )")
    out.append("  )")
    out.append(")")
    return "\n".join(out) + "\n"


def _kicad_mod_file(fp_name: str, description: str, tags: str, pads: list,
                    courtyard_margin_mm: float = 0.25,
                    silk_outline: Optional[list] = None) -> str:
    """Generate a .kicad_mod footprint file."""
    name = _sanitize_lib_name(fp_name)
    desc = _coerce_tool_string(description).replace('"', "'")
    tagstr = _coerce_tool_string(tags).replace('"', "'")
    silk_outline = silk_outline or []

    pad_lines = []
    xs: list[float] = []
    ys: list[float] = []
    for i, pad in enumerate(pads, start=1):
        num = str(pad.get("number", i))
        shape = (pad.get("shape") or "rect").lower()
        if shape not in {"rect", "roundrect", "circle", "oval", "trapezoid", "custom"}:
            shape = "rect"
        pad_type = (pad.get("pad_type") or pad.get("type") or "smd").lower()
        if pad_type not in {"smd", "thru_hole", "np_thru_hole", "connect"}:
            pad_type = "smd"
        size = pad.get("size_mm") or [1.0, 1.0]
        if not isinstance(size, (list, tuple)) or len(size) < 2:
            size = [1.0, 1.0]
        pos = pad.get("position_mm") or [0.0, 0.0]
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            pos = [0.0, 0.0]
        rot = float(pad.get("rotation_deg", 0))
        drill = pad.get("drill_mm", 0)
        layers = pad.get("layers") or (
            ["F.Cu", "F.Paste", "F.Mask"] if pad_type == "smd"
            else ["*.Cu", "*.Mask"]
        )
        layers_str = " ".join(f"\"{l}\"" for l in layers)
        at = f"(at {float(pos[0]):.4f} {float(pos[1]):.4f}" + (f" {rot}" if rot else "") + ")"
        size_str = f"(size {float(size[0]):.4f} {float(size[1]):.4f})"
        drill_str = ""
        if pad_type in ("thru_hole", "np_thru_hole") and drill:
            drill_str = f" (drill {float(drill):.4f})"
        pad_lines.append(
            f"  (pad \"{num}\" {pad_type} {shape} {at} {size_str}{drill_str} (layers {layers_str}))"
        )
        xs.append(float(pos[0]) - float(size[0]) / 2.0)
        xs.append(float(pos[0]) + float(size[0]) / 2.0)
        ys.append(float(pos[1]) - float(size[1]) / 2.0)
        ys.append(float(pos[1]) + float(size[1]) / 2.0)

    if xs and ys:
        left = min(xs) - courtyard_margin_mm
        right = max(xs) + courtyard_margin_mm
        top = min(ys) - courtyard_margin_mm
        bottom = max(ys) + courtyard_margin_mm
    else:
        left, right, top, bottom = -1.0, 1.0, -1.0, 1.0

    courtyard_lines = []
    for l1, l2, layer in (((left, top), (right, top), "F.CrtYd"),
                          ((right, top), (right, bottom), "F.CrtYd"),
                          ((right, bottom), (left, bottom), "F.CrtYd"),
                          ((left, bottom), (left, top), "F.CrtYd")):
        courtyard_lines.append(
            f"  (fp_line (start {l1[0]:.4f} {l1[1]:.4f}) (end {l2[0]:.4f} {l2[1]:.4f}) "
            f"(stroke (width 0.05) (type solid)) (layer \"{layer}\"))"
        )

    silk_lines = []
    for seg in silk_outline:
        t = (seg.get("type") or "").lower()
        if t == "line":
            s = seg.get("start_mm") or [0, 0]
            e = seg.get("end_mm") or [0, 0]
            silk_lines.append(
                f"  (fp_line (start {float(s[0]):.4f} {float(s[1]):.4f}) "
                f"(end {float(e[0]):.4f} {float(e[1]):.4f}) "
                f"(stroke (width 0.12) (type solid)) (layer \"F.SilkS\"))"
            )
        elif t == "circle":
            c = seg.get("center_mm") or [0, 0]
            r = float(seg.get("radius_mm", 0.3))
            silk_lines.append(
                f"  (fp_circle (center {float(c[0]):.4f} {float(c[1]):.4f}) "
                f"(end {float(c[0]) + r:.4f} {float(c[1]):.4f}) "
                f"(stroke (width 0.12) (type solid)) (fill none) (layer \"F.SilkS\"))"
            )

    lines = [
        f"(footprint \"{name}\" (version 20231120) (generator kicad_ai)",
        "  (layer \"F.Cu\")",
        f"  (descr \"{desc}\")",
        f"  (tags \"{tagstr}\")",
        "  (attr smd)",
        f"  (fp_text reference \"REF**\" (at 0 {top - 0.6:.3f}) (layer \"F.SilkS\")",
        "    (effects (font (size 1 1) (thickness 0.15))))",
        f"  (fp_text value \"{name}\" (at 0 {bottom + 0.6:.3f}) (layer \"F.Fab\")",
        "    (effects (font (size 1 1) (thickness 0.15))))",
    ]
    lines.extend(pad_lines)
    lines.extend(courtyard_lines)
    lines.extend(silk_lines)
    lines.append(")")
    return "\n".join(lines) + "\n"


# ---------- Tool implementations ----------

def _find_footprint_by_ref(board, ref: str):
    if not ref:
        return None
    for fp in board.Footprints():
        try:
            if fp.GetReference() == ref:
                return fp
        except Exception:
            continue
    return None


def tool_write_symbol(args: dict, board, project_path: str) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "message": "Missing symbol name."}
    try:
        content = _kicad_sym_file(
            symbol_name=name,
            description=args.get("description", ""),
            pins=args.get("pins") or [],
            properties=args.get("properties") or {},
        )
    except Exception as e:
        return {"ok": False, "message": f"Could not build symbol: {e}"}
    target_dir = os.path.join(_project_dir(board), "ai-generated-symbols")
    try:
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, f"{_sanitize_lib_name(name)}.kicad_sym")
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        fallback = os.path.join(_generated_dir(), "symbols")
        os.makedirs(fallback, exist_ok=True)
        target = os.path.join(fallback, f"{_sanitize_lib_name(name)}.kicad_sym")
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e2:
            return {"ok": False, "message": f"Could not write symbol: {e2}"}
    return {
        "ok": True,
        "message": f"Wrote symbol library to {target}. Add it via Preferences > Manage Symbol Libraries.",
        "path": target,
        "preview": content,
    }


def tool_write_footprint(args: dict, board, project_path: str) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "message": "Missing footprint name."}
    pads = args.get("pads") or []
    if not pads:
        return {"ok": False, "message": "Footprint must have at least one pad."}
    try:
        content = _kicad_mod_file(
            fp_name=name,
            description=args.get("description", ""),
            tags=args.get("tags", ""),
            pads=pads,
            courtyard_margin_mm=float(args.get("courtyard_margin_mm", 0.25)),
            silk_outline=args.get("silk_outline") or [],
        )
    except Exception as e:
        return {"ok": False, "message": f"Could not build footprint: {e}"}
    pretty_dir = os.path.join(_project_dir(board), "ai-generated.pretty")
    try:
        os.makedirs(pretty_dir, exist_ok=True)
        target = os.path.join(pretty_dir, f"{_sanitize_lib_name(name)}.kicad_mod")
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        fallback = os.path.join(_generated_dir(), "ai-generated.pretty")
        os.makedirs(fallback, exist_ok=True)
        target = os.path.join(fallback, f"{_sanitize_lib_name(name)}.kicad_mod")
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e2:
            return {"ok": False, "message": f"Could not write footprint: {e2}"}
    return {
        "ok": True,
        "message": (
            f"Wrote footprint to {target}. Add the containing .pretty folder via "
            "Preferences > Manage Footprint Libraries."
        ),
        "path": target,
        "preview": content,
    }


def tool_move_footprint(args: dict, board, project_path: str) -> dict:
    ref = str(args.get("ref") or "").strip()
    fp = _find_footprint_by_ref(board, ref)
    if fp is None:
        return {"ok": False, "message": f"No footprint with reference '{ref}'."}
    try:
        x_iu = pcbnew.FromMM(float(args["x_mm"]))
        y_iu = pcbnew.FromMM(float(args["y_mm"]))
        fp.SetPosition(_vector2i(x_iu, y_iu))
        if "rotation_deg" in args and args["rotation_deg"] is not None:
            rot = float(args["rotation_deg"])
            if hasattr(fp, "SetOrientationDegrees"):
                fp.SetOrientationDegrees(rot)
            else:
                fp.SetOrientation(rot * 10)
        if args.get("flip_to"):
            target_layer = str(args["flip_to"])
            current = _layer_name_safe(board, fp.GetLayer())
            if target_layer != current:
                try:
                    fp.Flip(_vector2i(x_iu, y_iu), False)
                except Exception:
                    pass
        pcbnew.Refresh()
    except Exception as e:
        return {"ok": False, "message": f"Move failed: {e}"}
    return {
        "ok": True,
        "message": f"Moved {ref} to ({args['x_mm']}, {args['y_mm']}) mm.",
    }


def tool_arrange_footprints(args: dict, board, project_path: str) -> dict:
    placements = args.get("placements") or []
    if not placements:
        return {"ok": False, "message": "No placements provided."}
    results = []
    for p in placements:
        sub = tool_move_footprint(p, board, project_path)
        results.append({
            "ref": p.get("ref", ""),
            "ok": sub.get("ok", False),
            "message": sub.get("message", ""),
        })
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "ok": ok_count > 0,
        "message": f"Applied {ok_count}/{len(results)} placements.",
        "results": results,
    }


def tool_create_track(args: dict, board, project_path: str) -> dict:
    try:
        start = args.get("start_mm") or [0, 0]
        end = args.get("end_mm") or [0, 0]
        width = float(args.get("width_mm", 0.25))
        layer_name = str(args.get("layer") or "F.Cu")
        net_name = str(args.get("net") or "").strip()

        track = pcbnew.PCB_TRACK(board)
        track.SetStart(_vector2i(pcbnew.FromMM(float(start[0])), pcbnew.FromMM(float(start[1]))))
        track.SetEnd(_vector2i(pcbnew.FromMM(float(end[0])), pcbnew.FromMM(float(end[1]))))
        track.SetLayer(_layer_id_from_name(board, layer_name))
        track.SetWidth(pcbnew.FromMM(width))
        if net_name:
            try:
                net = board.FindNet(net_name)
                if net is not None:
                    track.SetNetCode(net.GetNetCode())
            except Exception:
                pass
        board.Add(track)
        pcbnew.Refresh()
    except Exception as e:
        return {"ok": False, "message": f"Track creation failed: {e}"}
    return {"ok": True, "message": f"Created track on {args.get('layer','F.Cu')} for net {args.get('net','')}."}


def tool_create_via(args: dict, board, project_path: str) -> dict:
    try:
        pos = args.get("position_mm") or [0, 0]
        size = float(args.get("size_mm", 0.6))
        drill = float(args.get("drill_mm", 0.3))
        from_layer = _layer_id_from_name(board, str(args.get("from_layer") or "F.Cu"))
        to_layer = _layer_id_from_name(board, str(args.get("to_layer") or "B.Cu"))
        net_name = str(args.get("net") or "").strip()

        via = pcbnew.PCB_VIA(board)
        via.SetPosition(_vector2i(pcbnew.FromMM(float(pos[0])), pcbnew.FromMM(float(pos[1]))))
        try:
            via.SetWidth(pcbnew.FromMM(size))
        except Exception:
            pass
        try:
            via.SetDrill(pcbnew.FromMM(drill))
        except Exception:
            pass
        try:
            via.SetLayerPair(from_layer, to_layer)
        except Exception:
            pass
        if net_name:
            try:
                net = board.FindNet(net_name)
                if net is not None:
                    via.SetNetCode(net.GetNetCode())
            except Exception:
                pass
        board.Add(via)
        pcbnew.Refresh()
    except Exception as e:
        return {"ok": False, "message": f"Via creation failed: {e}"}
    return {"ok": True, "message": f"Created via at {args.get('position_mm')} for net {args.get('net','')}."}


def tool_delete_track(args: dict, board, project_path: str) -> dict:
    idx = args.get("index")
    if idx is None:
        return {"ok": False, "message": "Missing track index."}
    try:
        tracks = list(board.GetTracks())
        i = int(idx)
        if i < 0 or i >= len(tracks):
            return {"ok": False, "message": f"Track index {i} out of range (have {len(tracks)})."}
        board.Remove(tracks[i])
        pcbnew.Refresh()
    except Exception as e:
        return {"ok": False, "message": f"Delete failed: {e}"}
    return {"ok": True, "message": f"Removed track #{idx}."}


# ---------- Mode classes ----------

class Mode:
    id: str = ""
    label: str = ""
    hint: str = ""
    system_prompt: str = ""

    def gather_context(self, board, project_path: str, attachments: list,
                       context_modules: Optional[dict] = None) -> dict:
        return {}

    def tool_schema(self) -> list[dict]:
        return []

    def execute_tool(self, name: str, args: dict, board, project_path: str) -> dict:
        return {"ok": False, "message": f"Unknown tool {name}"}


class SymbolBuilderMode(Mode):
    id = "symbol_builder"
    label = "Symbol Builder"
    hint = "Describe the part, or attach a datasheet PDF. You can also attach an existing .kicad_sym to edit."
    system_prompt = (
        "MODE: Symbol Builder.\n"
        "Goal: help the user build a new KiCad schematic symbol (.kicad_sym) from a description, "
        "datasheet, or existing symbol. Determine: reference prefix, pin list (number, name, "
        "electrical_type, side), properties (Reference, Value, Footprint, Datasheet). "
        "When you have enough information, call the tool `create_symbol` to write the symbol. "
        "Use standard KiCad electrical types: input, output, bidirectional, tri_state, passive, "
        "power_in, power_out, open_collector, open_emitter, no_connect, unspecified."
    )

    def gather_context(self, board, project_path, attachments, context_modules=None):
        return {
            "project_dir": _project_dir(board),
            "attachments_summary": [
                {"filename": a.get("filename"), "kind": a.get("kind"), "size": a.get("size")}
                for a in attachments
            ],
        }

    def tool_schema(self):
        return [{
            "type": "function",
            "function": {
                "name": "create_symbol",
                "description": "Generate a KiCad .kicad_sym library file for a single schematic symbol.",
                "parameters": {
                    "type": "object",
                    "required": ["name", "pins"],
                    "properties": {
                        "name": {"type": "string", "description": "Symbol name (e.g. STM32F103C8T6)."},
                        "description": {"type": "string"},
                        "properties": {
                            "type": "object",
                            "properties": {
                                "Reference": {"type": "string", "description": "Reference prefix like U, R, C."},
                                "Value": {"type": "string"},
                                "Footprint": {"type": "string"},
                                "Datasheet": {"type": "string"},
                                "Description": {"type": "string"},
                            },
                        },
                        "pins": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["number", "name"],
                                "properties": {
                                    "number": {"type": "string"},
                                    "name": {"type": "string"},
                                    "electrical_type": {
                                        "type": "string",
                                        "enum": ["input", "output", "bidirectional", "tri_state",
                                                 "passive", "free", "unspecified", "power_in",
                                                 "power_out", "open_collector", "open_emitter",
                                                 "no_connect"],
                                    },
                                    "side": {
                                        "type": "string",
                                        "enum": ["left", "right", "top", "bottom"],
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }]

    def execute_tool(self, name, args, board, project_path):
        if name == "create_symbol":
            return tool_write_symbol(args, board, project_path)
        return super().execute_tool(name, args, board, project_path)


class SymbolToFootprintMode(Mode):
    id = "symbol_to_footprint"
    label = "Symbol to Footprint"
    hint = "Attach the .kicad_sym and the datasheet. I will infer pin count, pitch, and package."
    system_prompt = (
        "MODE: Symbol to Footprint.\n"
        "Goal: build a matching KiCad footprint (.kicad_mod) for a symbol plus a datasheet. "
        "Determine: package family (SOIC, QFP, BGA, TH, etc.), pad count, pitch, pad size, and "
        "courtyard. Align pad numbers with symbol pin numbers. When ready, call "
        "`create_footprint` with the pad geometry, and optionally `place_on_board` to drop it on "
        "the current PCB."
    )

    def gather_context(self, board, project_path, attachments, context_modules=None):
        return {
            "project_dir": _project_dir(board),
            "attachments_summary": [
                {"filename": a.get("filename"), "kind": a.get("kind"), "size": a.get("size")}
                for a in attachments
            ],
            "board_file": project_path,
        }

    def tool_schema(self):
        return [{
            "type": "function",
            "function": {
                "name": "create_footprint",
                "description": "Generate a KiCad .kicad_mod footprint file.",
                "parameters": {
                    "type": "object",
                    "required": ["name", "pads"],
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "string"},
                        "courtyard_margin_mm": {"type": "number"},
                        "pads": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["number", "shape", "size_mm", "position_mm"],
                                "properties": {
                                    "number": {"type": "string"},
                                    "shape": {"type": "string",
                                               "enum": ["rect", "roundrect", "circle", "oval"]},
                                    "pad_type": {"type": "string",
                                                  "enum": ["smd", "thru_hole", "np_thru_hole"]},
                                    "size_mm": {"type": "array", "items": {"type": "number"},
                                                 "minItems": 2, "maxItems": 2},
                                    "position_mm": {"type": "array", "items": {"type": "number"},
                                                     "minItems": 2, "maxItems": 2},
                                    "rotation_deg": {"type": "number"},
                                    "drill_mm": {"type": "number"},
                                    "layers": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "silk_outline": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["line", "circle"]},
                                    "start_mm": {"type": "array", "items": {"type": "number"}},
                                    "end_mm": {"type": "array", "items": {"type": "number"}},
                                    "center_mm": {"type": "array", "items": {"type": "number"}},
                                    "radius_mm": {"type": "number"},
                                },
                            },
                        },
                    },
                },
            },
        }]

    def execute_tool(self, name, args, board, project_path):
        if name == "create_footprint":
            return tool_write_footprint(args, board, project_path)
        return super().execute_tool(name, args, board, project_path)


class PCBLayoutMode(Mode):
    id = "pcb_layout"
    label = "PCB Layout"
    system_prompt = (
        "MODE: PCB Layout.\n"
        "Goal: propose and execute footprint placement changes. Use the board context (outline, "
        "existing components, positions, rotations, layer) to compute positions in millimetres. "
        "Coordinates follow KiCad convention (+X right, +Y down). When applying multiple moves, "
        "prefer a single `arrange_footprints` call."
    )

    def gather_context(self, board, project_path, attachments, context_modules=None):
        opts = context_modules or {}
        if opts.get("include_board_data", True):
            rows = _collect_component_rows(board)
            return gather_board_context_for_ai(board, rows)
        return {"board_file": project_path}

    def tool_schema(self):
        placement_props = {
            "ref": {"type": "string"},
            "x_mm": {"type": "number"},
            "y_mm": {"type": "number"},
            "rotation_deg": {"type": "number"},
            "flip_to": {"type": "string", "enum": ["F.Cu", "B.Cu"]},
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "move_footprint",
                    "description": "Move a single footprint by reference to an absolute mm position.",
                    "parameters": {
                        "type": "object",
                        "required": ["ref", "x_mm", "y_mm"],
                        "properties": placement_props,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "arrange_footprints",
                    "description": "Apply multiple footprint placements in a single batch.",
                    "parameters": {
                        "type": "object",
                        "required": ["placements"],
                        "properties": {
                            "placements": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["ref", "x_mm", "y_mm"],
                                    "properties": placement_props,
                                },
                            },
                        },
                    },
                },
            },
        ]

    def execute_tool(self, name, args, board, project_path):
        if name == "move_footprint":
            return tool_move_footprint(args, board, project_path)
        if name == "arrange_footprints":
            return tool_arrange_footprints(args, board, project_path)
        return super().execute_tool(name, args, board, project_path)


class RoutingMode(Mode):
    id = "routing"
    label = "Routing"
    system_prompt = (
        "MODE: Routing.\n"
        "Goal: propose and execute copper tracks and vias. Use the nets summary "
        "(net → pad anchors in mm) and existing tracks to plan routes. Use `create_track` to add a "
        "segment, `create_via` to layer-change, and `delete_track` to remove a bad segment by index. "
        "Keep widths sensible (0.2–0.5 mm for signals)."
    )

    def gather_context(self, board, project_path, attachments, context_modules=None):
        base: dict[str, Any] = {"board_file": project_path}
        opts = context_modules or {}
        if opts.get("include_board_data", True):
            rows = _collect_component_rows(board)
            base.update(gather_board_context_for_ai(board, rows))
        base["nets"] = gather_nets_summary(board)
        return base

    def tool_schema(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_track",
                    "description": "Create a straight copper segment on a layer for a net.",
                    "parameters": {
                        "type": "object",
                        "required": ["net", "layer", "start_mm", "end_mm", "width_mm"],
                        "properties": {
                            "net": {"type": "string"},
                            "layer": {"type": "string"},
                            "start_mm": {"type": "array", "items": {"type": "number"},
                                          "minItems": 2, "maxItems": 2},
                            "end_mm": {"type": "array", "items": {"type": "number"},
                                        "minItems": 2, "maxItems": 2},
                            "width_mm": {"type": "number"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_via",
                    "description": "Create a through/blind via for a net between two layers.",
                    "parameters": {
                        "type": "object",
                        "required": ["net", "position_mm", "size_mm", "drill_mm"],
                        "properties": {
                            "net": {"type": "string"},
                            "position_mm": {"type": "array", "items": {"type": "number"},
                                             "minItems": 2, "maxItems": 2},
                            "size_mm": {"type": "number"},
                            "drill_mm": {"type": "number"},
                            "from_layer": {"type": "string"},
                            "to_layer": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_track",
                    "description": "Delete a track or via by its index in the existing_tracks array.",
                    "parameters": {
                        "type": "object",
                        "required": ["index"],
                        "properties": {"index": {"type": "integer"}},
                    },
                },
            },
        ]

    def execute_tool(self, name, args, board, project_path):
        if name == "create_track":
            return tool_create_track(args, board, project_path)
        if name == "create_via":
            return tool_create_via(args, board, project_path)
        if name == "delete_track":
            return tool_delete_track(args, board, project_path)
        return super().execute_tool(name, args, board, project_path)


def _collect_component_rows(board) -> list:
    ec = edge_cut_row(board)
    foot = [footprint_row(fp) for fp in board.Footprints()]
    foot = sort_rows_top_to_bottom_then_left_to_right(foot)
    if ec:
        return [ec] + foot
    return foot


MODES: dict[str, Mode] = {
    SymbolBuilderMode.id: SymbolBuilderMode(),
    SymbolToFootprintMode.id: SymbolToFootprintMode(),
    PCBLayoutMode.id: PCBLayoutMode(),
    RoutingMode.id: RoutingMode(),
}

DEFAULT_MODE_ID = PCBLayoutMode.id


def get_mode(mode_id: Optional[str]) -> Mode:
    return MODES.get((mode_id or "").strip(), MODES[DEFAULT_MODE_ID])


def _history_to_openai_messages(chat_history: list) -> list:
    """Convert our internal chat history into OpenAI-compatible messages.

    Our history stores assistant turns with optional proposed tool_calls plus per-call
    status/result. OpenAI doesn't need the tool protocol here since we gate execution
    on user approval locally; we instead narrate applied/discarded actions as text.
    """
    out = []
    for msg in chat_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            narration_bits = []
            for tc in tool_calls:
                status = tc.get("status", "pending")
                name = tc.get("name", "")
                args = tc.get("args", {})
                result = tc.get("result") or {}
                args_str = json.dumps(args, ensure_ascii=False)
                if status == "applied":
                    narration_bits.append(
                        f"[Applied action: {name}({args_str}) -> "
                        f"{result.get('message','ok')}]"
                    )
                elif status == "discarded":
                    narration_bits.append(f"[Discarded action: {name}({args_str})]")
                else:
                    narration_bits.append(f"[Proposed action (pending): {name}({args_str})]")
            merged = (content or "").strip()
            if narration_bits:
                merged = (merged + "\n\n" + "\n".join(narration_bits)).strip()
            out.append({"role": "assistant", "content": merged or "(no text)"})
            continue
        # For user messages we keep content as-is (string or vision content parts list).
        out.append({"role": role, "content": content})
    return out


def fetch_ai_chat_response(
    chat_history: list,
    user_rules: dict[str, str],
    board,
    mode: Mode,
    attachments: Optional[list] = None,
    context_modules: Optional[dict] = None,
    screenshot_window=None,
) -> tuple[str, list[dict]]:
    """Send a chat turn to OpenAI in the given mode. Returns (text, tool_calls).

    tool_calls is a list of dicts with {id, name, args, status: 'pending'}.
    context_modules keys (all default True if omitted):
      include_board_data — JSON footprint/outline context from pcbnew
      include_f_cu_svg — F.Cu SVG plot excerpt
      include_screenshot — PNG capture of the PCB editor window (vision models)
    """
    api_key, model = get_openai_settings()
    if not api_key:
        raise ValueError("Missing OpenAI API key. Set OPENAI_API_KEY in .env / environment.")
    if not model:
        raise ValueError("Missing OpenAI model. Set OPENAI_MODEL in .env / environment.")

    attachments = attachments or []
    opts = context_modules or {}
    include_svg = opts.get("include_f_cu_svg", True)
    include_shot = opts.get("include_screenshot", True)

    project_path = ""
    try:
        project_path = board.GetFileName() if hasattr(board, "GetFileName") else ""
    except Exception:
        pass

    mode_context = mode.gather_context(board, project_path, attachments, context_modules=opts)

    if include_svg:
        try:
            svg_excerpt = try_export_board_svg_snippet(board)
            if svg_excerpt:
                mode_context.setdefault("svg_f_cu_plot_excerpt", svg_excerpt)
        except Exception:
            pass

    vision_ok = model_supports_openai_vision(model) or bool(opts.get("force_vision"))
    kicad_ai_log(
        f"vision check: model={model!r} heuristic={model_supports_openai_vision(model)} "
        f"force_vision={bool(opts.get('force_vision'))} effective={vision_ok}"
    )
    png_bytes = try_capture_kicad_window_png_bytes(screenshot_window) if include_shot else None
    use_image = bool(png_bytes) and vision_ok

    system_prompt = (
        _BASE_GUARDRAILS
        + "\n"
        + mode.system_prompt
        + _user_rules_block(user_rules)
    )

    context_text = (
        f"ACTIVE MODE: {mode.label}\n\n"
        "Here is the current KiCad context for this turn (JSON):\n\n"
        + json.dumps(mode_context, indent=2, default=str)
    )
    if include_shot and png_bytes and not use_image:
        context_text += (
            "\n\nNote: A PCB editor screenshot was captured but not attached because the selected model "
            "may not support images. Use a vision-capable model such as gpt-4o to include the screenshot."
        )

    context_parts: list[dict] = [{"type": "text", "text": context_text}]
    if use_image and png_bytes:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        win_title = ""
        try:
            if screenshot_window is not None and hasattr(screenshot_window, "GetTitle"):
                win_title = screenshot_window.GetTitle() or ""
        except Exception:
            win_title = ""
        caption = (
            f"The following image is a PNG capture of the KiCad window '{win_title}'."
            if win_title else
            "The following image is a PNG capture of the active KiCad editor window."
        )
        context_parts.append({"type": "text", "text": caption})
        context_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    for att in attachments:
        context_parts.extend(attachment_to_message_parts(att, use_image=vision_ok))

    # Collapse to plain text for non-vision models if there are no image parts.
    has_images = any(p.get("type") == "image_url" for p in context_parts)
    if has_images:
        context_message_content: Union[list, str] = context_parts
    else:
        context_message_content = "\n\n".join(
            p.get("text", "") for p in context_parts if p.get("type") == "text"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_message_content},
        {"role": "assistant",
         "content": f"I have the context for {mode.label}. How can I help?"},
    ]
    messages.extend(_history_to_openai_messages(chat_history))

    request_payload: dict = {
        "model": model,
        "temperature": 0.3,
        "messages": messages,
    }
    tool_schema = mode.tool_schema()
    if tool_schema:
        request_payload["tools"] = tool_schema
        request_payload["tool_choice"] = "auto"

    # ---- Log payload shape -------------------------------------------------
    try:
        image_count = 0
        text_chars = 0
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                text_chars += len(c)
            elif isinstance(c, list):
                for part in c:
                    if part.get("type") == "image_url":
                        image_count += 1
                    elif part.get("type") == "text":
                        text_chars += len(part.get("text", ""))
        kicad_ai_log(
            f"OpenAI request: model={model} mode={mode.id} messages={len(messages)} "
            f"text_chars={text_chars} image_parts={image_count} "
            f"tools={len(tool_schema) if tool_schema else 0} "
            f"attachments_this_turn={len(attachments or [])} "
            f"use_image_screenshot={use_image} vision_ok={vision_ok}"
        )
        # Log which attachments went along on this turn.
        for a in (attachments or []):
            kicad_ai_log(
                f"  turn attachment: {a.get('filename','?')} kind={a.get('kind','?')} "
                f"size={a.get('size',0)}B"
            )
    except Exception as _e:
        kicad_ai_log(f"OpenAI request: (could not summarize payload: {_e})")

    request_body = json.dumps(request_payload).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        kicad_ai_log("OpenAI response: received")
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            message = error_payload.get("error", {}).get("message") or str(exc)
        except Exception:
            message = str(exc)
        kicad_ai_log(f"OpenAI error (HTTP): {message}")
        raise ValueError(f"OpenAI request failed: {message}") from exc
    except urllib.error.URLError as exc:
        kicad_ai_log(f"OpenAI error (URL): {exc.reason}")
        raise ValueError(f"OpenAI request failed: {exc.reason}") from exc

    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("OpenAI returned no choices.")

    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    raw_tool_calls = msg.get("tool_calls") or []
    kicad_ai_log(
        f"OpenAI reply: text_len={len(text)} tool_calls={len(raw_tool_calls)}"
    )
    tool_calls: list[dict] = []
    for tc in raw_tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        tool_calls.append({
            "id": tc.get("id") or str(uuid.uuid4()),
            "name": name,
            "args": args,
            "status": "pending",
            "result": None,
        })

    return text, tool_calls


# ---------------------------------------------------------------------------
# Rules persistence
# ---------------------------------------------------------------------------

def _rules_file_path() -> str:
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, "kicad-ai-rules.txt")


def load_user_rules() -> dict[str, str]:
    path = _rules_file_path()
    if not os.path.isfile(path):
        return _empty_user_rules()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return _empty_user_rules()

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            rules = _empty_user_rules()
            for key, _label in RULE_SECTION_LABELS:
                value = data.get(key, "")
                if isinstance(value, str):
                    rules[key] = value
            return rules
    except Exception:
        pass

    # Backward compatibility: older versions stored a single text blob.
    rules = _empty_user_rules()
    rules["general_rules"] = raw
    return rules


def save_user_rules(rules: dict[str, str]) -> tuple[bool, str]:
    path = _rules_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2)
        return True, ""
    except OSError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------

def _plugin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _data_root() -> str:
    return os.path.join(_plugin_dir(), "kicad-ai-data")


def _conversations_dir() -> str:
    path = os.path.join(_data_root(), "conversations")
    os.makedirs(path, exist_ok=True)
    return path


def _attachments_root() -> str:
    path = os.path.join(_data_root(), "attachments")
    os.makedirs(path, exist_ok=True)
    return path


def _attachments_dir(conv_id: str) -> str:
    path = os.path.join(_attachments_root(), conv_id)
    os.makedirs(path, exist_ok=True)
    return path


def _index_path() -> str:
    return os.path.join(_data_root(), "index.json")


def _generated_dir() -> str:
    path = os.path.join(_plugin_dir(), "generated")
    os.makedirs(path, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty_index() -> dict:
    return {"current_id": None, "conversations": {}}


def load_conversations_index() -> dict:
    path = _index_path()
    if not os.path.isfile(path):
        return _empty_index()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_index()
        if "conversations" not in data or not isinstance(data["conversations"], dict):
            data["conversations"] = {}
        if "current_id" not in data:
            data["current_id"] = None
        return data
    except Exception:
        return _empty_index()


def save_conversations_index(index: dict) -> None:
    path = _index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
    except OSError:
        pass


def _conversation_file(conv_id: str) -> str:
    return os.path.join(_conversations_dir(), f"{conv_id}.json")


def load_conversation(conv_id: str) -> Optional[dict]:
    path = _conversation_file(conv_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("messages", [])
            data.setdefault("attachments", {})
            return data
    except Exception:
        return None
    return None


def save_conversation(conv: dict) -> None:
    if not conv or not conv.get("id"):
        return
    path = _conversation_file(conv["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(conv, f, indent=2)
    except OSError:
        pass


def new_conversation(mode_id: str) -> dict:
    conv_id = str(uuid.uuid4())
    now = _now_iso()
    conv = {
        "id": conv_id,
        "title": "New chat",
        "mode": mode_id,
        "created_at": now,
        "last_updated": now,
        "messages": [],
        "attachments": {},
    }
    save_conversation(conv)
    index = load_conversations_index()
    index["conversations"][conv_id] = {
        "id": conv_id,
        "title": conv["title"],
        "mode": mode_id,
        "created_at": now,
        "last_updated": now,
        "message_count": 0,
    }
    index["current_id"] = conv_id
    save_conversations_index(index)
    return conv


def auto_title_from_first_message(text: str, max_len: int = 40) -> str:
    s = (text or "").strip().splitlines()[0] if text and text.strip() else ""
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "New chat"
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def touch_conversation(conv: dict) -> None:
    now = _now_iso()
    conv["last_updated"] = now
    index = load_conversations_index()
    entry = index["conversations"].get(conv["id"], {})
    entry.update({
        "id": conv["id"],
        "title": conv.get("title", "New chat"),
        "mode": conv.get("mode", "pcb_layout"),
        "created_at": conv.get("created_at", now),
        "last_updated": now,
        "message_count": len(conv.get("messages", [])),
    })
    index["conversations"][conv["id"]] = entry
    index["current_id"] = conv["id"]
    save_conversations_index(index)


def rename_conversation(conv_id: str, title: str) -> None:
    conv = load_conversation(conv_id)
    if not conv:
        return
    conv["title"] = title or "New chat"
    conv["last_updated"] = _now_iso()
    save_conversation(conv)
    index = load_conversations_index()
    if conv_id in index["conversations"]:
        index["conversations"][conv_id]["title"] = conv["title"]
        index["conversations"][conv_id]["last_updated"] = conv["last_updated"]
    save_conversations_index(index)


def delete_conversation(conv_id: str) -> None:
    try:
        path = _conversation_file(conv_id)
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    try:
        att_dir = os.path.join(_attachments_root(), conv_id)
        if os.path.isdir(att_dir):
            shutil.rmtree(att_dir, ignore_errors=True)
    except OSError:
        pass
    index = load_conversations_index()
    if conv_id in index["conversations"]:
        del index["conversations"][conv_id]
    if index.get("current_id") == conv_id:
        index["current_id"] = None
    save_conversations_index(index)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
TEXT_EXTS = {
    ".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".kicad_sym", ".kicad_sch", ".kicad_pcb", ".kicad_mod", ".kicad_pro",
    ".kicad_wks", ".net", ".lib", ".py", ".sh", ".ini",
}
PDF_EXTS = {".pdf"}

MAX_PDF_PAGES = 8
MAX_TEXT_ATTACHMENT_CHARS = 24000


def _attachment_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    # Heuristic: try to read as text; otherwise mark as unknown.
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        if b"\0" in chunk:
            return "unknown"
        chunk.decode("utf-8")
        return "text"
    except Exception:
        return "unknown"


def ingest_attachment(conv_id: str, src_path: str) -> Optional[dict]:
    """Copy a file into the conversation attachments folder and describe it."""
    if not src_path or not os.path.isfile(src_path):
        return None
    kind = _attachment_kind(src_path)
    if kind == "unknown":
        return None
    att_dir = _attachments_dir(conv_id)
    att_id = str(uuid.uuid4())
    base = os.path.basename(src_path)
    stored_name = f"{att_id}__{base}"
    dst_path = os.path.join(att_dir, stored_name)
    try:
        shutil.copy2(src_path, dst_path)
    except OSError:
        return None
    try:
        size = os.path.getsize(dst_path)
    except OSError:
        size = 0
    return {
        "id": att_id,
        "filename": base,
        "kind": kind,
        "path": dst_path,
        "size": size,
    }


def _read_image_as_png_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return _downscale_png_bytes_if_large(raw)
    try:
        stream_in = io.BytesIO(raw)
        img = wx.Image(stream_in)
        if not img.IsOk():
            return None
        out = io.BytesIO()
        if not img.SaveFile(out, wx.BITMAP_TYPE_PNG):
            return None
        return _downscale_png_bytes_if_large(out.getvalue())
    except Exception:
        return None


def _pdf_pages_to_png_bytes(path: str, max_pages: int = MAX_PDF_PAGES) -> tuple[list[bytes], str]:
    """Render each page of a PDF to PNG. Tries pypdfium2, then PyMuPDF (fitz).

    Returns (pages, backend_name). Empty list + backend="none" if nothing worked.
    """
    fname = os.path.basename(path)
    size_info = ""
    try:
        size_info = f" size={os.path.getsize(path)}B"
    except Exception:
        pass
    kicad_ai_log(f"PDF raster: starting for '{fname}'{size_info}")

    # Backend 1: pypdfium2
    try:
        import pypdfium2 as pdfium  # type: ignore
        kicad_ai_log("PDF raster: trying pypdfium2")
        out: list[bytes] = []
        try:
            pdf = pdfium.PdfDocument(path)
            total = len(pdf)
            n = min(total, max_pages)
            kicad_ai_log(f"PDF raster: pypdfium2 opened; total_pages={total}, will render {n}")
            for i in range(n):
                page = pdf[i]
                try:
                    pil = page.render(scale=150.0 / 72.0).to_pil()
                except Exception as e:
                    kicad_ai_log(f"PDF raster: pypdfium2 page {i+1} render failed: {e}")
                    continue
                buf = io.BytesIO()
                try:
                    pil.save(buf, format="PNG")
                except Exception as e:
                    kicad_ai_log(f"PDF raster: pypdfium2 page {i+1} PNG encode failed: {e}")
                    continue
                out.append(_downscale_png_bytes_if_large(buf.getvalue()))
            if out:
                kicad_ai_log(f"PDF raster: pypdfium2 produced {len(out)} page image(s)")
                return out, "pypdfium2"
            kicad_ai_log("PDF raster: pypdfium2 produced 0 pages")
        except Exception as e:
            kicad_ai_log(f"PDF raster: pypdfium2 failed while reading document: {e}")
    except Exception as e:
        kicad_ai_log(f"PDF raster: pypdfium2 not importable ({e.__class__.__name__})")

    # Backend 2: PyMuPDF (fitz)
    try:
        import fitz  # type: ignore
        kicad_ai_log("PDF raster: trying fitz (PyMuPDF)")
        out2: list[bytes] = []
        try:
            doc = fitz.open(path)
            total = doc.page_count
            n = min(total, max_pages)
            kicad_ai_log(f"PDF raster: fitz opened; total_pages={total}, will render {n}")
            for i in range(n):
                try:
                    page = doc.load_page(i)
                    pix = page.get_pixmap(matrix=fitz.Matrix(150.0 / 72.0, 150.0 / 72.0))
                    png_data = pix.tobytes("png")
                except Exception as e:
                    kicad_ai_log(f"PDF raster: fitz page {i+1} failed: {e}")
                    continue
                out2.append(_downscale_png_bytes_if_large(png_data))
            doc.close()
            if out2:
                kicad_ai_log(f"PDF raster: fitz produced {len(out2)} page image(s)")
                return out2, "fitz"
            kicad_ai_log("PDF raster: fitz produced 0 pages")
        except Exception as e:
            kicad_ai_log(f"PDF raster: fitz failed while reading document: {e}")
    except Exception as e:
        kicad_ai_log(f"PDF raster: fitz not importable ({e.__class__.__name__})")

    kicad_ai_log("PDF raster: no backend produced any image")
    return [], "none"


def _pdf_extract_text(path: str, max_pages: int = MAX_PDF_PAGES * 4,
                       max_chars: int = MAX_TEXT_ATTACHMENT_CHARS * 4) -> tuple[str, str]:
    """Extract text from a PDF. Tries pypdf, PyPDF2, pdfminer.six, then fitz.

    Returns (text, backend_name). Empty text + backend="none" if nothing worked.
    """
    fname = os.path.basename(path)
    kicad_ai_log(f"PDF text: starting extraction for '{fname}'")

    for modname in ("pypdf", "PyPDF2"):
        try:
            mod = __import__(modname)
        except Exception as e:
            kicad_ai_log(f"PDF text: {modname} not importable ({e.__class__.__name__})")
            continue
        try:
            kicad_ai_log(f"PDF text: trying {modname}")
            reader = mod.PdfReader(path)  # type: ignore[attr-defined]
            pieces: list[str] = []
            total = 0
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages):
                if i >= max_pages:
                    break
                try:
                    t = page.extract_text() or ""
                except Exception as e:
                    kicad_ai_log(f"PDF text: {modname} page {i+1} extract failed: {e}")
                    t = ""
                if not t:
                    continue
                pieces.append(f"--- page {i + 1} ---\n{t.strip()}")
                total += len(t)
                if total >= max_chars:
                    break
            joined = "\n\n".join(pieces)
            if joined.strip():
                if len(joined) > max_chars:
                    joined = joined[:max_chars] + "\n<!-- truncated -->"
                kicad_ai_log(
                    f"PDF text: {modname} success — pages={page_count}, chars={len(joined)}"
                )
                kicad_ai_log(f"PDF text: first 200 chars: {joined[:200].replace(chr(10),' ')}")
                return joined, modname
            kicad_ai_log(f"PDF text: {modname} returned 0 chars (likely image-based PDF)")
        except Exception as e:
            kicad_ai_log(f"PDF text: {modname} reader failed: {e}")
            continue

    try:
        from pdfminer.high_level import extract_text as _pdfminer_extract  # type: ignore
        kicad_ai_log("PDF text: trying pdfminer.six")
        try:
            t = _pdfminer_extract(path) or ""
            if t.strip():
                if len(t) > max_chars:
                    t = t[:max_chars] + "\n<!-- truncated -->"
                kicad_ai_log(f"PDF text: pdfminer.six success — chars={len(t)}")
                kicad_ai_log(f"PDF text: first 200 chars: {t[:200].replace(chr(10),' ')}")
                return t, "pdfminer.six"
            kicad_ai_log("PDF text: pdfminer.six returned 0 chars")
        except Exception as e:
            kicad_ai_log(f"PDF text: pdfminer.six failed: {e}")
    except Exception as e:
        kicad_ai_log(f"PDF text: pdfminer.six not importable ({e.__class__.__name__})")

    try:
        import fitz  # type: ignore
        kicad_ai_log("PDF text: trying fitz text mode")
        try:
            doc = fitz.open(path)
            pieces = []
            total = 0
            n = min(doc.page_count, max_pages)
            for i in range(n):
                try:
                    t = doc.load_page(i).get_text() or ""
                except Exception as e:
                    kicad_ai_log(f"PDF text: fitz page {i+1} failed: {e}")
                    t = ""
                if not t:
                    continue
                pieces.append(f"--- page {i + 1} ---\n{t.strip()}")
                total += len(t)
                if total >= max_chars:
                    break
            doc.close()
            joined = "\n\n".join(pieces)
            if joined.strip():
                if len(joined) > max_chars:
                    joined = joined[:max_chars] + "\n<!-- truncated -->"
                kicad_ai_log(f"PDF text: fitz success — chars={len(joined)}")
                kicad_ai_log(f"PDF text: first 200 chars: {joined[:200].replace(chr(10),' ')}")
                return joined, "fitz"
            kicad_ai_log("PDF text: fitz returned 0 chars")
        except Exception as e:
            kicad_ai_log(f"PDF text: fitz open/read failed: {e}")
    except Exception as e:
        kicad_ai_log(f"PDF text: fitz not importable ({e.__class__.__name__})")

    kicad_ai_log("PDF text: no backend produced text")
    return "", "none"


def _detect_pdf_backends() -> dict[str, bool]:
    """Report which optional PDF backends are importable (no side effects)."""
    result = {"pypdfium2": False, "fitz": False, "pypdf": False, "PyPDF2": False,
              "pdfminer.six": False}
    for m in ("pypdfium2", "fitz", "pypdf", "PyPDF2"):
        try:
            __import__(m)
            result[m] = True
        except Exception:
            pass
    try:
        __import__("pdfminer.high_level")
        result["pdfminer.six"] = True
    except Exception:
        pass
    return result


def _read_text_truncated(path: str, max_chars: int = MAX_TEXT_ATTACHMENT_CHARS) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read(max_chars + 1)
    except OSError as e:
        return f"[could not read {os.path.basename(path)}: {e}]"
    if len(data) > max_chars:
        return data[:max_chars] + "\n<!-- truncated -->"
    return data


def attachment_to_message_parts(att: dict, use_image: bool) -> list[dict]:
    """Convert an attachment to OpenAI chat message content parts.

    use_image: whether the selected model supports vision image_url parts.
    """
    parts: list[dict] = []
    kind = att.get("kind", "unknown")
    name = att.get("filename", "file")
    path = att.get("path", "")
    kicad_ai_log(
        f"attachment: '{name}' kind={kind} use_image={use_image} path_exists={os.path.isfile(path) if path else False}"
    )
    if not path or not os.path.isfile(path):
        parts.append({"type": "text", "text": f"[attachment missing on disk: {name}]"})
        return parts

    if kind == "image":
        png = _read_image_as_png_bytes(path)
        if png and use_image:
            b64 = base64.b64encode(png).decode("ascii")
            parts.append({"type": "text", "text": f"[Image attachment: {name}]"})
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        else:
            parts.append({
                "type": "text",
                "text": f"[Image attachment {name} could not be attached (no vision model).]",
            })
        return parts

    if kind == "text":
        body = _read_text_truncated(path)
        parts.append({
            "type": "text",
            "text": f"[Text attachment: {name}]\n```\n{body}\n```",
        })
        return parts

    if kind == "pdf":
        pages: list[bytes] = []
        raster_backend = "none"
        if use_image:
            pages, raster_backend = _pdf_pages_to_png_bytes(path)
        else:
            kicad_ai_log(
                f"attachment: '{name}' skipping rasterization (use_image=False; "
                "model probably doesn't support vision)"
            )
        if pages and use_image:
            kicad_ai_log(
                f"attachment: '{name}' attaching {len(pages)} image page(s) via {raster_backend}"
            )
            parts.append({
                "type": "text",
                "text": (
                    f"[PDF attachment: {name} — {len(pages)} page(s) rasterized via {raster_backend}]"
                ),
            })
            for idx, png in enumerate(pages, start=1):
                b64 = base64.b64encode(png).decode("ascii")
                parts.append({"type": "text", "text": f"PDF page {idx}:"})
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            return parts

        text, text_backend = _pdf_extract_text(path)
        if text:
            reason = (
                "vision model not available"
                if not use_image
                else "rasterization backend not installed (pypdfium2/fitz)"
            )
            kicad_ai_log(
                f"attachment: '{name}' attaching text ({len(text)} chars) via {text_backend}; reason={reason}"
            )
            parts.append({
                "type": "text",
                "text": (
                    f"[PDF attachment: {name} — rasterization unavailable ({reason}); "
                    f"falling back to text extraction via {text_backend}. "
                    "Figures, package drawings and mechanical diagrams are NOT included — "
                    "only the text layer.]\n\n"
                    f"```\n{text}\n```"
                ),
            })
            return parts

        kicad_ai_log(f"attachment: '{name}' NO backend produced usable content")
        parts.append({
            "type": "text",
            "text": (
                f"[PDF attachment {name} could not be processed. "
                "Install one of: `pip install pypdfium2` (best; raster + fast) or "
                "`pip install pypdf` (text only). "
                "For vision, also select a vision-capable model such as gpt-4o.]"
            ),
        })
        return parts

    parts.append({"type": "text", "text": f"[Unsupported attachment: {name}]"})
    return parts


KICAD_EDITOR_TITLE_RE = re.compile(
    r"(PCB Editor|Schematic Editor|Symbol Editor|Footprint Editor)"
)


def enumerate_kicad_editor_windows() -> list[dict]:
    """Return a filtered list of top-level wx windows whose titles match known KiCad editors.

    Each entry is {"window": win, "title": title, "editor": matched editor name}.
    """
    found: list[dict] = []
    try:
        wins = wx.GetTopLevelWindows()
    except Exception:
        return found
    for w in wins:
        try:
            if not w.IsShown():
                continue
        except Exception:
            continue
        try:
            title = w.GetTitle() if hasattr(w, "GetTitle") else ""
        except Exception:
            title = ""
        if not title:
            continue
        m = KICAD_EDITOR_TITLE_RE.search(title)
        if not m:
            continue
        found.append({"window": w, "title": title, "editor": m.group(1)})
    # Stable order by editor, then title
    order = ["PCB Editor", "Schematic Editor", "Symbol Editor", "Footprint Editor"]
    found.sort(key=lambda r: (order.index(r["editor"]) if r["editor"] in order else 99, r["title"]))
    return found


def _guess_editor_role(class_name: str, win_name: str, title: str) -> str:
    """Best-effort label for KiCad / wx top-level windows."""
    c = (class_name or "").lower()
    n = (win_name or "").lower()
    t = (title or "").lower()
    if "pcbframe" in c or n == "pcbframe" or "pcb editor" in t or ("pcbnew" in t and "pcb" in t):
        return "PCB editor"
    if "schematic" in c or "eeschema" in c or n == "schematicframe" or "schematic editor" in t:
        return "Schematic editor"
    if "gerb" in t or "gerber" in t:
        return "Gerber viewer"
    if "3d" in t and ("viewer" in t or "3d" in c):
        return "3D viewer"
    if "kicad" in t and "project" in t:
        return "Project manager (heuristic)"
    if "dialog" in c:
        return "Dialog"
    if "frame" in c:
        return "Frame"
    return "Window"


def format_open_kicad_windows_report() -> str:
    """Enumerate wx top-level windows (KiCad editors and dialogs)."""
    lines: list[str] = []
    try:
        wins = wx.GetTopLevelWindows()
    except Exception as e:
        return f"Could not list top-level windows: {e}"

    if not wins:
        return "No top-level wx windows reported."

    lines.append(f"Top-level windows: {len(wins)}\n")
    for i, w in enumerate(wins):
        try:
            cls = w.__class__.__name__
            name = w.GetName() or "(unnamed)"
            title = ""
            try:
                if hasattr(w, "GetTitle"):
                    title = w.GetTitle() or ""
            except Exception:
                pass
            role = _guess_editor_role(cls, name, title)

            shown = bool(w.IsShown())
            enabled = bool(w.IsEnabled())
            try:
                iconized = bool(w.IsIconized())
            except Exception:
                iconized = False
            try:
                maximized = bool(w.IsMaximized())
            except Exception:
                maximized = False

            sz = w.GetClientSize()
            pos = w.GetPosition()
            try:
                fp = ""
                if hasattr(w, "GetScreenPosition"):
                    sp = w.GetScreenPosition()
                    fp = f"  Screen position: ({sp.x}, {sp.y})\n"
            except Exception:
                fp = ""

            lines.append(f"--- [{i + 1}] {role} ---")
            lines.append(f"  Class: {cls}")
            lines.append(f"  Window name: {name}")
            if title:
                lines.append(f"  Title: {title}")
            lines.append(
                f"  State: shown={shown}  enabled={enabled}  "
                f"iconized={iconized}  maximized={maximized}"
            )
            lines.append(f"  Client size: {int(sz.width)} × {int(sz.height)} px")
            lines.append(f"  Position (parent coords): ({pos.x}, {pos.y})")
            if fp:
                lines.append(fp.rstrip("\n"))

            try:
                if hasattr(w, "HasFocus"):
                    lines.append(f"  Has keyboard focus: {bool(w.HasFocus())}")
            except Exception:
                pass

            lines.append("")
        except Exception as ex:
            lines.append(f"--- [{i + 1}] (error describing window: {ex}) ---\n")

    return "\n".join(lines).rstrip()


# ===========================================================================
# Main dialog
# ===========================================================================

class _AttachmentsDropTarget(wx.FileDropTarget):
    """Accept file drops anywhere on the dialog and forward them to handle_dropped_files()."""

    def __init__(self, dialog: "KiCadAIDialog"):
        super().__init__()
        self._dialog = dialog

    def OnDropFiles(self, x, y, filenames):
        try:
            self._dialog.handle_dropped_files(list(filenames))
        except Exception:
            return False
        return True


class KiCadAIDialog(wx.Dialog):

    TAB_AI = 0
    TAB_AI_RULES = 1
    TAB_OPEN_WINDOWS = 2
    TAB_LOG = 3

    def __init__(self, parent, board):
        super().__init__(
            parent,
            title="KiCad AI",
            size=(1180, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.board = board
        self._chat_history: list[dict] = []
        self._pending_attachments: list[dict] = []
        self._current_conv: Optional[dict] = None
        self._current_mode_id: str = DEFAULT_MODE_ID
        self._action_buttons_by_call: dict[str, dict] = {}

        sizer = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(
            self,
            label=f"Board: {board.GetFileName() if hasattr(board, 'GetFileName') else ''}",
        )
        sizer.Add(header, 0, wx.ALL | wx.EXPAND, 8)

        notebook = wx.Notebook(self)
        self._notebook = notebook

        # ---- Tab 0: AI Chat (splitter: chat list | main chat) ----
        ai_panel = wx.Panel(notebook)
        ai_root = wx.BoxSizer(wx.VERTICAL)

        splitter = wx.SplitterWindow(ai_panel, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        splitter.SetMinimumPaneSize(200)

        # Left pane — chat history sidebar
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)

        self._new_chat_btn = wx.Button(left_panel, label="+ New Chat")
        self._new_chat_btn.Bind(wx.EVT_BUTTON, self._on_new_chat)
        left_sizer.Add(self._new_chat_btn, 0, wx.ALL | wx.EXPAND, 6)

        chats_label = wx.StaticText(left_panel, label="Chats")
        left_sizer.Add(chats_label, 0, wx.LEFT | wx.RIGHT, 6)

        self._chat_list = wx.ListCtrl(
            left_panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE,
        )
        self._chat_list.InsertColumn(0, "Title", width=200)
        self._chat_list.InsertColumn(1, "Mode", width=140)
        self._chat_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_chat_selected)
        self._chat_list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._on_chat_context_menu)
        left_sizer.Add(self._chat_list, 1, wx.ALL | wx.EXPAND, 6)

        left_panel.SetSizer(left_sizer)

        # Right pane — conversation
        right_panel = wx.Panel(splitter)
        right_sizer = wx.BoxSizer(wx.VERTICAL)

        # 1) Window Select
        win_box = wx.StaticBox(right_panel, label="KiCad window (for screenshot context)")
        win_sizer = wx.StaticBoxSizer(win_box, wx.HORIZONTAL)
        self._window_choice = wx.Choice(right_panel, choices=["(no KiCad windows found)"])
        self._window_choice.SetSelection(0)
        self._window_choice.Bind(wx.EVT_CHOICE, self._on_window_choice)
        self._window_refresh_btn = wx.Button(right_panel, label="Refresh", size=(90, -1))
        self._window_refresh_btn.Bind(wx.EVT_BUTTON, self._on_refresh_windows)
        win_sizer.Add(self._window_choice, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        win_sizer.Add(self._window_refresh_btn, 0, wx.ALL, 4)
        right_sizer.Add(win_sizer, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)

        # 2) Mode
        mode_box = wx.StaticBox(right_panel, label="Mode")
        mode_sizer = wx.StaticBoxSizer(mode_box, wx.VERTICAL)
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        self._mode_ids_in_choice = [
            SymbolBuilderMode.id, SymbolToFootprintMode.id,
            PCBLayoutMode.id, RoutingMode.id,
        ]
        self._mode_choice = wx.Choice(
            right_panel,
            choices=[MODES[mid].label for mid in self._mode_ids_in_choice],
        )
        self._mode_choice.SetSelection(self._mode_ids_in_choice.index(DEFAULT_MODE_ID))
        self._mode_choice.Bind(wx.EVT_CHOICE, self._on_mode_changed)
        mode_row.Add(self._mode_choice, 0, wx.ALL, 4)
        self._mode_hint = wx.StaticText(right_panel, label="")
        hint_font = self._mode_hint.GetFont()
        hint_font.MakeItalic()
        self._mode_hint.SetFont(hint_font)
        mode_row.Add(self._mode_hint, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        mode_sizer.Add(mode_row, 0, wx.EXPAND)
        right_sizer.Add(mode_sizer, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)

        # 3) KiCad context to include
        ctx_box = wx.StaticBox(right_panel, label="KiCad context to include")
        ctx_sizer = wx.StaticBoxSizer(ctx_box, wx.HORIZONTAL)
        self.ctx_board_data = wx.CheckBox(right_panel, label="Board data")
        self.ctx_board_data.SetValue(True)
        self.ctx_f_cu_svg = wx.CheckBox(right_panel, label="F.Cu SVG")
        self.ctx_f_cu_svg.SetValue(True)
        self.ctx_screenshot = wx.CheckBox(right_panel, label="Screenshot (selected window)")
        self.ctx_screenshot.SetValue(True)
        self.ctx_force_vision = wx.CheckBox(right_panel, label="Force vision (rasterize PDFs + images)")
        self.ctx_force_vision.SetToolTip(
            "Check this if your model supports images but is not auto-detected (e.g. gpt-5, "
            "gpt-4.1, o4-mini). When on, PDFs are rasterized via pypdfium2/fitz and sent as "
            "page images."
        )
        self.ctx_force_vision.SetValue(False)
        ctx_sizer.Add(self.ctx_board_data, 0, wx.ALL, 4)
        ctx_sizer.Add(self.ctx_f_cu_svg, 0, wx.ALL, 4)
        ctx_sizer.Add(self.ctx_screenshot, 0, wx.ALL, 4)
        ctx_sizer.Add(self.ctx_force_vision, 0, wx.ALL, 4)
        right_sizer.Add(ctx_sizer, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)

        # 4) Chat response (transcript)
        display_label = wx.StaticText(right_panel, label="Conversation:")
        right_sizer.Add(display_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.chat_display = wx.TextCtrl(
            right_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.chat_display.SetMinSize((-1, 220))
        right_sizer.Add(self.chat_display, 3, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        # 5) Proposed actions
        actions_box = wx.StaticBox(right_panel, label="Proposed actions")
        self._actions_static_box = actions_box
        self._actions_sizer = wx.StaticBoxSizer(actions_box, wx.VERTICAL)
        self._actions_parent = right_panel
        self._actions_empty_label = wx.StaticText(
            right_panel, label="(no pending actions)")
        self._actions_sizer.Add(self._actions_empty_label, 0, wx.ALL, 4)
        right_sizer.Add(self._actions_sizer, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        # 6) Message box
        input_label = wx.StaticText(right_panel, label="Your message:")
        right_sizer.Add(input_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.chat_input = wx.TextCtrl(right_panel, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        self.chat_input.SetMinSize((-1, 80))
        if hasattr(self.chat_input, "SetHint"):
            self.chat_input.SetHint("Type your question here (Shift+Enter for newline)...")
        self.chat_input.Bind(wx.EVT_KEY_DOWN, self._on_chat_input_key)
        right_sizer.Add(self.chat_input, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        # 7) Attachments (with drag-and-drop)
        att_box = wx.StaticBox(right_panel,
                                label="Attachments (click Attach… or drop files anywhere on this dialog)")
        self._att_static_box = att_box
        self._att_sizer = wx.StaticBoxSizer(att_box, wx.HORIZONTAL)
        self._attach_btn = wx.Button(right_panel, label="Attach…")
        self._attach_btn.Bind(wx.EVT_BUTTON, self._on_attach)
        self._att_sizer.Add(self._attach_btn, 0, wx.ALL, 4)
        self._att_chips_parent = right_panel
        self._att_chips_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._att_sizer.Add(self._att_chips_sizer, 1, wx.EXPAND | wx.ALL, 2)
        right_sizer.Add(self._att_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        # 8) Send / Clear
        ai_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.send_btn = wx.Button(right_panel, label="Send")
        self.send_btn.SetBackgroundColour(wx.Colour(24, 143, 80))
        self.send_btn.SetForegroundColour(wx.WHITE)
        self.clear_btn = wx.Button(right_panel, label="Clear chat")
        self.send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        self.clear_btn.Bind(wx.EVT_BUTTON, self._on_clear_chat)
        ai_actions.Add(self.send_btn, 0, wx.RIGHT, 8)
        ai_actions.Add(self.clear_btn, 0)
        right_sizer.Add(ai_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.ai_status_label = wx.StaticText(right_panel, label="Ready.")
        right_sizer.Add(self.ai_status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        right_panel.SetSizer(right_sizer)

        # Enable drag-and-drop of files onto the dialog for attachments.
        try:
            drop_target = _AttachmentsDropTarget(self)
            self.SetDropTarget(drop_target)
        except Exception:
            pass

        splitter.SplitVertically(left_panel, right_panel, 340)
        ai_root.Add(splitter, 1, wx.EXPAND | wx.ALL, 4)
        ai_panel.SetSizer(ai_root)
        notebook.AddPage(ai_panel, "AI")

        # ---- Tab 1: AI Rules ----
        rules_panel = wx.Panel(notebook)
        rules_sizer = wx.BoxSizer(wx.VERTICAL)

        rules_intro = wx.StaticText(
            rules_panel,
            label=(
                "Add persistent instructions that are included in every AI request.\n"
                "Use this for project context, design standards, and review preferences."
            ),
        )
        rules_sizer.Add(rules_intro, 0, wx.ALL | wx.EXPAND, 8)

        loaded_rules = load_user_rules()
        self.ai_rules_ctrls: dict[str, wx.TextCtrl] = {}
        rules_grid = wx.FlexGridSizer(rows=2, cols=2, vgap=8, hgap=8)
        rules_grid.AddGrowableRow(0, 1)
        rules_grid.AddGrowableRow(1, 1)
        rules_grid.AddGrowableCol(0, 1)
        rules_grid.AddGrowableCol(1, 1)

        for key, label in RULE_SECTION_LABELS:
            section_box = wx.StaticBox(rules_panel, label=label)
            section_sizer = wx.StaticBoxSizer(section_box, wx.VERTICAL)
            ctrl = wx.TextCtrl(rules_panel, style=wx.TE_MULTILINE)
            ctrl.SetMinSize((-1, 140))
            ctrl.SetValue(loaded_rules.get(key, ""))
            section_sizer.Add(ctrl, 1, wx.ALL | wx.EXPAND, 4)
            rules_grid.Add(section_sizer, 1, wx.EXPAND)
            self.ai_rules_ctrls[key] = ctrl

        rules_sizer.Add(rules_grid, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        rules_actions = wx.BoxSizer(wx.HORIZONTAL)
        save_rules_btn = wx.Button(rules_panel, label="Save rules")
        save_rules_btn.Bind(wx.EVT_BUTTON, self._on_save_rules)
        rules_actions.Add(save_rules_btn, 0)
        rules_sizer.Add(rules_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.ai_rules_status_label = wx.StaticText(rules_panel, label="")
        rules_sizer.Add(self.ai_rules_status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        rules_panel.SetSizer(rules_sizer)
        notebook.AddPage(rules_panel, "AI Rules")

        # ---- Tab 2: Open windows ----
        open_win_panel = wx.Panel(notebook)
        open_win_sizer = wx.BoxSizer(wx.VERTICAL)

        open_win_intro = wx.StaticText(
            open_win_panel,
            label=(
                "wx top-level windows currently in this KiCad process — editors, viewers, and dialogs.\n"
                "Use Refresh to update the list after opening or closing a window."
            ),
        )
        open_win_sizer.Add(open_win_intro, 0, wx.ALL | wx.EXPAND, 8)

        ow_toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.open_windows_refresh_btn = wx.Button(open_win_panel, label="Refresh")
        self.open_windows_refresh_btn.Bind(wx.EVT_BUTTON, self._on_refresh_open_windows)
        ow_toolbar.Add(self.open_windows_refresh_btn, 0)
        ow_toolbar.AddStretchSpacer()
        open_win_sizer.Add(ow_toolbar, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.open_windows_text = wx.TextCtrl(
            open_win_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        mono = wx.Font(
            9,
            wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_NORMAL,
        )
        self.open_windows_text.SetFont(mono)
        open_win_sizer.Add(self.open_windows_text, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        open_win_panel.SetSizer(open_win_sizer)
        notebook.AddPage(open_win_panel, "Open Windows")

        # ---- Tab 3: Log ----
        log_panel = wx.Panel(notebook)
        log_sizer = wx.BoxSizer(wx.VERTICAL)

        log_intro = wx.StaticText(
            log_panel,
            label=(
                "Diagnostic log — shows PDF backend attempts, attachment processing, "
                "and each OpenAI request/response. Use this tab to verify your PDFs "
                "are being parsed correctly before sending."
            ),
        )
        log_sizer.Add(log_intro, 0, wx.ALL | wx.EXPAND, 8)

        log_toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.log_refresh_btn = wx.Button(log_panel, label="Refresh")
        self.log_clear_btn = wx.Button(log_panel, label="Clear")
        self.log_copy_btn = wx.Button(log_panel, label="Copy all")
        self.log_autoscroll = wx.CheckBox(log_panel, label="Auto-scroll")
        self.log_autoscroll.SetValue(True)
        self.log_refresh_btn.Bind(wx.EVT_BUTTON, self._on_log_refresh)
        self.log_clear_btn.Bind(wx.EVT_BUTTON, self._on_log_clear)
        self.log_copy_btn.Bind(wx.EVT_BUTTON, self._on_log_copy)
        log_toolbar.Add(self.log_refresh_btn, 0, wx.RIGHT, 6)
        log_toolbar.Add(self.log_clear_btn, 0, wx.RIGHT, 6)
        log_toolbar.Add(self.log_copy_btn, 0, wx.RIGHT, 6)
        log_toolbar.Add(self.log_autoscroll, 0, wx.ALIGN_CENTER_VERTICAL)
        log_toolbar.AddStretchSpacer()
        log_sizer.Add(log_toolbar, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.log_text = wx.TextCtrl(
            log_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.TE_DONTWRAP,
        )
        log_mono = wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
        )
        self.log_text.SetFont(log_mono)
        log_sizer.Add(self.log_text, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        log_panel.SetSizer(log_sizer)
        notebook.AddPage(log_panel, "Log")

        self._notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_notebook_page_changed)
        self._refresh_open_windows_list()
        self._refresh_log_text()
        # Subscribe to live log updates; wx.CallAfter ensures thread-safe append.
        self._log_listener = lambda entry: wx.CallAfter(self._on_log_entry_appended, entry)
        add_log_listener(self._log_listener)

        sizer.Add(notebook, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # (Bottom Close button hidden — use the window's title-bar close button.)

        self.SetSizer(sizer)
        self.SetMinSize((900, 600))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._post_init_state()

    def _refresh_open_windows_list(self):
        if hasattr(self, "open_windows_text") and self.open_windows_text:
            self.open_windows_text.SetValue(format_open_kicad_windows_report())

    def _on_refresh_open_windows(self, _evt):
        self._refresh_open_windows_list()

    def _on_notebook_page_changed(self, event):
        sel = event.GetSelection()
        if sel == self.TAB_OPEN_WINDOWS:
            self._refresh_open_windows_list()
        elif sel == self.TAB_LOG:
            self._refresh_log_text()
        event.Skip()

    # ---- Log tab ----

    def _refresh_log_text(self) -> None:
        if not hasattr(self, "log_text") or self.log_text is None:
            return
        try:
            self.log_text.SetValue(get_log_text())
            if getattr(self, "log_autoscroll", None) and self.log_autoscroll.GetValue():
                self.log_text.ShowPosition(self.log_text.GetLastPosition())
        except Exception:
            pass

    def _on_log_entry_appended(self, entry: str) -> None:
        """Called on the main thread with each new log entry (live append)."""
        if not hasattr(self, "log_text") or self.log_text is None:
            return
        try:
            self.log_text.AppendText(entry + "\n")
            if getattr(self, "log_autoscroll", None) and self.log_autoscroll.GetValue():
                self.log_text.ShowPosition(self.log_text.GetLastPosition())
        except Exception:
            pass

    def _on_log_refresh(self, _evt) -> None:
        self._refresh_log_text()

    def _on_log_clear(self, _evt) -> None:
        clear_log()
        self._refresh_log_text()

    def _on_log_copy(self, _evt) -> None:
        text = get_log_text()
        if not text:
            return
        try:
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(text))
                finally:
                    wx.TheClipboard.Close()
                self.ai_status_label.SetLabel("Log copied to clipboard.")
        except Exception:
            pass

    def _context_modules_from_ui(self) -> dict:
        return {
            "include_board_data": self.ctx_board_data.GetValue(),
            "include_f_cu_svg": self.ctx_f_cu_svg.GetValue(),
            "include_screenshot": self.ctx_screenshot.GetValue(),
            "force_vision": self.ctx_force_vision.GetValue()
                if hasattr(self, "ctx_force_vision") else False,
        }

    # ---- Post-construction init ----

    def _post_init_state(self) -> None:
        """Called after the dialog tree is built. Loads conversation list + picks current."""
        self._refresh_kicad_windows()
        self._refresh_chat_list()
        index = load_conversations_index()
        cur_id = index.get("current_id")
        conv = load_conversation(cur_id) if cur_id else None
        if conv is None:
            conv = new_conversation(DEFAULT_MODE_ID)
        self._load_conversation_into_ui(conv)
        self._refresh_chat_list()

    # ---- Mode + hint ----

    def _selected_mode(self) -> Mode:
        return get_mode(self._current_mode_id)

    def _update_mode_hint(self) -> None:
        hint = self._selected_mode().hint
        self._mode_hint.SetLabel(hint)
        try:
            parent_width = self._mode_hint.GetParent().GetClientSize().width
            wrap_w = max(parent_width - 220, 300)
            self._mode_hint.Wrap(wrap_w)
        except Exception:
            pass
        self._mode_hint.GetParent().Layout()

    def _set_mode(self, mode_id: str, persist: bool = True) -> None:
        mode_id = mode_id if mode_id in MODES else DEFAULT_MODE_ID
        self._current_mode_id = mode_id
        if mode_id in self._mode_ids_in_choice:
            self._mode_choice.SetSelection(self._mode_ids_in_choice.index(mode_id))
        self._update_mode_hint()
        if persist and self._current_conv is not None:
            self._current_conv["mode"] = mode_id
            touch_conversation(self._current_conv)
            save_conversation(self._current_conv)
            self._refresh_chat_list()

    def _on_mode_changed(self, _evt):
        idx = self._mode_choice.GetSelection()
        if idx < 0 or idx >= len(self._mode_ids_in_choice):
            return
        self._set_mode(self._mode_ids_in_choice[idx])

    # ---- Window select ----

    def _refresh_kicad_windows(self) -> None:
        """Populate the window dropdown with currently-open KiCad editor windows."""
        self._kicad_windows: list[dict] = enumerate_kicad_editor_windows()
        labels = [f"{w['editor']}  —  {w['title']}" for w in self._kicad_windows]
        if not labels:
            labels = ["(no KiCad editor windows found)"]
        self._window_choice.Clear()
        self._window_choice.AppendItems(labels)
        self._window_choice.SetSelection(0)

    def _on_refresh_windows(self, _evt) -> None:
        self._refresh_kicad_windows()

    def _on_window_choice(self, _evt) -> None:
        # Selection is read on-demand by _selected_window(); nothing to persist.
        pass

    def _selected_window(self):
        wins = getattr(self, "_kicad_windows", []) or []
        idx = self._window_choice.GetSelection()
        if idx < 0 or idx >= len(wins):
            return None
        try:
            w = wins[idx].get("window")
            return w if w else None
        except Exception:
            return None

    # ---- Conversation list ----

    def _refresh_chat_list(self) -> None:
        self._chat_list.DeleteAllItems()
        index = load_conversations_index()
        convs = list(index.get("conversations", {}).values())
        convs.sort(key=lambda c: c.get("last_updated", ""), reverse=True)
        self._chat_list_ids: list[str] = []
        for c in convs:
            row = self._chat_list.InsertItem(self._chat_list.GetItemCount(), c.get("title", "Untitled"))
            mode = c.get("mode", "")
            mode_label = MODES[mode].label if mode in MODES else mode
            self._chat_list.SetItem(row, 1, mode_label)
            self._chat_list.SetItem(row, 2, c.get("last_updated", ""))
            self._chat_list_ids.append(c.get("id", ""))
            if self._current_conv and c.get("id") == self._current_conv.get("id"):
                self._chat_list.Select(row)
                self._chat_list.Focus(row)

    def _on_chat_selected(self, evt):
        row = evt.GetIndex()
        if row < 0 or row >= len(self._chat_list_ids):
            return
        conv_id = self._chat_list_ids[row]
        if self._current_conv and conv_id == self._current_conv.get("id"):
            return
        conv = load_conversation(conv_id)
        if conv is None:
            return
        self._load_conversation_into_ui(conv)

    def _on_chat_context_menu(self, evt):
        row = evt.GetIndex()
        if row < 0 or row >= len(self._chat_list_ids):
            return
        conv_id = self._chat_list_ids[row]

        menu = wx.Menu()
        rename_item = menu.Append(wx.ID_ANY, "Rename…")
        delete_item = menu.Append(wx.ID_ANY, "Delete")

        def on_rename(_e, cid=conv_id):
            current = load_conversation(cid) or {}
            dlg = wx.TextEntryDialog(
                self, "New chat title:", "Rename chat",
                value=current.get("title", "New chat"),
            )
            try:
                if dlg.ShowModal() == wx.ID_OK:
                    new_title = (dlg.GetValue() or "").strip() or "New chat"
                    rename_conversation(cid, new_title)
                    if self._current_conv and self._current_conv.get("id") == cid:
                        self._current_conv["title"] = new_title
                    self._refresh_chat_list()
            finally:
                dlg.Destroy()

        def on_delete(_e, cid=conv_id):
            if wx.MessageBox(
                "Delete this chat and its attachments? This cannot be undone.",
                "Delete chat", wx.YES_NO | wx.ICON_WARNING,
            ) != wx.YES:
                return
            delete_conversation(cid)
            if self._current_conv and self._current_conv.get("id") == cid:
                self._current_conv = None
                new_conv = new_conversation(self._current_mode_id)
                self._load_conversation_into_ui(new_conv)
            self._refresh_chat_list()

        self.Bind(wx.EVT_MENU, on_rename, rename_item)
        self.Bind(wx.EVT_MENU, on_delete, delete_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_new_chat(self, _evt):
        conv = new_conversation(self._current_mode_id)
        self._load_conversation_into_ui(conv)
        self._refresh_chat_list()

    def _load_conversation_into_ui(self, conv: dict) -> None:
        self._current_conv = conv
        self._chat_history = copy.deepcopy(conv.get("messages", []))
        self._set_mode(conv.get("mode", DEFAULT_MODE_ID), persist=False)
        self.chat_display.SetValue("")
        for msg in self._chat_history:
            role = msg.get("role", "user")
            if role == "user":
                self._append_user_turn_to_display(msg)
            elif role == "assistant":
                self._append_assistant_turn_to_display(msg)
        self._clear_pending_attachments_ui()
        self._rebuild_action_cards()
        index = load_conversations_index()
        index["current_id"] = conv.get("id")
        save_conversations_index(index)
        self.ai_status_label.SetLabel("Loaded chat.")

    # ---- Chat input / display ----

    def _on_chat_input_key(self, event):
        if event.GetKeyCode() == wx.WXK_RETURN and not event.ShiftDown():
            self._on_send(None)
            return
        event.Skip()

    _TURN_RULE = "=" * 72

    def _append_raw(self, label: str, text: str) -> None:
        leading = "\n\n" if self.chat_display.GetValue() else ""
        header = (
            f"{leading}{self._TURN_RULE}\n"
            f"  {label.upper()}\n"
            f"{self._TURN_RULE}\n"
        )
        self.chat_display.AppendText(header + f"{text}\n")

    def _append_user_turn_to_display(self, msg: dict) -> None:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(t for t in text_parts if t)
        atts = msg.get("attachments") or []
        if atts:
            att_names = ", ".join(a.get("filename", "?") for a in atts)
            content = (content + f"\n[attached: {att_names}]").strip()
        self._append_raw("You", content or "(empty)")

    def _append_assistant_turn_to_display(self, msg: dict) -> None:
        text = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            lines = [f"(proposed {len(tool_calls)} action(s))"]
            for tc in tool_calls:
                status = tc.get("status", "pending")
                lines.append(f"  - [{status}] {tc.get('name','')}({json.dumps(tc.get('args', {}), ensure_ascii=False)})")
                res = tc.get("result") or {}
                if res:
                    lines.append(f"      -> {res.get('message','')}")
            text = (text + "\n\n" + "\n".join(lines)).strip()
        self._append_raw("AI", text or "(no text)")

    def collect_rows(self):
        return _collect_component_rows(self.board)

    def _get_user_rules(self) -> dict[str, str]:
        if hasattr(self, "ai_rules_ctrls") and self.ai_rules_ctrls:
            return {
                key: ctrl.GetValue()
                for key, ctrl in self.ai_rules_ctrls.items()
            }
        return load_user_rules()

    def _on_save_rules(self, _evt):
        rules = self._get_user_rules()
        ok, err = save_user_rules(rules)
        if ok:
            self.ai_rules_status_label.SetLabel("Rules saved.")
            wx.MessageBox("AI rules saved.", "AI Rules", wx.OK | wx.ICON_INFORMATION)
        else:
            self.ai_rules_status_label.SetLabel("")
            wx.MessageBox(f"Failed to save rules: {err}", "AI Rules", wx.OK | wx.ICON_WARNING)

    # ---- Attachments ----

    def _on_attach(self, _evt):
        if self._current_conv is None:
            return
        wildcard = (
            "All supported|*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp;*.pdf;*.txt;*.md;*.log;*.csv;"
            "*.json;*.xml;*.yaml;*.yml;*.net;*.kicad_sym;*.kicad_sch;*.kicad_pcb;*.kicad_mod;*.kicad_pro|"
            "Images|*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp|"
            "PDF|*.pdf|"
            "Text / KiCad|*.txt;*.md;*.net;*.kicad_sym;*.kicad_sch;*.kicad_pcb;*.kicad_mod;*.kicad_pro|"
            "All files|*.*"
        )
        dlg = wx.FileDialog(
            self, "Attach files", wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            paths = dlg.GetPaths()
        finally:
            dlg.Destroy()
        self.handle_dropped_files(list(paths))

    def handle_dropped_files(self, paths: list[str]) -> None:
        """Ingest a list of file paths (from FileDialog or drag-and-drop) as attachments."""
        if self._current_conv is None:
            return
        if not paths:
            return
        failures = []
        added = 0
        pdf_added = False
        for p in paths:
            att = ingest_attachment(self._current_conv["id"], p)
            if att is None:
                failures.append(os.path.basename(p))
                continue
            self._pending_attachments.append(att)
            added += 1
            if att.get("kind") == "pdf":
                pdf_added = True
        self._rebuild_attachment_chips()
        if added:
            msg = f"Attached {added} file(s)."
            if pdf_added:
                backends = _detect_pdf_backends()
                kicad_ai_log(f"PDF backend scan: {backends}")
                if backends["pypdfium2"] or backends["fitz"]:
                    bk = "pypdfium2" if backends["pypdfium2"] else "fitz"
                    msg += f" PDF will be rasterized via {bk}."
                elif any(backends[k] for k in ("pypdf", "PyPDF2", "pdfminer.six")):
                    avail = next(k for k in ("pypdf", "PyPDF2", "pdfminer.six") if backends[k])
                    msg += (
                        f" PDF rasterization unavailable — will fall back to "
                        f"text-only extraction via {avail} "
                        "(figures/package drawings won't be seen)."
                    )
                else:
                    msg += (
                        " PDF cannot be processed — install `pypdfium2` (preferred) or `pypdf`."
                    )
            self.ai_status_label.SetLabel(msg)
        if failures:
            wx.MessageBox(
                "Could not attach:\n- " + "\n- ".join(failures),
                "KiCad AI", wx.OK | wx.ICON_WARNING,
            )

    def _clear_pending_attachments_ui(self) -> None:
        self._pending_attachments = []
        self._rebuild_attachment_chips()

    def _rebuild_attachment_chips(self) -> None:
        self._att_chips_sizer.Clear(True)
        if not self._pending_attachments:
            placeholder = wx.StaticText(self._att_chips_parent, label="(none — click Attach…)")
            self._att_chips_sizer.Add(placeholder, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        else:
            for att in self._pending_attachments:
                chip = self._build_chip(att)
                self._att_chips_sizer.Add(chip, 0, wx.ALL, 2)
        self._att_chips_parent.Layout()

    def _build_chip(self, att: dict) -> wx.Panel:
        chip = wx.Panel(self._att_chips_parent, style=wx.BORDER_SIMPLE)
        s = wx.BoxSizer(wx.HORIZONTAL)
        icon = {"image": "[img]", "pdf": "[pdf]", "text": "[txt]"}.get(att.get("kind", ""), "[?]")
        label = wx.StaticText(chip, label=f"{icon} {att.get('filename','?')}")
        s.Add(label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        remove_btn = wx.Button(chip, label="×", size=(22, 22), style=wx.BU_EXACTFIT)
        remove_btn.Bind(wx.EVT_BUTTON, lambda _e, aid=att.get("id"): self._on_remove_attachment(aid))
        s.Add(remove_btn, 0, wx.ALL, 2)
        chip.SetSizer(s)
        chip.Layout()
        return chip

    def _on_remove_attachment(self, att_id: Optional[str]) -> None:
        if not att_id:
            return
        before = len(self._pending_attachments)
        self._pending_attachments = [a for a in self._pending_attachments if a.get("id") != att_id]
        if len(self._pending_attachments) != before:
            self._rebuild_attachment_chips()

    # ---- Action cards ----

    def _rebuild_action_cards(self) -> None:
        self._actions_sizer.Clear(True)
        self._action_buttons_by_call = {}
        pending = []
        for msg in self._chat_history:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("status", "pending") == "pending":
                    pending.append((msg, tc))
        if not pending:
            lbl = wx.StaticText(self._actions_parent, label="(no pending actions)")
            self._actions_sizer.Add(lbl, 0, wx.ALL, 4)
        else:
            header = wx.BoxSizer(wx.HORIZONTAL)
            apply_all_btn = wx.Button(self._actions_parent, label="Apply all")
            apply_all_btn.Bind(wx.EVT_BUTTON, self._on_apply_all)
            header.AddStretchSpacer()
            header.Add(apply_all_btn, 0, wx.ALL, 4)
            self._actions_sizer.Add(header, 0, wx.EXPAND)
            for _msg, tc in pending:
                card = self._build_action_card(tc)
                self._actions_sizer.Add(card, 0, wx.ALL | wx.EXPAND, 3)
        self._actions_parent.Layout()

    def _build_action_card(self, tc: dict) -> wx.Panel:
        card = wx.Panel(self._actions_parent, style=wx.BORDER_SIMPLE)
        s = wx.BoxSizer(wx.VERTICAL)
        head = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(card, label=tc.get("name", ""))
        tf = title.GetFont()
        tf.MakeBold()
        title.SetFont(tf)
        head.Add(title, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        apply_btn = wx.Button(card, label="Apply", size=(70, 26))
        discard_btn = wx.Button(card, label="Discard", size=(70, 26))
        apply_btn.Bind(wx.EVT_BUTTON, lambda _e, cid=tc.get("id"): self._on_apply_action(cid))
        discard_btn.Bind(wx.EVT_BUTTON, lambda _e, cid=tc.get("id"): self._on_discard_action(cid))
        head.Add(apply_btn, 0, wx.ALL, 2)
        head.Add(discard_btn, 0, wx.ALL, 2)
        s.Add(head, 0, wx.EXPAND)

        args_str = json.dumps(tc.get("args", {}), indent=2, ensure_ascii=False)
        args_txt = wx.TextCtrl(card, value=args_str,
                                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        mono = wx.Font(8, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        args_txt.SetFont(mono)
        args_txt.SetMinSize((-1, min(120, 20 + 14 * (args_str.count("\n") + 1))))
        s.Add(args_txt, 0, wx.EXPAND | wx.ALL, 3)
        card.SetSizer(s)
        self._action_buttons_by_call[tc.get("id", "")] = {
            "apply": apply_btn, "discard": discard_btn, "card": card,
        }
        return card

    def _find_tool_call(self, call_id: str) -> tuple[Optional[dict], Optional[dict]]:
        for msg in self._chat_history:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("id") == call_id:
                    return msg, tc
        return None, None

    def _confirm_write_dialog(self, tc: dict) -> bool:
        """For file-writing tools, show confirmation with target path preview."""
        name = tc.get("name", "")
        if name not in ("create_symbol", "create_footprint"):
            return True
        args = tc.get("args", {})
        fp_name = args.get("name", "unnamed")
        kind_label = "symbol" if name == "create_symbol" else "footprint"
        msg = (
            f"Write {kind_label} '{fp_name}' to the project directory?\n\n"
            "You can review the generated file afterward."
        )
        return wx.MessageBox(msg, "Confirm write", wx.YES_NO | wx.ICON_QUESTION) == wx.YES

    def _on_apply_action(self, call_id: Optional[str]) -> None:
        if not call_id:
            return
        msg, tc = self._find_tool_call(call_id)
        if tc is None:
            return
        if tc.get("status") != "pending":
            return
        if not self._confirm_write_dialog(tc):
            return
        conv = self._current_conv or {}
        mode = get_mode(conv.get("mode", DEFAULT_MODE_ID))
        project_path = ""
        try:
            project_path = self.board.GetFileName() if hasattr(self.board, "GetFileName") else ""
        except Exception:
            pass
        try:
            result = mode.execute_tool(tc.get("name", ""), tc.get("args", {}) or {},
                                        self.board, project_path)
        except Exception as exc:
            result = {"ok": False, "message": f"Exception: {exc}"}
        tc["status"] = "applied" if result.get("ok") else "pending"
        tc["result"] = result
        if not result.get("ok"):
            wx.MessageBox(result.get("message", "Action failed."),
                           "KiCad AI", wx.OK | wx.ICON_WARNING)
        self._persist_current_conversation()
        self._rerender_display()
        self._rebuild_action_cards()

    def _on_discard_action(self, call_id: Optional[str]) -> None:
        if not call_id:
            return
        msg, tc = self._find_tool_call(call_id)
        if tc is None:
            return
        tc["status"] = "discarded"
        tc["result"] = {"ok": False, "message": "discarded by user"}
        self._persist_current_conversation()
        self._rerender_display()
        self._rebuild_action_cards()

    def _on_apply_all(self, _evt) -> None:
        pending_ids = []
        for msg in self._chat_history:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("status", "pending") == "pending":
                    pending_ids.append(tc.get("id"))
        for cid in pending_ids:
            self._on_apply_action(cid)

    def _rerender_display(self) -> None:
        self.chat_display.SetValue("")
        for m in self._chat_history:
            if m.get("role") == "user":
                self._append_user_turn_to_display(m)
            elif m.get("role") == "assistant":
                self._append_assistant_turn_to_display(m)

    # ---- Send ----

    def _persist_current_conversation(self) -> None:
        if self._current_conv is None:
            return
        self._current_conv["messages"] = copy.deepcopy(self._chat_history)
        self._current_conv["mode"] = self._current_mode_id
        # Dedup attachments registry across messages
        reg: dict = {}
        for m in self._chat_history:
            for a in m.get("attachments", []) or []:
                if a.get("id"):
                    reg[a["id"]] = a
        self._current_conv["attachments"] = reg
        touch_conversation(self._current_conv)
        save_conversation(self._current_conv)

    def _on_send(self, _evt):
        if self._current_conv is None:
            self._current_conv = new_conversation(self._current_mode_id)
            self._chat_history = []
        user_text = self.chat_input.GetValue().strip()
        if not user_text and not self._pending_attachments:
            return

        attachments_for_msg = list(self._pending_attachments)
        user_msg: dict = {"role": "user", "content": user_text}
        if attachments_for_msg:
            user_msg["attachments"] = [
                {"id": a["id"], "filename": a["filename"], "kind": a["kind"],
                 "path": a["path"], "size": a.get("size", 0)}
                for a in attachments_for_msg
            ]
        self._chat_history.append(user_msg)
        self._append_user_turn_to_display(user_msg)
        self.chat_input.SetValue("")
        self._pending_attachments = []
        self._rebuild_attachment_chips()
        self.ai_status_label.SetLabel("Thinking...")
        self.send_btn.Enable(False)

        # Auto-title on first turn
        if self._current_conv.get("title", "New chat") == "New chat" and user_text:
            self._current_conv["title"] = auto_title_from_first_message(user_text)

        mode = self._selected_mode()

        try:
            wx.BeginBusyCursor()
            user_rules = self._get_user_rules()
            text, tool_calls = fetch_ai_chat_response(
                self._chat_history,
                user_rules,
                self.board,
                mode,
                attachments=attachments_for_msg,
                context_modules=self._context_modules_from_ui(),
                screenshot_window=self._selected_window(),
            )
        except ValueError as exc:
            self.ai_status_label.SetLabel("Error.")
            wx.MessageBox(str(exc), "KiCad AI", wx.OK | wx.ICON_WARNING)
            self._chat_history.pop()
            return
        finally:
            if wx.IsBusy():
                wx.EndBusyCursor()
            self.send_btn.Enable(True)

        assistant_msg = {"role": "assistant", "content": text, "tool_calls": tool_calls}
        self._chat_history.append(assistant_msg)
        self._append_assistant_turn_to_display(assistant_msg)
        self._persist_current_conversation()
        self._refresh_chat_list()
        self._rebuild_action_cards()
        self.ai_status_label.SetLabel(
            "Ready." if not tool_calls else f"Ready. {len(tool_calls)} proposed action(s) below."
        )

    def _on_clear_chat(self, _evt):
        if wx.MessageBox(
            "Clear all messages in this chat? The chat itself stays in the sidebar.",
            "Clear chat", wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return
        self._chat_history.clear()
        self.chat_display.SetValue("")
        self.chat_input.SetValue("")
        self._pending_attachments = []
        self._rebuild_attachment_chips()
        self._rebuild_action_cards()
        self._persist_current_conversation()
        self._refresh_chat_list()
        self.ai_status_label.SetLabel("Chat cleared.")

    # ---- Lifecycle ----

    def _on_close(self, event):
        global _ACTIVE_DIALOG
        if _ACTIVE_DIALOG is self:
            _ACTIVE_DIALOG = None
        try:
            listener = getattr(self, "_log_listener", None)
            if listener is not None:
                remove_log_listener(listener)
        except Exception:
            pass
        self.Destroy()


# ===========================================================================
# Plugin registration
# ===========================================================================

class KiCadAIPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiCad AI Assistant"
        self.category = "AI"
        self.description = "General-purpose AI assistant for KiCad PCB design."
        self.show_toolbar_button = True
        _plugin_dir = os.path.dirname(os.path.abspath(__file__))
        _icon = os.path.join(_plugin_dir, "kicad-ai-icon.png")
        if os.path.isfile(_icon):
            self.icon_file_name = _icon
            self.dark_icon_file_name = _icon

    def Run(self):
        global _ACTIVE_DIALOG
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox(
                "Open a board in PCB Editor first.",
                "KiCad AI",
                wx.OK | wx.ICON_WARNING,
            )
            return

        if _ACTIVE_DIALOG is not None and _ACTIVE_DIALOG:
            try:
                if _ACTIVE_DIALOG.IsShown():
                    _ACTIVE_DIALOG.board = board
                    _ACTIVE_DIALOG.Raise()
                    return
            except Exception:
                _ACTIVE_DIALOG = None

        parent = None
        try:
            app = wx.GetApp()
            if app is not None:
                parent = app.GetTopWindow()
        except Exception:
            parent = None

        dlg = KiCadAIDialog(parent, board)
        _ACTIVE_DIALOG = dlg
        dlg.Show()


KiCadAIPlugin().register()
