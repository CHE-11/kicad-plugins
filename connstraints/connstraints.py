# connstraints.py
# KiCAD Plugin: footprint bounding boxes + relative placement constraints
# (e.g. keep SW11 5 mm to the right of SW12 — edge-to-edge gap).


import base64
import io
import json
import os
import re
import shutil
import tempfile
from typing import Optional, Union
import urllib.error
import urllib.request

import wx  # type: ignore
import wx.grid as gridlib  # type: ignore
import pcbnew  # type: ignore

_ACTIVE_DIALOG = None


def mm(iu: int) -> float:
    return float(pcbnew.ToMM(iu))


def natural_sort_key(value: str):
    parts = re.split(r"(\d+)", (value or "").strip())
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def board_constraints_path(board) -> Optional[str]:
    path = board.GetFileName() if hasattr(board, "GetFileName") else ""
    if not path:
        return None
    base, _ = os.path.splitext(path)
    return f"{base}.constraints.json"


# Anchor column: use a component name from the table or board outline tokens:
#   EDGE_CUT, EDGE CUT, BOARD, BOARD OUTLINE (case/spacing insensitive)
BOARD_OUTLINE_ANCHOR_DISPLAY = "EDGE_CUT"


class _CenterLineBBox:
    """Lightweight bbox that mirrors the KiCad BOX2I read API."""
    def __init__(self, left, top, right, bottom):
        self._left = min(int(left), int(right))
        self._right = max(int(left), int(right))
        self._top = min(int(top), int(bottom))
        self._bottom = max(int(top), int(bottom))
    def GetLeft(self): return self._left
    def GetRight(self): return self._right
    def GetTop(self): return self._top
    def GetBottom(self): return self._bottom
    def GetWidth(self): return self._right - self._left
    def GetHeight(self): return self._bottom - self._top


def _item_stroke_width(item) -> int:
    try:
        if hasattr(item, "GetWidth"):
            w = item.GetWidth()
            if isinstance(w, (int, float)) and w > 0:
                return int(w)
        if hasattr(item, "GetStroke"):
            stroke = item.GetStroke()
            if hasattr(stroke, "GetWidth"):
                w = stroke.GetWidth()
                if isinstance(w, (int, float)) and w > 0:
                    return int(w)
    except Exception:
        pass
    return 0


def board_outline_bbox(board):
    """Bounding box of Edge.Cuts center lines (line-width stripped), or None."""
    try:
        if hasattr(board, "UpdateBoardOutline"):
            board.UpdateBoardOutline()
    except Exception:
        pass

    min_x: int = 0
    max_x: int = 0
    min_y: int = 0
    max_y: int = 0
    found = False
    try:
        edge_layer = pcbnew.Edge_Cuts
        for item in board.GetDrawings():
            try:
                if item.GetLayer() != edge_layer:
                    continue
                ib = item.GetBoundingBox()
                half_w = _item_stroke_width(item) // 2
                left = ib.GetLeft() + half_w
                right = ib.GetRight() - half_w
                top = ib.GetTop() + half_w
                bottom = ib.GetBottom() - half_w
                if right < left:
                    left = right = (left + right) // 2
                if bottom < top:
                    top = bottom = (top + bottom) // 2
                if not found:
                    min_x, max_x, min_y, max_y = left, right, top, bottom
                    found = True
                else:
                    min_x = min(min_x, left)
                    max_x = max(max_x, right)
                    min_y = min(min_y, top)
                    max_y = max(max_y, bottom)
            except Exception:
                continue
    except Exception:
        pass

    if found:
        return _CenterLineBBox(min_x, min_y, max_x, max_y)

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

    try:
        if hasattr(board, "ComputeBoundingBox"):
            bb2 = board.ComputeBoundingBox(True)
            if _nonempty(bb2):
                return bb2
    except Exception:
        pass

    return None


def is_board_outline_anchor(ref: str) -> bool:
    r = " ".join((ref or "").strip().split()).upper()
    return r in (
        "EDGE_CUT",
        "EDGECUT",
        "EDGE CUT",
        "BOARD",
        "BOARD OUTLINE",
        "OUTLINE",
        "BOARD_OUTLINE",
    )


def edge_cut_row(board):
    """One synthetic table row for the Edge.Cuts bounding box (for visibility & copy-paste anchor name)."""
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
            "n/a",
        ],
    }


def footprint_item_id(fp) -> str:
    """Stable KiCad object id (UUID string) for a footprint, if available."""
    if fp is None:
        return ""

    def _clean_uuid_text(value) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        if text.startswith("<pcbnew.") and "proxy of <Swig Object" in text:
            return ""
        return text

    try:
        u = fp.GetUuid()
        if u is not None:
            if hasattr(u, "AsStdString"):
                text = _clean_uuid_text(u.AsStdString())
                if text:
                    return text
            if hasattr(u, "AsString"):
                text = _clean_uuid_text(u.AsString())
                if text:
                    return text
            if hasattr(u, "AsLegacyTimestampString"):
                text = _clean_uuid_text(u.AsLegacyTimestampString())
                if text:
                    return text
            text = _clean_uuid_text(u)
            if text:
                return text
    except Exception:
        pass
    try:
        mu = fp.m_Uuid
        if hasattr(mu, "AsStdString"):
            text = _clean_uuid_text(mu.AsStdString())
            if text:
                return text
        if hasattr(mu, "AsString"):
            text = _clean_uuid_text(mu.AsString())
            if text:
                return text
        if hasattr(mu, "AsLegacyTimestampString"):
            text = _clean_uuid_text(mu.AsLegacyTimestampString())
            if text:
                return text
        text = _clean_uuid_text(mu)
        if text:
            return text
    except Exception:
        pass
    return ""


def footprint_name(fp) -> str:
    ref = fp.GetReference() if hasattr(fp, "GetReference") else ""
    val = fp.GetValue() if hasattr(fp, "GetValue") else ""
    if ref and val:
        return f"{ref} ({val})"
    return ref or val or "Footprint"


def footprint_bbox(fp):
    try:
        return fp.GetBoundingBox(False, False)
    except Exception:
        return fp.GetBoundingBox()


def footprint_is_locked(fp) -> bool:
    if fp is None:
        return False

    for method_name in ("IsLocked", "GetLocked", "IsPlacementLocked"):
        if not hasattr(fp, method_name):
            continue
        try:
            return bool(getattr(fp, method_name)())
        except Exception:
            continue

    for attr_name in ("m_Locked",):
        if not hasattr(fp, attr_name):
            continue
        try:
            value = getattr(fp, attr_name)
            return bool(value() if callable(value) else value)
        except Exception:
            continue

    return False


def footprint_row(fp):
    bbox = footprint_bbox(fp)

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
            "Yes" if footprint_is_locked(fp) else "No",
        ],
    }


def sort_rows_top_to_bottom_then_left_to_right(rows, x_bucket_mm=0.05, y_bucket_mm=0.05):
    """
    Book order: top-to-bottom, then left-to-right.
    Uses component centers so tall/wide footprints sort by where they visually sit.
    """
    x_bucket_iu = pcbnew.FromMM(x_bucket_mm)
    y_bucket_iu = pcbnew.FromMM(y_bucket_mm)

    def key(r):
        center_x, center_y = r["center"]
        y_bucket = int(round(center_y / y_bucket_iu)) if y_bucket_iu else center_y
        x_bucket = int(round(center_x / x_bucket_iu)) if x_bucket_iu else center_x
        return (y_bucket, x_bucket, r["ref"])

    return sorted(rows, key=key)


# --- Constraint model ---

RELATION_IDS = (
    "RIGHT_OF",
    "LEFT_OF",
    "ALIGN_HORIZONTALLY",
    "ABOVE",
    "BELOW",
    "ALIGN_VERTICALLY",
)
RELATION_LABELS = {
    "RIGHT_OF": "Right of (→ gap)",
    "LEFT_OF": "Left of (← gap)",
    "ALIGN_HORIZONTALLY": "Aligned horizontally (same X, gap=0)",
    "ABOVE": "Above (↑ gap)",
    "BELOW": "Below (↓ gap)",
    "ALIGN_VERTICALLY": "Aligned vertically (same Y, gap=0)",
}
LABEL_TO_ID = {v: k for k, v in RELATION_LABELS.items()}
EDGE_POINT_LABELS = (
    "Top",
    "Right",
    "Bottom",
    "Left",
    "Center",
)

_HORIZONTAL_EDGES = ("Left", "Right")
_VERTICAL_EDGES = ("Top", "Bottom")
_ALL_EDGES = ("Left", "Right", "Top", "Bottom", "Center")
_HORIZONTAL_RELATIONS = ("RIGHT_OF", "LEFT_OF", "ALIGN_HORIZONTALLY")
_VERTICAL_RELATIONS = ("ABOVE", "BELOW", "ALIGN_VERTICALLY")

EDGE_DESCRIPTION_LABELS = {
    "Top": "Top edge",
    "Right": "Right edge",
    "Bottom": "Bottom edge",
    "Left": "Left edge",
    "Center": "Center",
    "TopLeft": "Top Left corner",
    "TopRight": "Top Right corner",
    "BottomRight": "Bottom Right corner",
    "BottomLeft": "Bottom Left corner",
}

RELATION_DESCRIPTION_LABELS = {
    "RIGHT_OF": "to the right of",
    "LEFT_OF": "to the left of",
    "ALIGN_HORIZONTALLY": "horizontally aligned with",
    "ABOVE": "above",
    "BELOW": "below",
    "ALIGN_VERTICALLY": "vertically aligned with",
}

PICK_STAGE_IDLE = "idle"
PICK_STAGE_CONSTRAINED = "pick_constrained"
PICK_STAGE_ANCHOR = "pick_anchor"
PICK_STAGE_VIEW = "pick_view"

CONSTRAINT_FILTER_MODE_COMPONENTS_SELECTION = "components_selection"
CONSTRAINT_FILTER_MODE_PCB_SELECTION = "pcb_selection"


class ConstraintDetailsDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="New constraint", style=wx.DEFAULT_DIALOG_STYLE)

        sizer = wx.BoxSizer(wx.VERTICAL)
        form = wx.FlexGridSizer(2, 2, 8, 8)
        form.AddGrowableCol(1, 1)

        form.Add(wx.StaticText(self, label="Relation"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.relation_choice = wx.Choice(
            self, choices=[RELATION_LABELS[key] for key in RELATION_IDS]
        )
        self.relation_choice.SetSelection(0)
        form.Add(self.relation_choice, 1, wx.EXPAND)

        form.Add(wx.StaticText(self, label="Distance (mm)"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.distance_ctrl = wx.TextCtrl(self, value="")
        form.Add(self.distance_ctrl, 1, wx.EXPAND)

        sizer.Add(form, 1, wx.ALL | wx.EXPAND, 12)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        self.SetSizerAndFit(sizer)
        self.CentreOnParent()

    def get_values(self):
        relation_label = self.relation_choice.GetStringSelection()
        relation_id = LABEL_TO_ID.get(relation_label, "RIGHT_OF")
        return relation_id, self.distance_ctrl.GetValue().strip()


def _bbox_x_edges(bbox):
    left = bbox.GetLeft()
    right = bbox.GetRight()
    return (min(left, right), max(left, right))


def _bbox_y_edges(bbox):
    top = bbox.GetTop()
    bottom = bbox.GetBottom()
    return (min(top, bottom), max(top, bottom))


def _bbox_center(bbox):
    left, right = _bbox_x_edges(bbox)
    top, bottom = _bbox_y_edges(bbox)
    return (left + right) // 2, (top + bottom) // 2


def _bbox_edge_point(bbox, edge_name: str):
    left, right = _bbox_x_edges(bbox)
    top, bottom = _bbox_y_edges(bbox)
    center_x, center_y = _bbox_center(bbox)

    points = {
        "topleft": (left, top),
        "top": (center_x, top),
        "topright": (right, top),
        "right": (right, center_y),
        "bottomright": (right, bottom),
        "bottom": (center_x, bottom),
        "bottomleft": (left, bottom),
        "left": (left, center_y),
        "center": (center_x, center_y),
    }
    return points.get((edge_name or "").strip().lower(), (center_x, center_y))


def _edge_movement_axis(edge_name: str):
    if edge_name in ("Left", "Right"):
        return "x"
    if edge_name in ("Top", "Bottom"):
        return "y"
    return "both"


def _fp_by_ref(board, ref: str):
    ref = (ref or "").strip()
    if not ref:
        return None
    for fp in board.Footprints():
        r = fp.GetReference() if hasattr(fp, "GetReference") else ""
        if r == ref:
            return fp
    return None


def _fp_by_display_name(board, name: str):
    name = (name or "").strip()
    if not name:
        return None
    for fp in board.Footprints():
        if footprint_name(fp) == name:
            return fp
    return None


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


def _move_fp_by_delta(fp, dx: int, dy: int):
    if dx == 0 and dy == 0:
        return
    if hasattr(fp, "Move"):
        fp.Move(pcbnew.VECTOR2I(int(dx), int(dy)))
        return
    pos = fp.GetPosition()
    nx, ny = vector2i_xy(pos)
    nx += int(dx)
    ny += int(dy)
    fp.SetPosition(pcbnew.VECTOR2I(nx, ny))


def _relation_axis(relation: str) -> str:
    if relation in ("RIGHT_OF", "LEFT_OF", "ALIGN_HORIZONTALLY"):
        return "x"
    if relation in ("ABOVE", "BELOW", "ALIGN_VERTICALLY"):
        return "y"
    return "?"


def _measure_constraint_error(target_x: int, target_y: int, actual_x: int, actual_y: int, axis: str) -> tuple[int, int]:
    axis_error = abs(actual_x - target_x) if axis == "x" else abs(actual_y - target_y)
    cross_axis_offset = abs(actual_y - target_y) if axis == "x" else abs(actual_x - target_x)
    return axis_error, cross_axis_offset


def _flip_relation_id(relation_id: str) -> str:
    return {
        "RIGHT_OF": "LEFT_OF",
        "LEFT_OF": "RIGHT_OF",
        "ABOVE": "BELOW",
        "BELOW": "ABOVE",
        "ALIGN_HORIZONTALLY": "ALIGN_HORIZONTALLY",
        "ALIGN_VERTICALLY": "ALIGN_VERTICALLY",
    }.get((relation_id or "").strip(), relation_id or "RIGHT_OF")


def _constraint_node_key(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    if is_board_outline_anchor(text):
        return BOARD_OUTLINE_ANCHOR_DISPLAY.casefold()
    return text.casefold()


def _locked_footprint_node_keys(board) -> set[str]:
    locked: set[str] = set()
    try:
        for fp in board.Footprints():
            if not footprint_is_locked(fp):
                continue
            key = _constraint_node_key(footprint_name(fp))
            if key:
                locked.add(key)
    except Exception:
        pass
    return locked


def _format_solver_row_list(row_indices: list[int]) -> str:
    if not row_indices:
        return "(none)"
    return ", ".join(str(idx + 1) for idx in row_indices)


def _format_solver_name_list(names: list[str]) -> str:
    cleaned = [name for name in names if name]
    if not cleaned:
        return "(none)"
    return ", ".join(cleaned)


def _split_axis_delta(total_delta: int) -> tuple[int, int]:
    constrained_delta = total_delta // 2 if total_delta >= 0 else -((-total_delta) // 2)
    anchor_delta = constrained_delta - total_delta
    return constrained_delta, anchor_delta


def _build_axis_dependency_schedule(constraints: list, axis: str) -> dict:
    axis_rows: list[int] = []
    row_meta: dict[int, dict[str, str]] = {}
    anchor_to_rows: dict[str, list[int]] = {}
    constrained_keys: set[str] = set()
    anchor_key_order: list[str] = []
    seen_anchor_keys: set[str] = set()

    for row_index, c in enumerate(constraints):
        relation = c.get("relation") or "RIGHT_OF"
        if _relation_axis(relation) != axis:
            continue

        anchor_key = _constraint_node_key(c.get("anchor") or "")
        constrained_key = _constraint_node_key(c.get("constrained") or "")
        axis_rows.append(row_index)
        row_meta[row_index] = {
            "anchor_key": anchor_key,
            "constrained_key": constrained_key,
        }

        if anchor_key:
            anchor_to_rows.setdefault(anchor_key, []).append(row_index)
            if anchor_key not in seen_anchor_keys:
                anchor_key_order.append(anchor_key)
                seen_anchor_keys.add(anchor_key)
        if constrained_key:
            constrained_keys.add(constrained_key)

    board_key = BOARD_OUTLINE_ANCHOR_DISPLAY.casefold()
    order: list[int] = []
    scheduled_rows: set[int] = set()
    visited_nodes: set[str] = set()
    edge_rooted: list[int] = []
    natural_rooted: list[int] = []
    propagated: list[int] = []
    fallback: list[int] = []

    def _walk_from(root_key: str, root_kind: str):
        if not root_key:
            return
        queue: list[tuple[str, int]] = [(root_key, 0)]
        queue_idx = 0
        while queue_idx < len(queue):
            node_key, depth = queue[queue_idx]
            queue_idx += 1
            if node_key in visited_nodes:
                continue
            visited_nodes.add(node_key)

            for row_index in anchor_to_rows.get(node_key, []):
                if row_index in scheduled_rows:
                    continue
                order.append(row_index)
                scheduled_rows.add(row_index)
                if depth == 0:
                    if root_kind == "edge":
                        edge_rooted.append(row_index)
                    else:
                        natural_rooted.append(row_index)
                else:
                    propagated.append(row_index)

                child_key = row_meta[row_index]["constrained_key"]
                if child_key and child_key not in visited_nodes:
                    queue.append((child_key, depth + 1))

    if board_key in anchor_to_rows:
        _walk_from(board_key, "edge")

    for anchor_key in anchor_key_order:
        if anchor_key == board_key:
            continue
        if anchor_key in constrained_keys or anchor_key in visited_nodes:
            continue
        _walk_from(anchor_key, "natural")

    for row_index in axis_rows:
        if row_index in scheduled_rows:
            continue
        fallback.append(row_index)
        order.append(row_index)
        scheduled_rows.add(row_index)

    return {
        "axis": axis,
        "rows": axis_rows,
        "order": order,
        "edge_rooted": edge_rooted,
        "natural_rooted": natural_rooted,
        "propagated": propagated,
        "fallback": fallback,
    }


def _build_constraint_solve_schedule(constraints: list) -> dict:
    axis_summaries = {
        "x": _build_axis_dependency_schedule(constraints, "x"),
        "y": _build_axis_dependency_schedule(constraints, "y"),
    }

    order: list[int] = []
    scheduled_rows: set[int] = set()
    edge_rooted: list[int] = []
    natural_rooted: list[int] = []
    propagated: list[int] = []
    fallback: list[int] = []

    for axis in ("x", "y"):
        summary = axis_summaries[axis]
        for row_index in summary["order"]:
            if row_index in scheduled_rows:
                continue
            order.append(row_index)
            scheduled_rows.add(row_index)
        edge_rooted.extend(summary["edge_rooted"])
        natural_rooted.extend(summary["natural_rooted"])
        propagated.extend(summary["propagated"])
        fallback.extend(summary["fallback"])

    other_rows = [idx for idx in range(len(constraints)) if idx not in scheduled_rows]
    order.extend(other_rows)
    fallback.extend(other_rows)

    return {
        "order": order,
        "edge_rooted": edge_rooted,
        "natural_rooted": natural_rooted,
        "propagated": propagated,
        "fallback": fallback,
        "axes": axis_summaries,
    }


def _build_axis_stage_schedule(constraints: list, axis: str) -> dict:
    dependency = _build_axis_dependency_schedule(constraints, axis)
    edge_rows: list[int] = []
    free_rows: list[int] = []
    for row_index in dependency["order"]:
        anchor_name = (constraints[row_index].get("anchor") or "").strip()
        if is_board_outline_anchor(anchor_name):
            edge_rows.append(row_index)
        else:
            free_rows.append(row_index)
    return {
        "axis": axis,
        "edge_rows": edge_rows,
        "free_rows": free_rows,
        "dependency": dependency,
    }


def _apply_constraint_once(
    board,
    c: dict,
    row_index: int | None = None,
    pass_index: int | None = None,
    debug_log: list | None = None,
    locked_nodes: set[str] | None = None,
    stage_label: str | None = None,
) -> dict:
    """
    Move the constrained footprint so the selected constrained edge point sits
    relative to the selected anchor edge point by the requested gap.
    """
    locked_nodes = locked_nodes or set()
    anchor_name = (c.get("anchor") or "").strip()
    constrained_name = (c.get("constrained") or "").strip()
    anchor_edge = (c.get("anchor_edge") or "").strip()
    constrained_edge_input = c.get("constrained_edge") or "Right"
    constrained_edge = constrained_edge_input
    rel = c.get("relation") or "RIGHT_OF"
    axis = _relation_axis(rel)
    constrained_key = _constraint_node_key(constrained_name)
    anchor_key = _constraint_node_key(anchor_name)
    result = {
        "ok": False,
        "message": "",
        "row_index": row_index,
        "pass_index": pass_index,
        "anchor_name": anchor_name,
        "constrained_name": constrained_name,
        "anchor_edge": anchor_edge,
        "constrained_edge": constrained_edge,
        "relation": rel,
        "axis": axis,
        "dx": 0,
        "dy": 0,
        "movement_iu": 0,
        "movement_mm": 0.0,
        "axis_error_iu": 0,
        "axis_error_mm": 0.0,
        "cross_axis_offset_iu": 0,
        "cross_axis_offset_mm": 0.0,
        "motion_mode": "",
        "anchor_locked": False,
        "constrained_locked": False,
        "anchor_dx": 0,
        "anchor_dy": 0,
        "stage_label": stage_label or "",
    }
    try:
        gap_mm = float(c.get("gap_mm", 0))
    except (TypeError, ValueError):
        result["message"] = "Invalid gap."
        return result

    if not anchor_name or not constrained_name:
        result["message"] = "Anchor and constrained component names are required."
        return result
    if not anchor_edge:
        result["message"] = "Anchor edge is required."
        return result
    if anchor_name.strip().lower() == constrained_name.strip().lower():
        result["message"] = "Anchor and constrained must be different."
        return result

    constrained = _fp_by_display_name(board, constrained_name)
    if constrained is None:
        result["message"] = f"Constrained footprint not found: {constrained_name}"
        return result

    is_edge_cut_anchor = is_board_outline_anchor(anchor_name)
    anchor = None
    if is_edge_cut_anchor:
        ab = board_outline_bbox(board)
        if ab is None:
            result["message"] = "Could not compute Edge.Cuts outline — draw board outline on Edge.Cuts layer."
            return result
        if constrained_edge_input != "Center":
            _EDGE_CUT_NEAR_EDGE = {
                "RIGHT_OF": "Left",
                "LEFT_OF": "Right",
                "BELOW": "Top",
                "ABOVE": "Bottom",
            }
            constrained_edge = _EDGE_CUT_NEAR_EDGE.get(rel, constrained_edge)
    else:
        anchor = _fp_by_display_name(board, anchor_name)
        if anchor is None:
            result["message"] = f"Anchor footprint not found: {anchor_name}"
            return result
        ab = footprint_bbox(anchor)

    constrained_locked = bool(constrained_key and constrained_key in locked_nodes)
    anchor_locked = is_edge_cut_anchor or bool(anchor_key and anchor_key in locked_nodes)
    result["constrained_locked"] = constrained_locked
    result["anchor_locked"] = anchor_locked

    gap_iu = pcbnew.FromMM(gap_mm)
    cb = footprint_bbox(constrained)
    bbox_method = "footprint_bbox()"
    anchor_x, anchor_y = _bbox_edge_point(ab, anchor_edge)
    constrained_x, constrained_y = _bbox_edge_point(cb, constrained_edge)

    fp_pos = constrained.GetPosition()
    fp_x, fp_y = vector2i_xy(fp_pos)

    dx = dy = 0

    if rel == "RIGHT_OF":
        dx = (anchor_x + gap_iu) - constrained_x
        dy = anchor_y - constrained_y
    elif rel == "LEFT_OF":
        dx = (anchor_x - gap_iu) - constrained_x
        dy = anchor_y - constrained_y
    elif rel == "ALIGN_HORIZONTALLY":
        dx = anchor_x - constrained_x
        dy = anchor_y - constrained_y
    elif rel == "BELOW":
        dy = (anchor_y + gap_iu) - constrained_y
        dx = anchor_x - constrained_x
    elif rel == "ABOVE":
        dy = (anchor_y - gap_iu) - constrained_y
        dx = anchor_x - constrained_x
    elif rel == "ALIGN_VERTICALLY":
        dy = anchor_y - constrained_y
        dx = anchor_x - constrained_x
    else:
        result["message"] = f"Unknown relation: {rel}"
        return result

    dx_before_zero = dx
    dy_before_zero = dy
    if rel in ("RIGHT_OF", "LEFT_OF", "ALIGN_HORIZONTALLY"):
        dy = 0
    elif rel in ("ABOVE", "BELOW", "ALIGN_VERTICALLY"):
        dx = 0

    target_x = anchor_x
    target_y = anchor_y
    if rel == "RIGHT_OF":
        target_x = anchor_x + gap_iu
    elif rel == "LEFT_OF":
        target_x = anchor_x - gap_iu
    elif rel == "ALIGN_HORIZONTALLY":
        target_x = anchor_x
    elif rel == "BELOW":
        target_y = anchor_y + gap_iu
    elif rel == "ABOVE":
        target_y = anchor_y - gap_iu
    elif rel == "ALIGN_VERTICALLY":
        target_y = anchor_y

    constrained_move_dx = 0
    constrained_move_dy = 0
    anchor_move_dx = 0
    anchor_move_dy = 0

    if constrained_locked and anchor_locked:
        motion_mode = "blocked_locked"
    elif is_edge_cut_anchor:
        motion_mode = "edge_only"
        constrained_move_dx = dx
        constrained_move_dy = dy
    elif anchor_locked:
        motion_mode = "constrained_only"
        constrained_move_dx = dx
        constrained_move_dy = dy
    elif constrained_locked:
        motion_mode = "anchor_only"
        anchor_move_dx = -dx
        anchor_move_dy = -dy
    else:
        motion_mode = "split_evenly"
        if axis == "x":
            constrained_move_dx, anchor_move_dx = _split_axis_delta(dx)
        else:
            constrained_move_dy, anchor_move_dy = _split_axis_delta(dy)

    result["motion_mode"] = motion_mode

    if debug_log is not None:
        lines = [
            f"── {constrained_name}  →  {anchor_name} ──",
            f"  solver stage:        {stage_label or '-'}",
            f"  solver pass:         {(pass_index + 1) if pass_index is not None else '-'}",
            f"  row index:           {(row_index + 1) if row_index is not None else '-'}",
            f"  relation:           {rel}",
            f"  gap_mm:             {gap_mm}",
            f"  gap_iu:             {gap_iu}  ({mm(gap_iu):.4f} mm)",
            f"  is_edge_cut_anchor: {is_edge_cut_anchor}",
            f"  motion mode:        {motion_mode}",
            f"  constrained locked: {constrained_locked}",
            f"  anchor locked:      {anchor_locked}",
            f"  constrained_edge:   {constrained_edge}  (input was '{constrained_edge_input}')",
            f"  anchor_edge:        {anchor_edge}",
            f"  fp bbox method:     {bbox_method}",
            f"  anchor bbox (iu):   L={ab.GetLeft()}  R={ab.GetRight()}  T={ab.GetTop()}  B={ab.GetBottom()}",
            f"  anchor bbox (mm):   L={mm(ab.GetLeft()):.4f}  R={mm(ab.GetRight()):.4f}"
            f"  T={mm(ab.GetTop()):.4f}  B={mm(ab.GetBottom()):.4f}",
            f"  constr bbox (iu):   L={cb.GetLeft()}  R={cb.GetRight()}  T={cb.GetTop()}  B={cb.GetBottom()}",
            f"  constr bbox (mm):   L={mm(cb.GetLeft()):.4f}  R={mm(cb.GetRight()):.4f}"
            f"  T={mm(cb.GetTop()):.4f}  B={mm(cb.GetBottom()):.4f}",
            f"  anchor point (iu):  x={anchor_x}  y={anchor_y}",
            f"  anchor point (mm):  x={mm(anchor_x):.4f}  y={mm(anchor_y):.4f}",
            f"  constr point (iu):  x={constrained_x}  y={constrained_y}",
            f"  constr point (mm):  x={mm(constrained_x):.4f}  y={mm(constrained_y):.4f}",
            f"  fp position  (mm):  x={mm(fp_x):.4f}  y={mm(fp_y):.4f}",
            f"  dx (raw):           {dx_before_zero}  ({mm(dx_before_zero):.4f} mm)",
            f"  dy (raw):           {dy_before_zero}  ({mm(dy_before_zero):.4f} mm)",
            f"  constr move (mm):   dx={mm(constrained_move_dx):.4f}  dy={mm(constrained_move_dy):.4f}",
            f"  anchor move (mm):   dx={mm(anchor_move_dx):.4f}  dy={mm(anchor_move_dy):.4f}",
        ]
        if is_edge_cut_anchor:
            lines.append(f"  target edge (mm):   x={mm(target_x):.4f}  y={mm(target_y):.4f}")
            lines.append(f"  visual gap (mm):    {mm(gap_iu):.4f}")
        debug_log.extend(lines)

    if anchor is not None:
        _move_fp_by_delta(anchor, anchor_move_dx, anchor_move_dy)
    _move_fp_by_delta(constrained, constrained_move_dx, constrained_move_dy)
    ab_after = ab if is_edge_cut_anchor else footprint_bbox(anchor)
    cb_after = footprint_bbox(constrained)
    anchor_x_after, anchor_y_after = _bbox_edge_point(ab_after, anchor_edge)
    constrained_x_after, constrained_y_after = _bbox_edge_point(cb_after, constrained_edge)
    target_x_after = anchor_x_after
    target_y_after = anchor_y_after
    if rel == "RIGHT_OF":
        target_x_after = anchor_x_after + gap_iu
    elif rel == "LEFT_OF":
        target_x_after = anchor_x_after - gap_iu
    elif rel == "ALIGN_HORIZONTALLY":
        target_x_after = anchor_x_after
    elif rel == "BELOW":
        target_y_after = anchor_y_after + gap_iu
    elif rel == "ABOVE":
        target_y_after = anchor_y_after - gap_iu
    elif rel == "ALIGN_VERTICALLY":
        target_y_after = anchor_y_after
    axis_error_iu, cross_axis_offset_iu = _measure_constraint_error(
        target_x_after,
        target_y_after,
        constrained_x_after,
        constrained_y_after,
        axis,
    )
    movement_iu = max(
        abs(constrained_move_dx),
        abs(constrained_move_dy),
        abs(anchor_move_dx),
        abs(anchor_move_dy),
    )
    result.update(
        {
            "ok": True,
            "constrained_edge": constrained_edge,
            "dx": constrained_move_dx,
            "dy": constrained_move_dy,
            "anchor_dx": anchor_move_dx,
            "anchor_dy": anchor_move_dy,
            "movement_iu": movement_iu,
            "movement_mm": mm(movement_iu),
            "axis_error_iu": axis_error_iu,
            "axis_error_mm": mm(axis_error_iu),
            "cross_axis_offset_iu": cross_axis_offset_iu,
            "cross_axis_offset_mm": mm(cross_axis_offset_iu),
        }
    )
    if debug_log is not None:
        debug_log.append(
            "  post-apply:         "
            f"move={mm(movement_iu):.4f} mm  "
            f"axis_error={mm(axis_error_iu):.4f} mm  "
            f"cross_axis_offset={mm(cross_axis_offset_iu):.4f} mm"
        )
        if motion_mode == "blocked_locked" and axis_error_iu > 0:
            debug_log.append("  blocked:            both endpoints are locked on this axis.")
    return result


def apply_constraint(board, c: dict, debug_log: list | None = None) -> tuple[bool, str]:
    result = _apply_constraint_once(board, c, debug_log=debug_log)
    return bool(result.get("ok")), str(result.get("message", ""))


def _run_solver_stage(
    board,
    constraints: list,
    row_indices: list[int],
    axis: str,
    stage_name: str,
    locked_nodes: set[str],
    tolerance_iu: int,
    max_passes: int,
    errors_by_row: dict[int, str],
    debug_log: list | None = None,
    progress_callback=None,
    progress_state: dict | None = None,
) -> dict:
    if not row_indices:
        return {
            "axis": axis,
            "stage_name": stage_name,
            "rows": row_indices,
            "converged": True,
            "stalled": False,
            "had_errors": False,
            "passes_run": 0,
            "last_results": [],
            "last_max_move_iu": 0,
            "last_max_residual_iu": 0,
        }

    passes_run = 0
    converged = False
    stalled = False
    had_errors = False
    last_results: list[dict] = []
    last_max_move_iu = 0
    last_max_residual_iu = 0

    for pass_idx in range(max_passes):
        passes_run = pass_idx + 1
        pass_results: list[dict] = []
        pass_max_move_iu = 0
        pass_max_residual_iu = 0
        pass_had_errors = False

        if progress_state is not None:
            progress_state["current"] += 1
            if progress_callback is not None:
                progress_callback(progress_state["current"], progress_state["total"], False)

        if debug_log is not None:
            debug_log.append("")
            debug_log.append(
                f"=== Axis {axis.upper()} stage {stage_name} pass {passes_run} of {max_passes} ==="
            )
            debug_log.append(f"Stage rows:           {_format_solver_row_list(row_indices)}")
            debug_log.append(
                f"Locked nodes:         {_format_solver_name_list(sorted(locked_nodes, key=natural_sort_key))}"
            )

        for order_position, row_index in enumerate(row_indices):
            if debug_log is not None:
                debug_log.append(
                    f"\n--- Stage step {order_position + 1} of {len(row_indices)} (row {row_index + 1}) ---"
                )
            result = _apply_constraint_once(
                board,
                constraints[row_index],
                row_index=row_index,
                pass_index=pass_idx,
                debug_log=debug_log,
                locked_nodes=locked_nodes,
                stage_label=f"{axis.upper()} {stage_name}",
            )
            pass_results.append(result)
            if not result["ok"]:
                pass_had_errors = True
                errors_by_row.setdefault(row_index, f"Row {row_index + 1}: {result['message']}")
                if debug_log is not None:
                    debug_log.append(f"  ERROR:              {result['message']}")
                continue

            pass_max_move_iu = max(pass_max_move_iu, int(result.get("movement_iu", 0)))
            pass_max_residual_iu = max(pass_max_residual_iu, int(result.get("axis_error_iu", 0)))

        last_results = pass_results
        last_max_move_iu = pass_max_move_iu
        last_max_residual_iu = pass_max_residual_iu
        had_errors = had_errors or pass_had_errors

        if debug_log is not None:
            debug_log.append("")
            debug_log.append(
                "Stage summary:        "
                f"max_move={mm(pass_max_move_iu):.4f} mm  "
                f"max_residual={mm(pass_max_residual_iu):.4f} mm"
            )

        if pass_had_errors:
            break

        if pass_max_move_iu <= tolerance_iu and pass_max_residual_iu <= tolerance_iu:
            converged = True
            break

        if pass_max_move_iu == 0 and pass_max_residual_iu > tolerance_iu:
            stalled = True
            break

    return {
        "axis": axis,
        "stage_name": stage_name,
        "rows": row_indices,
        "converged": converged,
        "stalled": stalled,
        "had_errors": had_errors,
        "passes_run": passes_run,
        "last_results": last_results,
        "last_max_move_iu": last_max_move_iu,
        "last_max_residual_iu": last_max_residual_iu,
    }


def apply_all_constraints(
    board,
    constraints: list,
    debug_log: list | None = None,
    progress_callback=None,
) -> list[str]:
    if not constraints:
        return []

    tolerance_iu = max(1, int(pcbnew.FromMM(0.001)))
    max_passes = min(1000, max(10, len(constraints) * 6))
    errors_by_row: dict[int, str] = {}
    axis_stage_schedules = {
        "x": _build_axis_stage_schedule(constraints, "x"),
        "y": _build_axis_stage_schedule(constraints, "y"),
    }
    initial_locked_nodes = _locked_footprint_node_keys(board)
    axis_locks: dict[str, set[str]] = {
        "x": set(initial_locked_nodes),
        "y": set(initial_locked_nodes),
    }
    stage_sequence = []
    for axis in ("x", "y"):
        axis_schedule = axis_stage_schedules[axis]
        if axis_schedule["edge_rows"]:
            stage_sequence.append((axis, "EDGE_CUT", axis_schedule["edge_rows"]))
        if axis_schedule["free_rows"]:
            stage_sequence.append((axis, "FREE", axis_schedule["free_rows"]))

    progress_state = {"current": 0, "total": max(1, len(stage_sequence) * max_passes)}
    unresolved_stage_summaries: list[str] = []
    stage_results: list[dict] = []

    if debug_log is not None:
        debug_log.append("Solver settings:")
        debug_log.append(f"  tolerance:          {tolerance_iu} iu  ({mm(tolerance_iu):.6f} mm)")
        debug_log.append(f"  max passes:         {max_passes}")
        debug_log.append(
            f"  board locked nodes: {_format_solver_name_list(sorted(initial_locked_nodes, key=natural_sort_key))}"
        )
        debug_log.append("Two-stage schedule:")
        for axis in ("x", "y"):
            axis_schedule = axis_stage_schedules[axis]
            dependency = axis_schedule["dependency"]
            debug_log.append(
                f"  axis {axis}: edge={_format_solver_row_list(axis_schedule['edge_rows'])}  "
                f"free={_format_solver_row_list(axis_schedule['free_rows'])}  "
                f"fallback={_format_solver_row_list(dependency['fallback'])}"
            )

    for axis, stage_name, row_indices in stage_sequence:
        stage_summary = _run_solver_stage(
            board,
            constraints,
            row_indices,
            axis,
            stage_name,
            axis_locks[axis],
            tolerance_iu,
            max_passes,
            errors_by_row,
            debug_log=debug_log,
            progress_callback=progress_callback,
            progress_state=progress_state,
        )
        stage_results.append(stage_summary)

        if stage_name == "EDGE_CUT" and stage_summary["converged"]:
            new_lock_names = sorted(
                {
                    str(result.get("constrained_name", "")).strip()
                    for result in stage_summary["last_results"]
                    if result.get("ok") and str(result.get("constrained_name", "")).strip()
                },
                key=natural_sort_key,
            )
            axis_locks[axis].update(_constraint_node_key(name) for name in new_lock_names)
            if debug_log is not None:
                debug_log.append(
                    f"Locked {axis.upper()} nodes:  {_format_solver_name_list(new_lock_names)}"
                )

        if stage_summary["converged"] or stage_summary["had_errors"]:
            continue

        unresolved_results = [
            result
            for result in stage_summary["last_results"]
            if result.get("ok")
            and max(int(result.get("movement_iu", 0)), int(result.get("axis_error_iu", 0))) > tolerance_iu
        ]
        reason = "stalled" if stage_summary["stalled"] else "did not converge"
        summary = (
            f"Axis {axis.upper()} stage {stage_name} {reason} after {stage_summary['passes_run']} pass(es). "
            f"{len(unresolved_results)} unresolved row(s) remain."
        )
        unresolved_stage_summaries.append(summary)
        errors_by_row[len(constraints) + len(errors_by_row) + len(unresolved_stage_summaries)] = summary
        for result in unresolved_results[:8]:
            row_index = int(result.get("row_index", -1))
            errors_by_row[len(constraints) + len(errors_by_row) + len(unresolved_stage_summaries)] = (
                f"Row {row_index + 1}: unresolved in axis {axis.upper()} stage {stage_name} "
                f"(mode={result.get('motion_mode', '?')}, move={result.get('movement_mm', 0.0):.4f} mm, "
                f"axis_error={result.get('axis_error_mm', 0.0):.4f} mm)."
            )
        if debug_log is not None:
            debug_log.append("")
            debug_log.append(summary)
            for result in unresolved_results:
                debug_log.append(
                    "  unresolved row:     "
                    f"{int(result['row_index']) + 1}  "
                    f"mode={result.get('motion_mode', '?')}  "
                    f"move={result.get('movement_mm', 0.0):.4f} mm  "
                    f"axis_error={result.get('axis_error_mm', 0.0):.4f} mm"
                )

    if progress_callback is not None:
        progress_callback(progress_state["current"], progress_state["total"], True)

    if debug_log is not None and not unresolved_stage_summaries and not errors_by_row:
        debug_log.append("")
        debug_log.append("Solver converged for all active stages.")
    if hasattr(board, "BuildConnectivity"):
        try:
            board.BuildConnectivity()
        except Exception:
            pass
    try:
        pcbnew.Refresh()
    except Exception:
        pass
    return list(errors_by_row.values())


def load_constraints_from_file(path: Optional[str]) -> list:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("constraints") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rel = r.get("relation", "RIGHT_OF")
            if rel not in RELATION_IDS:
                rel = "RIGHT_OF"
            try:
                gap = float(r.get("gap_mm", 0))
            except (TypeError, ValueError):
                gap = 0.0
            if rel in ("ALIGN_HORIZONTALLY", "ALIGN_VERTICALLY"):
                gap = 0.0
            out.append(
                {
                    "constrained": str(r.get("constrained", "")).strip(),
                    "constrained_edge": _normalize_edge_name(r.get("constrained_edge", "Right"), "Right"),
                    "anchor": str(r.get("anchor", "")).strip(),
                    "anchor_edge": _normalize_optional_edge_name(
                        r["anchor_edge"] if "anchor_edge" in r else "Left",
                        "Left",
                    ),
                    "relation": rel,
                    "gap_mm": gap,
                }
            )
        return out
    except (OSError, json.JSONDecodeError):
        return []


def save_constraints_to_file(path: Optional[str], constraints: list) -> tuple:
    if not path:
        return False, "Board has no file path; save the PCB first."
    try:
        payload = {"version": 1, "constraints": constraints}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return True, ""
    except OSError as e:
        return False, str(e)


def load_dotenv_values(dotenv_path: str) -> dict[str, str]:
    values = {}
    if not dotenv_path or not os.path.isfile(dotenv_path):
        return values

    try:
        with open(dotenv_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                values[key] = value
    except OSError:
        return {}

    return values


def get_openai_settings() -> tuple[str, str]:
    api_key = PLACEHOLDER_FOR_OPENAI_API_KEY # type: ignore 
    model = PLACEHOLDER_FOR_OPENAI_MODEL  # type: ignore
    return api_key, model


def _extract_json_payload(text: str):
    raw_text = (text or "").strip()
    if not raw_text:
        raise ValueError("The AI response was empty.")

    candidates = [raw_text]
    fenced_matches = re.findall(r"```(?:json)?\s*(.*?)```", raw_text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())

    first_obj = raw_text.find("{")
    last_obj = raw_text.rfind("}")
    if first_obj != -1 and last_obj > first_obj:
        candidates.append(raw_text[first_obj : last_obj + 1])

    first_arr = raw_text.find("[")
    last_arr = raw_text.rfind("]")
    if first_arr != -1 and last_arr > first_arr:
        candidates.append(raw_text[first_arr : last_arr + 1])

    seen = set()
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            continue

    raise ValueError("The AI response was not valid JSON.")


def _normalize_edge_name(value: str, default: str) -> str:
    normalized = " ".join(str(value or "").strip().split()).lower()
    return {
        "top": "Top",
        "right": "Right",
        "bottom": "Bottom",
        "left": "Left",
        "center": "Center",
        "centre": "Center",
        "middle": "Center",
    }.get(normalized, default)


def _normalize_optional_edge_name(value: str, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _normalize_edge_name(text, default)


def _normalize_relation_id(value: str) -> Optional[str]:
    normalized = " ".join(str(value or "").strip().split()).upper()
    mapping = {
        "RIGHT_OF": "RIGHT_OF",
        "RIGHT OF": "RIGHT_OF",
        "TO THE RIGHT OF": "RIGHT_OF",
        RELATION_LABELS["RIGHT_OF"].upper(): "RIGHT_OF",
        "LEFT_OF": "LEFT_OF",
        "LEFT OF": "LEFT_OF",
        "TO THE LEFT OF": "LEFT_OF",
        RELATION_LABELS["LEFT_OF"].upper(): "LEFT_OF",
        "ALIGN_HORIZONTALLY": "ALIGN_HORIZONTALLY",
        "ALIGNED_HORIZONTALLY": "ALIGN_HORIZONTALLY",
        "ALIGNED HORIZONTALLY": "ALIGN_HORIZONTALLY",
        "ALIGN HORIZONTALLY": "ALIGN_HORIZONTALLY",
        RELATION_LABELS["ALIGN_HORIZONTALLY"].upper(): "ALIGN_HORIZONTALLY",
        "ABOVE": "ABOVE",
        RELATION_LABELS["ABOVE"].upper(): "ABOVE",
        "BELOW": "BELOW",
        RELATION_LABELS["BELOW"].upper(): "BELOW",
        "ALIGN_VERTICALLY": "ALIGN_VERTICALLY",
        "ALIGNED_VERTICALLY": "ALIGN_VERTICALLY",
        "ALIGNED VERTICALLY": "ALIGN_VERTICALLY",
        "ALIGN VERTICALLY": "ALIGN_VERTICALLY",
        RELATION_LABELS["ALIGN_VERTICALLY"].upper(): "ALIGN_VERTICALLY",
    }
    return mapping.get(normalized)


def build_component_name_lookup(component_rows: list) -> dict[str, str]:
    lookup = {}
    for row in component_rows:
        if row.get("is_edge_cut"):
            continue
        display_name = (row.get("display_name") or row["values"][0]).strip()
        ref = (row.get("ref") or "").strip()
        if display_name:
            lookup[display_name.casefold()] = display_name
        if ref:
            lookup[ref.casefold()] = display_name

    for alias in (
        BOARD_OUTLINE_ANCHOR_DISPLAY,
        "EDGE CUT",
        "EDGECUT",
        "BOARD",
        "BOARD OUTLINE",
        "OUTLINE",
    ):
        lookup[alias.casefold()] = BOARD_OUTLINE_ANCHOR_DISPLAY

    return lookup


def normalize_generated_constraints(raw_constraints, component_rows: list) -> tuple[list, list[str]]:
    if not isinstance(raw_constraints, list):
        raise ValueError("The AI response must include a constraints array.")

    name_lookup = build_component_name_lookup(component_rows)
    normalized_constraints = []
    warnings = []

    for index, constraint in enumerate(raw_constraints):
        if not isinstance(constraint, dict):
            warnings.append(f"Skipped item {index + 1}: expected an object.")
            continue

        constrained_raw = str(constraint.get("constrained", "")).strip()
        anchor_raw = str(constraint.get("anchor", "")).strip()
        constrained_name = name_lookup.get(constrained_raw.casefold(), "")
        anchor_name = name_lookup.get(anchor_raw.casefold(), "")

        if not constrained_name:
            warnings.append(f"Skipped item {index + 1}: unknown constrained component '{constrained_raw}'.")
            continue
        if is_board_outline_anchor(constrained_name):
            warnings.append(f"Skipped item {index + 1}: the constrained component cannot be EDGE_CUT.")
            continue
        if not anchor_name:
            warnings.append(f"Skipped item {index + 1}: unknown anchor '{anchor_raw}'.")
            continue
        if constrained_name == anchor_name:
            warnings.append(f"Skipped item {index + 1}: constrained and anchor cannot be the same.")
            continue

        relation_id = _normalize_relation_id(constraint.get("relation", ""))
        if relation_id not in RELATION_IDS:
            warnings.append(f"Skipped item {index + 1}: invalid relation '{constraint.get('relation', '')}'.")
            continue

        try:
            gap_mm = float(constraint.get("gap_mm", 0))
        except (TypeError, ValueError):
            warnings.append(f"Skipped item {index + 1}: invalid gap '{constraint.get('gap_mm', '')}'.")
            continue
        if relation_id in ("ALIGN_HORIZONTALLY", "ALIGN_VERTICALLY"):
            gap_mm = 0.0

        normalized_constraints.append(
            {
                "constrained": constrained_name,
                "constrained_edge": _normalize_edge_name(constraint.get("constrained_edge", "Right"), "Right"),
                "anchor": anchor_name,
                "anchor_edge": _normalize_optional_edge_name(
                    constraint["anchor_edge"] if "anchor_edge" in constraint else "Left",
                    "Left",
                ),
                "relation": relation_id,
                "gap_mm": gap_mm,
            }
        )

    return normalized_constraints, warnings


def model_supports_openai_vision(model: str) -> bool:
    """Rough check for chat/completions image input support (gpt-4o family, etc.)."""
    m = (model or "").lower().strip()
    if not m:
        return False
    if "gpt-3.5" in m:
        return False
    if m.startswith("o1") or m.startswith("o3"):
        return False
    if "gpt-4" in m or "gpt-4o" in m:
        return True
    return False


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


def try_capture_kicad_window_png_bytes() -> Optional[bytes]:
    """Grab the PCB editor top window client area as PNG (may fail with OpenGL canvases)."""
    try:
        app = wx.GetApp()
        if app is None:
            return None
        top = app.GetTopWindow()
        if top is None:
            return None
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
    """Export F.Cu to SVG via plotter; empty string if unsupported or on failure."""
    if not hasattr(pcbnew, "PLOT_FORMAT_SVG"):
        return ""
    tmp = tempfile.mkdtemp(prefix="connstraints_ai_svg_")
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


def gather_board_context_for_ai(
    board,
    component_rows: list,
    existing_constraints: list,
) -> dict:
    """Structured board data for the model (positions, bbox, outline, existing constraints)."""
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

    existing = []
    for c in existing_constraints or []:
        existing.append(
            {
                "constrained": c.get("constrained", ""),
                "constrained_edge": c.get("constrained_edge", ""),
                "anchor": c.get("anchor", ""),
                "anchor_edge": c.get("anchor_edge", ""),
                "relation": c.get("relation", ""),
                "gap_mm": c.get("gap_mm", 0),
            }
        )

    return {
        "board_file": path,
        "units": "mm",
        "axes": "+X right, +Y down (KiCad PCB space, mm below).",
        "board_outline_bbox_mm": outline_mm,
        "edge_cut_summary": edge_cut_summary,
        "footprint_count": len(components),
        "components": components,
        "existing_constraints": existing,
    }


def fetch_ai_generated_constraints(
    prompt_text: str,
    component_rows: list,
    board,
    existing_constraints: list,
) -> tuple[list, str, str]:
    api_key, model = get_openai_settings()
    if not api_key:
        raise ValueError(
            "Missing OPENAI API key. Set OPENAI_API_KEY in the .env file or as an environment variable."
        )
    if not model:
        raise ValueError(
            "Missing OpenAI model. Set OPENAI_MODEL in the .env file or as an environment variable."
        )

    board_context = gather_board_context_for_ai(board, component_rows, existing_constraints)
    svg_excerpt = try_export_board_svg_snippet(board)
    if svg_excerpt:
        board_context["svg_f_cu_plot_excerpt"] = svg_excerpt

    png_bytes = try_capture_kicad_window_png_bytes()
    use_image = bool(png_bytes) and model_supports_openai_vision(model)

    system_prompt = (
        "You are an expert KiCad PCB layout assistant. You convert natural-language placement goals into "
        "a minimal, consistent set of relative bounding-box constraints between footprints.\n\n"
        "OUTPUT FORMAT: Respond with JSON only (no prose outside JSON). The root object MUST have:\n"
        '  "summary": string — short explanation of what you did and any assumptions.\n'
        '  "constraints": array of constraint objects.\n\n'
        "EACH CONSTRAINT OBJECT must include these keys exactly:\n"
        "  constrained — string: reference or display name of the footprint TO MOVE (from board_context.components).\n"
        "  constrained_edge — one of: Top, Right, Bottom, Left, Center (which edge/center of that footprint’s bbox).\n"
        "  anchor — string: reference/display name of another footprint, OR a board-outline token (see below).\n"
        "  anchor_edge — Top, Right, Bottom, Left, or Center.\n"
        "  relation — one of: RIGHT_OF, LEFT_OF, ALIGN_HORIZONTALLY, ABOVE, BELOW, ALIGN_VERTICALLY.\n"
        "  gap_mm — number: non-negative edge-to-edge gap in millimeters along the relation axis.\n"
        "           For ALIGN_HORIZONTALLY / ALIGN_VERTICALLY, gap_mm must be 0.\n\n"
        "SEMANTICS:\n"
        "- RIGHT_OF / LEFT_OF / ALIGN_HORIZONTALLY apply along X; ABOVE / BELOW / ALIGN_VERTICALLY apply along Y.\n"
        "- ALIGN_HORIZONTALLY means same X for the chosen edges/centers (gap_mm = 0).\n"
        "- ALIGN_VERTICALLY means same Y for the chosen edges/centers (gap_mm = 0).\n"
        "- gap_mm is the distance between the specified constrained_edge and anchor_edge, measured along that axis.\n"
        "- IMPORTANT: Center is still a single point used on ONE axis per constraint. "
        "If relation is X-axis (RIGHT_OF/LEFT_OF/ALIGN_HORIZONTALLY), solve only X. "
        "If relation is Y-axis (ABOVE/BELOW/ALIGN_VERTICALLY), solve only Y.\n"
        "- Do NOT assume Center means lock both X and Y in one rule. "
        "To align both axes, emit two constraints: one horizontal and one vertical.\n"
        "- constrained is always the part whose position is adjusted to satisfy the rule relative to anchor.\n\n"
        "ANCHOR NAMES FOR BOARD OUTLINE: Use exactly one of the allowed_anchor_special_values from the user "
        "payload when anchoring to Edge.Cuts / board outline (often labeled EDGE_CUT in this tool).\n"
        "EDGE_CUT PLACEMENT RULES: When anchoring to the board outline, the system automatically uses the nearest "
        "edge of the constrained component (e.g. RIGHT_OF uses Left edge, LEFT_OF uses Right edge, "
        "BELOW uses Top edge, ABOVE uses Bottom edge). So gap_mm is always the visible distance from "
        "the component to the board edge. Use the anchor_edge that matches the board side you want to "
        "constrain against (e.g. anchor_edge=Left + relation=RIGHT_OF to keep a component inside from "
        "the left edge; anchor_edge=Top + relation=BELOW to keep it below the top edge).\n\n"
        "SOLVER ERROR DEBUGGING:\n"
        "- The user_request may include pasted editor output such as 'Constraint solve failed or did not converge', "
        "'Solver did not converge after N pass(es)', or lines like "
        "'Row 112: still moving SW6 BELOW SW5 by 0.8600 mm on axis y.' Treat that as a request to diagnose and "
        "repair the current constraint set, not as a request for unrelated new placement rules.\n"
        "- When solver row numbers are present, interpret them as 1-based indices into board_context.existing_constraints "
        "in the order provided.\n"
        "- Use the reported component names, relation, axis, and repeated movement amount to detect contradictory, cyclic, "
        "or over-constrained rules. Prefer the smallest fix that preserves the user's layout intent.\n"
        "- If fixing the issue requires replacing or removing existing rows, omit the bad rows from your output and say so "
        "clearly in summary so the user can replace the old set in the table.\n\n"
        "RULES:\n"
        "- Use ONLY component refs, display names, or allowed outline tokens present in board_context. Never invent parts.\n"
        "- Prefer the smallest useful set of constraints; omit redundant rows.\n"
        "- Trust numeric fields in board_context for positions and bbox edges; use screenshot/SVG only to resolve ambiguity.\n"
        "- If the user request is ambiguous, pick the simplest physically plausible reading and note it in summary.\n"
        "- If existing_constraints is non-empty, you may extend or replace logically per the user request; avoid contradicting "
        "unless the user asks to change placement intent.\n"
        "- NEVER output a constraint that is an exact duplicate of one already in existing_constraints (same constrained, "
        "constrained_edge, anchor, anchor_edge, relation, AND gap_mm). If the user's request matches an existing rule "
        "exactly, omit it from your output."
    )

    user_payload = {
        "user_request": prompt_text,
        "allowed_anchor_special_values": [
            BOARD_OUTLINE_ANCHOR_DISPLAY,
            "EDGE_CUT",
            "EDGE CUT",
            "BOARD",
            "BOARD OUTLINE",
        ],
        "board_context": board_context,
        "media": {
            "pcb_editor_screenshot_png_included": use_image,
            "svg_f_cu_plot_included": bool(svg_excerpt),
        },
    }
    user_text = json.dumps(user_payload, indent=2)

    if use_image and png_bytes:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        user_content: Union[list, str] = [
            {
                "type": "text",
                "text": (
                    user_text
                    + "\n\nThe following image is a PNG capture of the KiCad PCB editor window "
                    "(approximate 2D view of the board; use it together with board_context)."
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        user_content = user_text
        if png_bytes and not use_image:
            user_content += (
                "\n\nNote: A PCB editor screenshot was captured but not attached because the selected model "
                "may not support images in the Chat Completions API. Use a vision-capable model such as gpt-4o "
                "to include the screenshot."
            )

    request_payload = {
        "model": model,
        "temperature": 0.15,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
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
        with urllib.request.urlopen(request, timeout=120) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            message = error_payload.get("error", {}).get("message") or str(exc)
        except Exception:
            message = str(exc)
        raise ValueError(f"OpenAI request failed: {message}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"OpenAI request failed: {exc.reason}") from exc

    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("OpenAI returned no choices.")

    content = choices[0].get("message", {}).get("content", "")
    parsed = _extract_json_payload(content)
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary", "")).strip()
        raw_constraints = parsed.get("constraints", [])
    else:
        summary = ""
        raw_constraints = parsed

    constraints, warnings = normalize_generated_constraints(raw_constraints, component_rows)
    return constraints, summary, "\n".join(warnings)


class AllFootprintsDialog(wx.Dialog):
    APPLY_UNDO_LIMIT = 4
    COLS = [
        "Name",
        "Constraints",
        "Width",
        "Height",
        "x_left",
        "x_right",
        "y_top",
        "y_bottom",
        "x_center",
        "y_center",
        "Locked",
    ]
    COL_NAME = 0
    COL_CONSTRAINTS = 1
    COL_WIDTH = 2
    COL_HEIGHT = 3
    COL_X_LEFT = 4
    COL_X_RIGHT = 5
    COL_Y_TOP = 6
    COL_Y_BOTTOM = 7
    COL_X_CENTER = 8
    COL_Y_CENTER = 9
    COL_LOCKED = 10

    CON_COLS = [
        "Point/Edge 1",
        "Edge 1",
        "Point/Edge 2",
        "Edge 2",
        "Relation",
        "Distance",
        "Description",
    ]
    CON_COL_CONSTRAINED = 0
    CON_COL_CONSTRAINED_EDGE = 1
    CON_COL_ANCHOR = 2
    CON_COL_ANCHOR_EDGE = 3
    CON_COL_RELATION = 4
    CON_COL_DISTANCE = 5
    CON_COL_DESCRIPTION = 6

    TAB_COMPONENTS = 0
    TAB_CONSTRAINTS = 1
    TAB_AI_BUILDER = 2
    TAB_DEBUG_LOG = 3

    def __init__(self, parent, board):
        super().__init__(
            parent,
            title="Connstraints",
            size=(1600, 1000),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.board = board
        self._populating = False
        self._constraints_path = board_constraints_path(board)
        self.constraints = load_constraints_from_file(self._constraints_path)
        self.constraints = self._normalize_constraint_orientations(self.constraints)
        self.component_rows = []
        self._warned_missing_constraints_path = False
        self.apply_undo_stack = []
        self.constraint_filter_name = None
        self.constraint_filter_mode = None
        self.view_pcb_constraints_btn = None
        self.show_components_constraints_btn = None
        self.constraint_view_indices = []
        self.constraint_view_flipped = []
        self.pick_stage = PICK_STAGE_IDLE
        self.pick_constrained = None
        self.pick_last_signature = None
        self.pick_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_pick_timer, self.pick_timer)
        self._ai_result_rows = []
        self._con_sort_col = -1
        self._con_sort_asc = True
        self._comp_sort_col = -1
        self._comp_sort_asc = True
        self._filter_duplicates_only = False
        self._constraint_drawings: list = []
        self.apply_constraints_btn = None
        self.debug_apply_btn = None
        self.solve_activity = None
        self.debug_log_ctrl = None

        sizer = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(
            self,
            label=f"Board: {board.GetFileName() if hasattr(board, 'GetFileName') else ''}",
        )
        sizer.Add(header, 0, wx.ALL | wx.EXPAND, 8)

        notebook = wx.Notebook(self)
        self._notebook = notebook

        components_panel = wx.Panel(notebook)
        components_sizer = wx.BoxSizer(wx.VERTICAL)

        try:
            units = board.GetDesignSettings().GetDesignUnits()
        except Exception:
            units = "mm"
        fp_label = wx.StaticText(
            components_panel,
            label=(
                "First row = Edge.Cuts board outline bbox (coordinates are editable).\n\n"
                f"Values in {units}."
            ),
        )
        components_sizer.Add(fp_label, 0, wx.ALL | wx.EXPAND, 8)

        self.grid = gridlib.Grid(components_panel)
        self.grid.CreateGrid(0, len(self.COLS))
        self.grid.EnableDragRowSize(False)
        self.grid.SetRowLabelSize(0)
        self.grid.EnableEditing(True)

        for i, col in enumerate(self.COLS):
            self.grid.SetColLabelValue(i, col)

        self.grid.Bind(gridlib.EVT_GRID_CELL_CHANGED, self.on_any_grid_changed)
        self.grid.Bind(wx.EVT_KEY_DOWN, self.on_components_grid_key_down)
        self.grid.Bind(gridlib.EVT_GRID_LABEL_LEFT_CLICK, self.on_component_col_header_click)
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, self.on_component_selection_changed)
        self.grid.Bind(gridlib.EVT_GRID_RANGE_SELECT, self.on_component_selection_changed)

        components_sizer.Add(self.grid, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        components_selection_actions = wx.BoxSizer(wx.HORIZONTAL)
        clear_component_selection_btn = wx.Button(components_panel, label="Clear selection")
        clear_component_selection_btn.Bind(wx.EVT_BUTTON, self.on_clear_component_selection)
        components_selection_actions.Add(clear_component_selection_btn, 0)
        components_sizer.Add(components_selection_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.show_constraints_btn = wx.Button(components_panel, label="Show constraints on PCB")
        self.show_constraints_btn.Bind(wx.EVT_BUTTON, self.on_toggle_show_constraints)
        self.show_dimensions_cb = wx.CheckBox(components_panel, label="Dimensions")
        self.show_dimensions_cb.SetValue(True)
        self.show_labels_cb = wx.CheckBox(components_panel, label="Labels")
        self.show_labels_cb.SetValue(True)

        components_overlay_actions = wx.BoxSizer(wx.HORIZONTAL)
        components_overlay_actions.Add(self.show_constraints_btn, 0, wx.RIGHT, 8)
        components_overlay_actions.Add(self.show_dimensions_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        components_overlay_actions.Add(self.show_labels_cb, 0, wx.ALIGN_CENTER_VERTICAL)
        components_sizer.Add(components_overlay_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        components_panel.SetSizer(components_sizer)
        notebook.AddPage(components_panel, "Components")

        constraints_panel = wx.Panel(notebook)
        constraints_sizer = wx.BoxSizer(wx.VERTICAL)

        c_label = wx.StaticText(
            constraints_panel,
            label=(
                "Constraints — choose component names from the Components table.\n"
                "Anchor can also be EDGE_CUT / BOARD OUTLINE for the Edge.\n"
                "Pick constraint lets you select items in PCB Editor and then choose their bbox edge."
            ),
        )
        constraints_sizer.Add(c_label, 0, wx.ALL | wx.EXPAND, 8)

        self.pick_status_label = wx.StaticText(constraints_panel, label="Edge picker idle.")
        constraints_sizer.Add(self.pick_status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.cgrid = gridlib.Grid(constraints_panel)
        self.cgrid.CreateGrid(0, len(self.CON_COLS))
        self.cgrid.EnableDragRowSize(False)
        self.cgrid.EnableEditing(True)

        for i, col in enumerate(self.CON_COLS):
            self.cgrid.SetColLabelValue(i, col)

        edge_attr_1 = gridlib.GridCellAttr()
        edge_attr_1.SetEditor(gridlib.GridCellChoiceEditor(list(EDGE_POINT_LABELS)))
        self.cgrid.SetColAttr(1, edge_attr_1)
        edge_attr_2 = gridlib.GridCellAttr()
        edge_attr_2.SetEditor(gridlib.GridCellChoiceEditor(list(EDGE_POINT_LABELS)))
        self.cgrid.SetColAttr(3, edge_attr_2)

        rel_choices = [RELATION_LABELS[k] for k in RELATION_IDS]
        rel_attr = gridlib.GridCellAttr()
        rel_attr.SetEditor(gridlib.GridCellChoiceEditor(rel_choices))
        self.cgrid.SetColAttr(4, rel_attr)
        self.refresh_constraint_choice_editors()

        self.cgrid.Bind(gridlib.EVT_GRID_CELL_CHANGED, self.on_any_grid_changed)
        self.cgrid.Bind(wx.EVT_KEY_DOWN, self.on_constraints_grid_key_down)
        self.cgrid.Bind(gridlib.EVT_GRID_LABEL_LEFT_CLICK, self.on_constraint_col_header_click)
        self.cgrid.Bind(gridlib.EVT_GRID_SELECT_CELL, self.on_constraint_selection_changed)
        self.cgrid.Bind(gridlib.EVT_GRID_RANGE_SELECT, self.on_constraint_selection_changed)

        constraints_sizer.Add(self.cgrid, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        top_actions = wx.BoxSizer(wx.HORIZONTAL)
        row_actions = wx.BoxSizer(wx.HORIZONTAL)
        picker_actions = wx.BoxSizer(wx.HORIZONTAL)
        add_btn = wx.Button(constraints_panel, label="Add constraint row")
        copy_btn = wx.Button(constraints_panel, label="Copy selected row(s)")
        pick_btn = wx.Button(constraints_panel, label="Pick constraint")
        cancel_pick_btn = wx.Button(constraints_panel, label="Cancel pick")
        self.view_pcb_constraints_btn = wx.Button(
            constraints_panel,
            label="View PCBEditor Selected Component Constraints",
        )
        self.show_components_constraints_btn = wx.Button(
            constraints_panel,
            label="Show Components Constraints",
        )
        clear_view_btn = wx.Button(constraints_panel, label="Clear view")
        up_btn = wx.Button(constraints_panel, label="Move Up ↑")
        down_btn = wx.Button(constraints_panel, label="Move Down ↓")
        del_btn = wx.Button(constraints_panel, label="Remove selected row(s)")
        self.undo_apply_btn = wx.Button(self, label="Undo apply")
        self.apply_constraints_btn = wx.Button(self, label="Apply constraints")
        self.apply_constraints_btn.SetBackgroundColour(wx.Colour(24, 143, 80))
        self.apply_constraints_btn.SetForegroundColour(wx.WHITE)
        add_btn.Bind(wx.EVT_BUTTON, self.on_add_constraint_row)
        copy_btn.Bind(wx.EVT_BUTTON, self.on_copy_constraint_row)
        pick_btn.Bind(wx.EVT_BUTTON, self.on_start_pick_constraint)
        cancel_pick_btn.Bind(wx.EVT_BUTTON, self.on_cancel_pick)
        self.view_pcb_constraints_btn.Bind(wx.EVT_BUTTON, self.on_start_view_constraint)
        self.show_components_constraints_btn.Bind(wx.EVT_BUTTON, self.on_show_component_constraints)
        clear_view_btn.Bind(wx.EVT_BUTTON, self.on_clear_constraint_view)
        up_btn.Bind(wx.EVT_BUTTON, self.on_move_constraint_rows_up)
        down_btn.Bind(wx.EVT_BUTTON, self.on_move_constraint_rows_down)
        del_btn.Bind(wx.EVT_BUTTON, self.on_remove_constraint_row)
        self.debug_apply_btn = wx.Button(self, label="Debug apply")
        self.undo_apply_btn.Bind(wx.EVT_BUTTON, self.on_undo_apply_constraints)
        self.apply_constraints_btn.Bind(wx.EVT_BUTTON, self.on_apply_constraints)
        self.debug_apply_btn.Bind(wx.EVT_BUTTON, self.on_debug_apply_constraints)
        top_actions.Add(up_btn, 0, wx.RIGHT, 8)
        top_actions.Add(down_btn, 0, wx.RIGHT, 8)
        constraints_sizer.Add(top_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        row_actions.Add(add_btn, 0, wx.RIGHT, 8)
        row_actions.Add(copy_btn, 0, wx.RIGHT, 8)
        row_actions.Add(del_btn, 0)
        constraints_sizer.Add(row_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        picker_actions.Add(pick_btn, 0, wx.RIGHT, 8)
        picker_actions.Add(cancel_pick_btn, 0)
        constraints_sizer.Add(picker_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        view_dup_btn = wx.Button(constraints_panel, label="View duplicates")
        view_dup_btn.Bind(wx.EVT_BUTTON, self.on_view_duplicates)

        view_actions = wx.BoxSizer(wx.HORIZONTAL)
        view_actions.Add(self.view_pcb_constraints_btn, 0, wx.RIGHT, 8)
        view_actions.Add(self.show_components_constraints_btn, 0, wx.RIGHT, 8)
        view_actions.Add(clear_view_btn, 0, wx.RIGHT, 8)
        constraints_sizer.Add(view_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        dup_actions = wx.BoxSizer(wx.HORIZONTAL)
        dup_actions.Add(view_dup_btn, 0)
        constraints_sizer.Add(dup_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        constraints_panel.SetSizer(constraints_sizer)
        notebook.AddPage(constraints_panel, "Constraints")

        ai_panel = wx.Panel(notebook)
        ai_sizer = wx.BoxSizer(wx.VERTICAL)

        ai_label = wx.StaticText(
            ai_panel,
            label=(
                "Describe the layout you want and AI will draft constraints using the current components.\n"
                "You can also paste solver failures or non-converged row messages and ask AI to repair the constraint set.\n"
                "Each request sends board data (positions, bbox, outline, existing constraints), an optional F.Cu SVG plot, "
                "and—if your model supports vision (e.g. gpt-4o)—a PNG of the PCB editor window.\n"
                "After generation, tick the constraints you want, then click Update Constraints Table.\n"
                "Example: Place SW1 19.05mm to the right of SW2, and keep the top row 8mm below EDGE_CUT."
            ),
        )
        ai_sizer.Add(ai_label, 0, wx.ALL | wx.EXPAND, 8)

        self.ai_prompt_ctrl = wx.TextCtrl(ai_panel, style=wx.TE_MULTILINE, size=(-1, 80))
        if hasattr(self.ai_prompt_ctrl, "SetHint"):
            self.ai_prompt_ctrl.SetHint(
                "Describe the layout you want, or paste a solver error and ask AI to fix the conflicting constraints."
            )
        ai_sizer.Add(self.ai_prompt_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        ai_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.ai_clear_btn = wx.Button(ai_panel, label="Clear prompt")
        self.ai_generate_btn = wx.Button(ai_panel, label="Generate constraints")
        self.ai_generate_btn.Bind(wx.EVT_BUTTON, self.on_generate_ai_constraints)
        self.ai_clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_ai_prompt)
        ai_actions.Add(self.ai_clear_btn, 0, wx.RIGHT, 8)
        ai_actions.Add(self.ai_generate_btn, 0)
        ai_sizer.Add(ai_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.ai_status_label = wx.StaticText(ai_panel, label="Ready to generate constraints.")
        ai_sizer.Add(self.ai_status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        ai_summary_label = wx.StaticText(ai_panel, label="Summary & warnings")
        ai_sizer.Add(ai_summary_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        self.ai_preview_ctrl = wx.TextCtrl(
            ai_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.ai_preview_ctrl.SetMinSize((-1, 80))
        ai_sizer.Add(self.ai_preview_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.ai_generated_label = wx.StaticText(ai_panel, label="Generated constraints (select to add)")
        ai_sizer.Add(self.ai_generated_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.ai_results_scroll = wx.ScrolledWindow(ai_panel, style=wx.VSCROLL)
        self.ai_results_scroll.SetScrollRate(0, 10)
        self.ai_results_sizer = wx.BoxSizer(wx.VERTICAL)
        self.ai_results_scroll.SetSizer(self.ai_results_sizer)
        ai_sizer.Add(self.ai_results_scroll, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        add_row = wx.BoxSizer(wx.HORIZONTAL)
        self.ai_replace_checkbox = wx.CheckBox(
            ai_panel,
            label="Replace the current constraints in the table instead of appending",
        )
        add_row.Add(self.ai_replace_checkbox, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        ai_sizer.Add(add_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        ai_panel.SetSizer(ai_sizer)
        notebook.AddPage(ai_panel, "AI Builder")

        debug_panel = wx.Panel(notebook)
        debug_sizer = wx.BoxSizer(wx.VERTICAL)

        debug_label = wx.StaticText(
            debug_panel,
            label=(
                "Debug Log\n"
                "Run Debug apply from the Constraints tab to populate this log."
            ),
        )
        debug_sizer.Add(debug_label, 0, wx.ALL | wx.EXPAND, 8)

        self.debug_log_ctrl = wx.TextCtrl(
            debug_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
        )
        self.debug_log_ctrl.SetMinSize((-1, 300))
        debug_sizer.Add(self.debug_log_ctrl, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        debug_actions = wx.BoxSizer(wx.HORIZONTAL)
        debug_copy_btn = wx.Button(debug_panel, label="Copy log")
        debug_clear_btn = wx.Button(debug_panel, label="Clear log")
        debug_copy_btn.Bind(wx.EVT_BUTTON, self.on_copy_debug_log)
        debug_clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_debug_log)
        debug_actions.Add(debug_copy_btn, 0, wx.RIGHT, 8)
        debug_actions.Add(debug_clear_btn, 0)
        debug_sizer.Add(debug_actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        debug_panel.SetSizer(debug_sizer)
        notebook.AddPage(debug_panel, "Debug Log")

        sizer.Add(notebook, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self._constraints_btns = wx.BoxSizer(wx.HORIZONTAL)
        self._constraints_btns.Add(self.undo_apply_btn, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        self._constraints_btns.Add(self.apply_constraints_btn, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        self.solve_status_label = wx.StaticText(self, label="")
        self.solve_status_label.Hide()
        self._constraints_btns.Add(self.solve_status_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        self._constraints_btns.Add(self.debug_apply_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        self._ai_builder_btns = wx.BoxSizer(wx.HORIZONTAL)
        self.ai_bottom_add_btn = wx.Button(self, label="Update Constraints Table")
        self.ai_bottom_add_btn.Bind(wx.EVT_BUTTON, self.on_add_ai_constraints_to_table)
        self._ai_builder_btns.Add(self.ai_bottom_add_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        close_btn = wx.Button(self, wx.ID_CLOSE, "Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda evt: self.Close())

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self._constraints_btns, 0, wx.ALIGN_CENTER_VERTICAL)
        btns.Add(self._ai_builder_btns, 0, wx.ALIGN_CENTER_VERTICAL)
        btns.AddStretchSpacer()
        btns.Add(close_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed)
        self._sync_bottom_buttons()

        self.SetSizer(sizer)
        self.SetMinSize((900, 560))
        self.Bind(wx.EVT_CLOSE, self.on_close_dialog)
        self._update_constraint_mode_button_styles()
        self.populate()
        self.populate_constraints()
        self.update_undo_apply_button()
        self.Centre()

    def _sync_bottom_buttons(self):
        is_ai = self._notebook.GetSelection() == self.TAB_AI_BUILDER
        self._constraints_btns.ShowItems(not is_ai)
        self._ai_builder_btns.ShowItems(is_ai)
        self.Layout()

    def _on_tab_changed(self, evt):
        self._sync_bottom_buttons()
        evt.Skip()

    def _set_debug_log_text(self, text: str, switch_to_tab: bool = False):
        if self.debug_log_ctrl is not None:
            self.debug_log_ctrl.SetValue(text or "")
        if switch_to_tab and hasattr(self, "_notebook") and self._notebook is not None:
            self._notebook.SetSelection(self.TAB_DEBUG_LOG)
            self._sync_bottom_buttons()

    def on_copy_debug_log(self, _evt):
        if self.debug_log_ctrl is None:
            return
        data = wx.TextDataObject(self.debug_log_ctrl.GetValue())
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(data)
            finally:
                wx.TheClipboard.Close()

    def on_clear_debug_log(self, _evt):
        self._set_debug_log_text("")

    def _copy_text_to_clipboard(self, text: str) -> bool:
        if not text:
            return False
        data = wx.TextDataObject(text)
        if not wx.TheClipboard.Open():
            return False
        try:
            wx.TheClipboard.SetData(data)
            return True
        finally:
            wx.TheClipboard.Close()

    def on_component_selection_changed(self, evt):
        evt.Skip()
        if self._populating or self.constraint_filter_mode != CONSTRAINT_FILTER_MODE_COMPONENTS_SELECTION:
            return
        try:
            wx.CallAfter(self._sync_constraint_filter_from_component_selection)
        except Exception:
            self._sync_constraint_filter_from_component_selection()

    def on_clear_component_selection(self, _evt):
        self.grid.ClearSelection()
        try:
            self.grid.SetGridCursor(-1, -1)
        except Exception:
            pass
        if self.constraint_filter_mode == CONSTRAINT_FILTER_MODE_COMPONENTS_SELECTION:
            self.constraint_filter_name = None
            self.populate_constraints()
            if self._constraint_drawings:
                self._refresh_constraint_drawings_if_visible()

    def on_constraint_selection_changed(self, evt):
        evt.Skip()
        if self._populating or not self._constraint_drawings:
            return
        try:
            wx.CallAfter(self._refresh_constraint_drawings_if_visible)
        except Exception:
            self._refresh_constraint_drawings_if_visible()

    def _mark_dirty(self):
        pass

    def _selected_component_filter_name(self):
        rows = self._selected_grid_rows(self.grid)
        if not rows:
            return None
        row = rows[0]
        if not (0 <= row < self.grid.GetNumberRows()):
            return None
        name = self.grid.GetCellValue(row, self.COL_NAME).strip()
        return name or None

    def _selected_constraint_cell_filter_name(self):
        if self.cgrid is None or self.cgrid.GetNumberRows() <= 0:
            return None

        row = self.cgrid.GetGridCursorRow()
        col = self.cgrid.GetGridCursorCol()
        if not (0 <= row < self.cgrid.GetNumberRows()):
            selected_rows = self._selected_grid_rows(self.cgrid)
            if not selected_rows:
                return None
            row = selected_rows[0]
            col = self.CON_COL_CONSTRAINED

        constrained_name = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED).strip()
        anchor_name = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR).strip()

        if col == self.CON_COL_CONSTRAINED and constrained_name:
            return constrained_name
        if col == self.CON_COL_ANCHOR and anchor_name:
            return anchor_name

        return constrained_name or anchor_name or None

    def _sync_constraint_filter_from_component_selection(self):
        filter_name = self._selected_component_filter_name()
        if filter_name == self.constraint_filter_name and not self._filter_duplicates_only:
            if self._constraint_drawings:
                self._refresh_constraint_drawings_if_visible()
            return
        self.constraints = self._constraints_from_grid()
        self._filter_duplicates_only = False
        self.constraint_filter_name = filter_name
        self.populate_constraints()
        if self._constraint_drawings:
            self._refresh_constraint_drawings_if_visible()

    def on_show_component_constraints(self, _evt):
        filter_name = self._selected_constraint_cell_filter_name()
        if not filter_name:
            filter_name = self._selected_component_filter_name()
        if not filter_name:
            wx.MessageBox(
                "Select a component in the Constraints table (preferred) or a row in the Components table first.",
                "Show Components Constraints",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        self.constraints = self._constraints_from_grid()
        self._filter_duplicates_only = False
        self.constraint_filter_mode = CONSTRAINT_FILTER_MODE_COMPONENTS_SELECTION
        self.constraint_filter_name = filter_name
        self.populate_constraints()
        if self._constraint_drawings:
            self._refresh_constraint_drawings_if_visible()
        self.reset_pick_state(f"Viewing constraints for {filter_name} from Components selection.")

    def persist_constraints(self, show_errors: bool = False):
        self.constraints = self._constraints_from_grid() if self.cgrid.GetNumberRows() else self.constraints
        ok, err = save_constraints_to_file(self._constraints_path, self.constraints)
        if ok:
            return True

        if show_errors or (not self._warned_missing_constraints_path and "save the PCB first" in err):
            wx.MessageBox(err, "Persist constraints", wx.OK | wx.ICON_WARNING)
        if "save the PCB first" in err:
            self._warned_missing_constraints_path = True
        return False

    def update_undo_apply_button(self):
        if hasattr(self, "undo_apply_btn") and self.undo_apply_btn:
            self.undo_apply_btn.Enable(bool(self.apply_undo_stack))

    def _set_solver_status(self, text: str = "", running: bool = False):
        if self.solve_status_label is None:
            return
        if running:
            self.solve_status_label.SetLabel(text)
            self.solve_status_label.Show(bool(text))
        else:
            self.solve_status_label.SetLabel("")
            self.solve_status_label.Hide()
        self.Layout()
        try:
            wx.YieldIfNeeded()
        except Exception:
            pass

    def _set_solver_controls_enabled(self, enabled: bool):
        if self.apply_constraints_btn is not None:
            self.apply_constraints_btn.Enable(enabled)
        if self.debug_apply_btn is not None:
            self.debug_apply_btn.Enable(enabled)
        if self.undo_apply_btn is not None:
            self.undo_apply_btn.Enable(enabled and bool(self.apply_undo_stack))

    def _update_solver_progress(self, pass_number: int, max_passes: int, finished: bool):
        status = f"{pass_number}/{max_passes}"
        if finished:
            status += " done"
        self._set_solver_status(status, running=not finished)

    def _show_copyable_text_dialog(self, title: str, text_value: str, size=(720, 360)):
        dlg = wx.Dialog(self, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(dlg, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=size)
        text.SetValue(text_value)
        dlg_sizer.Add(text, 1, wx.ALL | wx.EXPAND, 8)

        copy_btn = wx.Button(dlg, label="Copy to clipboard")

        def _on_copy(_e):
            data = wx.TextDataObject(text.GetValue())
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(data)
                finally:
                    wx.TheClipboard.Close()

        copy_btn.Bind(wx.EVT_BUTTON, _on_copy)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.Add(copy_btn, 0, wx.RIGHT, 8)
        btn_sizer.Add(wx.Button(dlg, wx.ID_OK, "Close"), 0)
        dlg_sizer.Add(btn_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_RIGHT, 8)
        dlg.SetSizerAndFit(dlg_sizer)
        dlg.SetMinSize((560, 260))
        dlg.CentreOnParent()
        dlg.ShowModal()
        dlg.Destroy()

    def capture_apply_snapshot(self):
        positions = {}
        for fp in self.board.Footprints():
            key = footprint_item_id(fp) or footprint_name(fp)
            if not key:
                continue
            pos = fp.GetPosition()
            x, y = vector2i_xy(pos)
            positions[key] = (x, y)
        return positions

    def push_apply_undo_snapshot(self):
        self.apply_undo_stack.append(self.capture_apply_snapshot())
        if len(self.apply_undo_stack) > self.APPLY_UNDO_LIMIT:
            self.apply_undo_stack = self.apply_undo_stack[-self.APPLY_UNDO_LIMIT :]
        self.update_undo_apply_button()

    def restore_apply_snapshot(self, snapshot):
        for fp in self.board.Footprints():
            key = footprint_item_id(fp) or footprint_name(fp)
            if key not in snapshot:
                continue
            x, y = snapshot[key]
            try:
                fp.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
            except Exception:
                pos = fp.GetPosition()
                cur_x, cur_y = vector2i_xy(pos)
                _move_fp_by_delta(fp, int(x) - cur_x, int(y) - cur_y)

        if hasattr(self.board, "BuildConnectivity"):
            try:
                self.board.BuildConnectivity()
            except Exception:
                pass
        try:
            pcbnew.Refresh()
        except Exception:
            pass

    def set_pick_status(self, text: str):
        self.pick_status_label.SetLabel(text)

    def reset_pick_state(self, status: str = "Edge picker idle."):
        self.pick_stage = PICK_STAGE_IDLE
        self.pick_constrained = None
        self.pick_last_signature = None
        if self.pick_timer.IsRunning():
            self.pick_timer.Stop()
        self.set_pick_status(status)
        self._update_constraint_mode_button_styles()

    def _update_constraint_mode_button_styles(self):
        active_bg = wx.Colour(230, 145, 56)
        active_fg = wx.BLACK
        mode_states = (
            (
                self.view_pcb_constraints_btn,
                self.constraint_filter_mode == CONSTRAINT_FILTER_MODE_PCB_SELECTION,
            ),
            (
                self.show_components_constraints_btn,
                self.constraint_filter_mode == CONSTRAINT_FILTER_MODE_COMPONENTS_SELECTION,
            ),
        )
        for btn, is_active in mode_states:
            if btn is None:
                continue
            if is_active:
                btn.SetBackgroundColour(active_bg)
                btn.SetForegroundColour(active_fg)
            else:
                btn.SetBackgroundColour(wx.NullColour)
                btn.SetForegroundColour(wx.NullColour)
            btn.Refresh()

    def set_ai_status(self, text: str):
        if hasattr(self, "ai_status_label") and self.ai_status_label:
            self.ai_status_label.SetLabel(text)

    def _constraint_identity(self, constraint: dict) -> tuple:
        return (
            constraint.get("constrained", ""),
            constraint.get("constrained_edge", ""),
            constraint.get("anchor", ""),
            constraint.get("anchor_edge", ""),
            constraint.get("relation", ""),
            round(float(constraint.get("gap_mm", 0.0)), 6),
        )

    def _constraint_rule_key(self, constraint: dict) -> tuple:
        return (
            constraint.get("constrained", ""),
            constraint.get("constrained_edge", ""),
            constraint.get("anchor", ""),
            constraint.get("anchor_edge", ""),
        )

    def _is_orientation_component_name(self, name: str) -> bool:
        cleaned = (name or "").strip()
        return bool(cleaned) and not is_board_outline_anchor(cleaned)

    def _flipped_constraint_orientation(self, constraint: dict) -> dict:
        constrained_edge = _normalize_edge_name(constraint.get("constrained_edge", "Right"), "Right")
        anchor_edge = _normalize_optional_edge_name(
            constraint["anchor_edge"] if "anchor_edge" in constraint else "Left",
            "Left",
        )
        relation = constraint.get("relation", "RIGHT_OF")
        if relation not in RELATION_IDS:
            relation = "RIGHT_OF"
        try:
            gap = float(constraint.get("gap_mm", 0.0))
        except (TypeError, ValueError):
            gap = 0.0
        return {
            "constrained": str(constraint.get("anchor", "")).strip(),
            "constrained_edge": _normalize_edge_name(anchor_edge or "Right", "Right"),
            "anchor": str(constraint.get("constrained", "")).strip(),
            "anchor_edge": _normalize_optional_edge_name(constrained_edge, "Left"),
            "relation": _flip_relation_id(relation),
            "gap_mm": gap,
        }

    def _normalize_constraint_orientations(self, constraints: list[dict]) -> list[dict]:
        if not constraints:
            return []

        component_counts: dict[str, int] = {}
        orientation_counts: dict[tuple[str, str], int] = {}
        normalized_rows: list[dict] = []

        for raw in constraints:
            if not isinstance(raw, dict):
                continue
            row = {
                "constrained": str(raw.get("constrained", "")).strip(),
                "constrained_edge": _normalize_edge_name(raw.get("constrained_edge", "Right"), "Right"),
                "anchor": str(raw.get("anchor", "")).strip(),
                "anchor_edge": _normalize_optional_edge_name(
                    raw["anchor_edge"] if "anchor_edge" in raw else "Left",
                    "Left",
                ),
                "relation": raw.get("relation", "RIGHT_OF"),
                "gap_mm": raw.get("gap_mm", 0.0),
            }
            if row["relation"] not in RELATION_IDS:
                row["relation"] = "RIGHT_OF"
            try:
                row["gap_mm"] = float(row.get("gap_mm", 0.0))
            except (TypeError, ValueError):
                row["gap_mm"] = 0.0
            normalized_rows.append(row)

            con_name = row["constrained"]
            anc_name = row["anchor"]
            orientation_counts[(con_name, anc_name)] = orientation_counts.get((con_name, anc_name), 0) + 1
            if self._is_orientation_component_name(con_name):
                component_counts[con_name] = component_counts.get(con_name, 0) + 1
            if self._is_orientation_component_name(anc_name):
                component_counts[anc_name] = component_counts.get(anc_name, 0) + 1

        output: list[dict] = []
        for row in normalized_rows:
            con_name = row["constrained"]
            anc_name = row["anchor"]
            if not con_name or not anc_name or con_name == anc_name:
                output.append(dict(row))
                continue

            same_orient = orientation_counts.get((con_name, anc_name), 0) - 1
            reverse_orient = orientation_counts.get((anc_name, con_name), 0)
            should_flip = False
            if reverse_orient > same_orient:
                should_flip = True
            elif reverse_orient == same_orient:
                con_count = component_counts.get(con_name, 0)
                anc_count = component_counts.get(anc_name, 0)
                if anc_count > con_count:
                    should_flip = True

            output.append(self._flipped_constraint_orientation(row) if should_flip else dict(row))

        return output

    def _format_ai_summary_only(self, summary: str, warnings_text: str) -> str:
        lines = []
        if summary:
            lines.append(f"Summary: {summary}")
        if warnings_text:
            if lines:
                lines.append("")
            lines.append("Warnings:")
            lines.extend(warnings_text.splitlines())
        return "\n".join(lines).strip()

    def _describe_constraint_dict(self, c: dict) -> str:
        return self._constraint_description_from_values(
            c.get("constrained", ""),
            c.get("constrained_edge", ""),
            c.get("anchor", ""),
            c.get("anchor_edge", ""),
            c.get("relation", "RIGHT_OF"),
            f"{float(c.get('gap_mm', 0.0)):.3f}",
        )

    def _clear_ai_result_rows(self):
        self._ai_result_rows = []
        if not hasattr(self, "ai_results_scroll") or self.ai_results_scroll is None:
            return
        self.ai_results_sizer.Clear(True)
        self.ai_results_scroll.Layout()

    def _populate_ai_result_checkboxes(self, constraints: list, summary: str, warnings_text: str):
        self._clear_ai_result_rows()
        self.ai_preview_ctrl.SetValue(self._format_ai_summary_only(summary, warnings_text))

        existing = self._constraints_from_grid() if self.cgrid.GetNumberRows() else list(self.constraints)
        existing = self._normalize_constraint_orientations(existing)
        constraints = self._normalize_constraint_orientations(constraints)
        existing_rule_keys = {self._constraint_rule_key(item) for item in existing}
        existing_identities = {self._constraint_identity(item) for item in existing}

        seen_rule_keys: dict[tuple, int] = {}
        for idx, c in enumerate(constraints):
            row_sizer = wx.BoxSizer(wx.HORIZONTAL)
            cb = wx.CheckBox(self.ai_results_scroll, label="")
            desc = self._describe_constraint_dict(c)

            rule_key = self._constraint_rule_key(c)
            identity = self._constraint_identity(c)
            is_exact_duplicate = identity in existing_identities
            is_overwrite = rule_key in existing_rule_keys and not is_exact_duplicate
            is_duplicate_in_batch = rule_key in seen_rule_keys

            if is_exact_duplicate:
                desc += "  [exact duplicate — will be skipped]"
                cb.SetValue(False)
            elif is_overwrite:
                desc += "  [overwrites existing]"
                cb.SetValue(True)
            elif is_duplicate_in_batch:
                desc += "  [duplicate of row above — will be skipped]"
                cb.SetValue(False)
            else:
                cb.SetValue(True)

            seen_rule_keys[rule_key] = idx

            st = wx.StaticText(self.ai_results_scroll, label=desc)

            if is_exact_duplicate or is_duplicate_in_batch:
                st.SetForegroundColour(wx.Colour(160, 160, 160))
            elif is_overwrite:
                st.SetForegroundColour(wx.Colour(200, 120, 0))

            row_sizer.Add(cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            row_sizer.Add(st, 1, wx.ALIGN_CENTER_VERTICAL)
            self.ai_results_sizer.Add(row_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 4)
            self._ai_result_rows.append({"checkbox": cb, "constraint": c})
        self.ai_results_scroll.Layout()
        self.ai_results_scroll.FitInside()

    def on_clear_ai_prompt(self, _evt):
        self.ai_prompt_ctrl.SetValue("")
        self.ai_preview_ctrl.SetValue("")
        self._clear_ai_result_rows()
        self.set_ai_status("AI prompt cleared.")

    def on_add_ai_constraints_to_table(self, _evt):
        if not self._ai_result_rows:
            wx.MessageBox(
                "Generate constraints first, then tick the rows you want and click Update Constraints Table.",
                "AI Builder",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        selected_raw = [r["constraint"] for r in self._ai_result_rows if r["checkbox"].GetValue()]
        selected_raw = self._normalize_constraint_orientations(selected_raw)
        if not selected_raw:
            wx.MessageBox(
                "Select at least one generated constraint (checkbox) to add.",
                "AI Builder",
                wx.OK | wx.ICON_WARNING,
            )
            return

        selected: list[dict] = []
        seen_keys: set[tuple] = set()
        for c in selected_raw:
            key = self._constraint_rule_key(c)
            if key not in seen_keys:
                selected.append(c)
                seen_keys.add(key)

        self.constraints = self._constraints_from_grid()
        if self.ai_replace_checkbox.GetValue():
            self.constraints = selected
        else:
            new_by_rule_key = {self._constraint_rule_key(c): c for c in selected}
            merged = []
            overwritten = 0
            consumed_keys: set[tuple] = set()
            for existing in self.constraints:
                rule_key = self._constraint_rule_key(existing)
                if rule_key in new_by_rule_key:
                    merged.append(new_by_rule_key[rule_key])
                    consumed_keys.add(rule_key)
                    overwritten += 1
                else:
                    merged.append(existing)
            for c in selected:
                if self._constraint_rule_key(c) not in consumed_keys:
                    merged.append(c)
            self.constraints = merged

            parts = []
            added = len(selected) - overwritten
            if added:
                parts.append(f"{added} added")
            if overwritten:
                parts.append(f"{overwritten} overwritten")
            if parts:
                self.set_ai_status(
                    f"Constraints updated ({', '.join(parts)}). See the Constraints tab."
                )

        self.constraints = self._normalize_constraint_orientations(self.constraints)
        self.constraint_filter_name = None
        self.populate_constraints()
        self.refresh_component_constraint_counts()
        self.persist_constraints(show_errors=True)
        if self.ai_replace_checkbox.GetValue():
            self.set_ai_status(
                f"{len(selected)} constraint(s) replaced in the table."
            )

    def on_generate_ai_constraints(self, _evt):
        prompt_text = self.ai_prompt_ctrl.GetValue().strip()
        if not prompt_text:
            wx.MessageBox(
                "Enter a text prompt describing the constraints you want to generate.",
                "AI Builder",
                wx.OK | wx.ICON_WARNING,
            )
            return

        self.constraints = self._constraints_from_grid()
        component_rows = self.collect_rows()
        self.ai_preview_ctrl.SetValue("")
        self._clear_ai_result_rows()
        self.set_ai_status("Generating constraints with OpenAI...")
        self.ai_generate_btn.Enable(False)

        try:
            wx.BeginBusyCursor()
            generated_constraints, summary, warnings_text = fetch_ai_generated_constraints(
                prompt_text,
                component_rows,
                self.board,
                self.constraints,
            )
        except ValueError as exc:
            self._clear_ai_result_rows()
            self.ai_preview_ctrl.SetValue("")
            self.set_ai_status("AI generation failed.")
            wx.MessageBox(str(exc), "AI Builder", wx.OK | wx.ICON_WARNING)
            return
        finally:
            if wx.IsBusy():
                wx.EndBusyCursor()
            self.ai_generate_btn.Enable(True)

        if not generated_constraints:
            self._clear_ai_result_rows()
            preview = self._format_ai_summary_only(summary, warnings_text)
            extra = "No valid constraints were generated."
            self.ai_preview_ctrl.SetValue(f"{preview}\n\n{extra}".strip() if preview else extra)
            self.set_ai_status("No valid constraints were generated.")
            return

        self._populate_ai_result_checkboxes(generated_constraints, summary, warnings_text)
        self.set_ai_status(
            f"Generated {len(generated_constraints)} constraint(s). "
            "Select checkboxes and click Add constraints to table."
        )

    def _selection_signature(self, item):
        fp = self._pickable_footprint_from_item(item)
        if fp is not None:
            uid = footprint_item_id(fp)
            return f"footprint:{uid or footprint_name(fp)}"

        if hasattr(item, "GetLayer"):
            try:
                if item.GetLayer() == pcbnew.Edge_Cuts:
                    try:
                        uid = item.GetUuid()
                        if hasattr(uid, "AsStdString"):
                            return f"edgecut:{uid.AsStdString()}"
                        if hasattr(uid, "AsString"):
                            return f"edgecut:{uid.AsString()}"
                    except Exception:
                        pass
                    return f"edgecut:{id(item)}"
            except Exception:
                pass

        return str(id(item))

    def get_current_selection_items(self):
        try:
            return list(pcbnew.GetCurrentSelection())
        except Exception:
            pass

        items = []
        for fp in self.board.Footprints():
            try:
                if fp.IsSelected():
                    items.append(fp)
            except Exception:
                pass
        for item in self.board.GetDrawings():
            try:
                if item.IsSelected():
                    items.append(item)
            except Exception:
                pass
        return items

    def _pickable_footprint_from_item(self, item):
        if item is None:
            return None
        if hasattr(item, "GetReference"):
            return item
        if hasattr(item, "GetParentFootprint"):
            try:
                fp = item.GetParentFootprint()
                if fp is not None:
                    return fp
            except Exception:
                pass
        if hasattr(item, "GetParent"):
            try:
                parent = item.GetParent()
                if parent is not None and hasattr(parent, "GetReference"):
                    return parent
            except Exception:
                pass
        return None

    def _pick_candidate_from_item(self, item, allow_edge_cut: bool):
        fp = self._pickable_footprint_from_item(item)
        if fp is not None:
            return {"name": footprint_name(fp), "kind": "footprint", "signature": self._selection_signature(fp)}

        if allow_edge_cut and hasattr(item, "GetLayer"):
            try:
                if item.GetLayer() == pcbnew.Edge_Cuts:
                    return {
                        "name": BOARD_OUTLINE_ANCHOR_DISPLAY,
                        "kind": "edge_cut",
                        "signature": self._selection_signature(item),
                    }
            except Exception:
                pass
        return None

    def _constraint_matches_filter(self, constraint: dict, filter_name: str) -> bool:
        return (
            (constraint.get("constrained") or "").strip() == filter_name
            or (constraint.get("anchor") or "").strip() == filter_name
        )

    def _normalized_constraint_view_row(self, constraint: dict, filter_name: str | None = None) -> tuple[dict, bool]:
        normalized = dict(constraint)
        active_filter = (filter_name if filter_name is not None else self.constraint_filter_name) or ""
        active_filter = active_filter.strip()
        if not active_filter:
            return normalized, False

        constrained_name = (normalized.get("constrained") or "").strip()
        anchor_name = (normalized.get("anchor") or "").strip()
        if anchor_name != active_filter or constrained_name == active_filter:
            return normalized, False

        return (
            {
                "constrained": anchor_name,
                "constrained_edge": normalized.get("anchor_edge", ""),
                "anchor": constrained_name,
                "anchor_edge": normalized.get("constrained_edge", ""),
                "relation": _flip_relation_id(normalized.get("relation", "RIGHT_OF")),
                "gap_mm": float(normalized.get("gap_mm", 0.0)),
            },
            True,
        )

    def _denormalize_constraint_view_row(self, constraint: dict, flipped: bool) -> dict:
        if not flipped:
            return dict(constraint)

        normalized = dict(constraint)
        return {
            "constrained": str(normalized.get("anchor", "")).strip(),
            "constrained_edge": normalized.get("anchor_edge", ""),
            "anchor": str(normalized.get("constrained", "")).strip(),
            "anchor_edge": normalized.get("constrained_edge", ""),
            "relation": _flip_relation_id(normalized.get("relation", "RIGHT_OF")),
            "gap_mm": float(normalized.get("gap_mm", 0.0)),
        }

    def _constraint_sort_key(self, constraint: dict, field: str):
        display_constraint, _ = self._normalized_constraint_view_row(constraint)
        if field == "gap_mm":
            try:
                return float(display_constraint.get("gap_mm", 0.0))
            except (TypeError, ValueError):
                return 0.0
        if field == "description":
            relation_id = str(display_constraint.get("relation", "RIGHT_OF"))
            gap_value = display_constraint.get("gap_mm", 0.0)
            try:
                distance_text = f"{float(gap_value):.3f}"
            except (TypeError, ValueError):
                distance_text = "0.000"
            description = self._constraint_description_from_values(
                str(display_constraint.get("constrained", "")),
                str(display_constraint.get("constrained_edge", "")),
                str(display_constraint.get("anchor", "")),
                str(display_constraint.get("anchor_edge", "")),
                relation_id,
                distance_text,
            )
            return natural_sort_key(description)
        return natural_sort_key(str(display_constraint.get(field, "")))

    def prompt_for_edge(self, target_name: str, stage_label: str):
        dlg = wx.SingleChoiceDialog(
            self,
            f"Select the {stage_label} edge for {target_name}.",
            "Choose edge",
            list(EDGE_POINT_LABELS),
        )
        default_edge = "Right" if self.pick_stage == PICK_STAGE_CONSTRAINED else "Left"
        if default_edge in EDGE_POINT_LABELS:
            dlg.SetSelection(EDGE_POINT_LABELS.index(default_edge))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return dlg.GetStringSelection() or None
        finally:
            dlg.Destroy()

    def prompt_for_relation_distance(self):
        dlg = ConstraintDetailsDialog(self)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return dlg.get_values()
        finally:
            dlg.Destroy()

    def _selected_constraint_source_indices(self):
        visible_rows = [
            row for row in self._selected_grid_rows(self.cgrid)
            if 0 <= row < len(self.constraint_view_indices)
        ]
        return [self.constraint_view_indices[row] for row in visible_rows]

    def _active_constraint_source_index(self):
        row = self.cgrid.GetGridCursorRow()
        if 0 <= row < len(self.constraint_view_indices):
            return self.constraint_view_indices[row]

        selected = self._selected_constraint_source_indices()
        if selected:
            return selected[0]
        return None

    def _visible_rows_for_source_indices(self, source_indices):
        visible = []
        source_set = set(source_indices)
        for visible_row, source_idx in enumerate(self.constraint_view_indices):
            if source_idx in source_set:
                visible.append(visible_row)
        return visible

    def append_constraint_row(self, constrained_name, constrained_edge, anchor_name, anchor_edge, relation_id, distance_text):
        self._populating = True
        try:
            self.cgrid.AppendRows(1)
            row = self.cgrid.GetNumberRows() - 1
            self.cgrid.SetCellValue(row, self.CON_COL_CONSTRAINED, constrained_name)
            self.cgrid.SetCellValue(row, self.CON_COL_CONSTRAINED_EDGE, constrained_edge)
            self.cgrid.SetCellValue(row, self.CON_COL_ANCHOR, anchor_name)
            self.cgrid.SetCellValue(row, self.CON_COL_ANCHOR_EDGE, anchor_edge)
            self.cgrid.SetCellValue(row, self.CON_COL_RELATION, RELATION_LABELS.get(relation_id, RELATION_LABELS["RIGHT_OF"]))
            self.cgrid.SetCellValue(row, self.CON_COL_DISTANCE, distance_text)
        finally:
            self._populating = False

        self.cgrid.SetGridCursor(self.cgrid.GetNumberRows() - 1, 0)
        self.refresh_component_constraint_counts()
        self.refresh_constraint_name_colours()
        self.refresh_constraint_descriptions()
        self.persist_constraints()

    def on_start_pick_constraint(self, _evt):
        self.pick_stage = PICK_STAGE_CONSTRAINED
        self.pick_constrained = None
        self.pick_last_signature = None
        if not self.pick_timer.IsRunning():
            self.pick_timer.Start(200)
        self.set_pick_status("Pick mode: select one constrained component in PCB Editor.")

    def on_start_view_constraint(self, _evt):
        self.constraint_filter_mode = CONSTRAINT_FILTER_MODE_PCB_SELECTION
        self._filter_duplicates_only = False
        self.pick_stage = PICK_STAGE_VIEW
        self.pick_last_signature = None
        if not self.pick_timer.IsRunning():
            self.pick_timer.Start(200)
        self._update_constraint_mode_button_styles()
        self.set_pick_status("View mode: select one component in PCB Editor to filter the constraints table.")

    def _find_duplicate_constraint_indices(self) -> set[int]:
        """Return indices into self.constraints that share the same component pair + axis."""
        rows_by_pair_axis: dict[tuple, list[int]] = {}
        for idx, c in enumerate(self.constraints):
            con = (c.get("constrained") or "").strip().casefold()
            anc = (c.get("anchor") or "").strip().casefold()
            rel = (c.get("relation") or "").strip()
            axis = self._constraint_axis(rel)
            if not con or not anc:
                continue
            pair = tuple(sorted((con, anc)))
            key = (pair, axis)
            rows_by_pair_axis.setdefault(key, []).append(idx)
        dupes: set[int] = set()
        for indices in rows_by_pair_axis.values():
            if len(indices) > 1:
                dupes.update(indices)
        return dupes

    def on_view_duplicates(self, _evt):
        self.constraints = self._constraints_from_grid()
        dupes = self._find_duplicate_constraint_indices()
        if not dupes:
            wx.MessageBox(
                "No duplicate or conflicting constraints found.",
                "View duplicates",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        self.constraint_filter_name = None
        self.constraint_filter_mode = None
        self._filter_duplicates_only = True
        self.populate_constraints()
        self.reset_pick_state(f"Showing {len(dupes)} duplicate/conflicting constraint(s).")

    def on_clear_constraint_view(self, _evt):
        self.constraints = self._constraints_from_grid()
        self.constraint_filter_name = None
        self.constraint_filter_mode = None
        self._filter_duplicates_only = False
        self.populate_constraints()
        self.reset_pick_state("Constraint view cleared.")

    def on_cancel_pick(self, _evt):
        self.reset_pick_state("Edge picker cancelled.")

    def on_pick_timer(self, _event):
        if self.pick_stage == PICK_STAGE_IDLE:
            return

        selection = self.get_current_selection_items()
        if len(selection) != 1:
            if not selection:
                return
            self.set_pick_status("Pick mode: select exactly one item.")
            return

        allow_edge_cut = self.pick_stage == PICK_STAGE_ANCHOR
        candidate = self._pick_candidate_from_item(selection[0], allow_edge_cut=allow_edge_cut)
        if candidate is None:
            if self.pick_stage == PICK_STAGE_CONSTRAINED:
                self.set_pick_status("Pick mode: select a footprint for the constrained item.")
            elif self.pick_stage == PICK_STAGE_VIEW:
                self.set_pick_status("View mode: select a footprint in PCB Editor.")
            else:
                self.set_pick_status("Pick mode: select a footprint or Edge.Cuts item for the anchor.")
            return

        signature = candidate["signature"]
        if signature == self.pick_last_signature:
            return
        self.pick_last_signature = signature

        if self.pick_stage == PICK_STAGE_VIEW:
            if candidate["kind"] != "footprint":
                self.set_pick_status("View mode: select a footprint in PCB Editor.")
                return
            self.constraints = self._constraints_from_grid()
            self.constraint_filter_mode = CONSTRAINT_FILTER_MODE_PCB_SELECTION
            self._filter_duplicates_only = False
            self.constraint_filter_name = candidate["name"]
            self.populate_constraints()
            if self._constraint_drawings:
                self._refresh_constraint_drawings_if_visible()
            self._update_constraint_mode_button_styles()
            self.set_pick_status(
                f"Viewing constraints for {candidate['name']}. Select another PCB component to re-filter."
            )
            return

        stage_label = "constrained" if self.pick_stage == PICK_STAGE_CONSTRAINED else "anchor"
        picked_edge = self.prompt_for_edge(candidate["name"], stage_label)
        if not picked_edge:
            self.reset_pick_state("Edge picker cancelled.")
            return

        if self.pick_stage == PICK_STAGE_CONSTRAINED:
            self.pick_constrained = {"name": candidate["name"], "edge": picked_edge}
            self.pick_stage = PICK_STAGE_ANCHOR
            self.set_pick_status(
                f"Picked constrained edge on {candidate['name']}. Now select the anchor item in PCB Editor."
            )
            return

        if not self.pick_constrained:
            self.reset_pick_state("Edge picker reset.")
            return

        relation_distance = self.prompt_for_relation_distance()
        if not relation_distance:
            self.reset_pick_state("Edge picker cancelled.")
            return

        relation_id, distance_text = relation_distance
        self.append_constraint_row(
            self.pick_constrained["name"],
            self.pick_constrained["edge"],
            candidate["name"],
            picked_edge,
            relation_id,
            distance_text,
        )
        self.reset_pick_state(
            f"Added constraint: {self.pick_constrained['name']} -> {candidate['name']}."
        )

    def _component_display_names(self):
        if self.component_rows:
            rows = self.component_rows
        else:
            rows = self.collect_rows()
        names = [row.get("display_name", row["values"][0]) for row in rows if not row.get("is_edge_cut")]
        return sorted(names, key=natural_sort_key)

    def _anchor_display_names(self):
        names = self._component_display_names()
        return [BOARD_OUTLINE_ANCHOR_DISPLAY] + names

    def refresh_constraint_choice_editors(self):
        constrained_choices = self._component_display_names()
        anchor_choices = self._anchor_display_names()

        constrained_attr = gridlib.GridCellAttr()
        constrained_attr.SetEditor(gridlib.GridCellChoiceEditor(constrained_choices, True))
        self.cgrid.SetColAttr(0, constrained_attr)

        anchor_attr = gridlib.GridCellAttr()
        anchor_attr.SetEditor(gridlib.GridCellChoiceEditor(anchor_choices, True))
        self.cgrid.SetColAttr(2, anchor_attr)

    def _constraint_description_from_values(
        self,
        constrained_name: str,
        constrained_edge: str,
        anchor_name: str,
        anchor_edge: str,
        relation_id: str,
        distance_text: str,
    ) -> str:
        if not constrained_name or not constrained_edge or not anchor_name or not anchor_edge:
            return ""

        constrained_label = EDGE_DESCRIPTION_LABELS.get(constrained_edge, f"{constrained_edge} edge")
        anchor_label = EDGE_DESCRIPTION_LABELS.get(anchor_edge, f"{anchor_edge} edge")
        relation_label = RELATION_DESCRIPTION_LABELS.get(relation_id, "from")
        distance_text = (distance_text or "").strip()

        if distance_text:
            return (
                f"The {constrained_label} of {constrained_name} is "
                f"{distance_text}mm {relation_label} the {anchor_label} of {anchor_name}."
            )

        return (
            f"The {constrained_label} of {constrained_name} is "
            f"{relation_label} the {anchor_label} of {anchor_name}."
        )

    def refresh_constraint_descriptions(self):
        if self.cgrid.GetNumberRows() <= 0:
            return

        self._populating = True
        try:
            for row in range(self.cgrid.GetNumberRows()):
                constrained_name = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED).strip()
                constrained_edge = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED_EDGE).strip()
                anchor_name = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR).strip()
                anchor_edge = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR_EDGE).strip()
                relation_label = self.cgrid.GetCellValue(row, self.CON_COL_RELATION).strip()
                relation_id = LABEL_TO_ID.get(relation_label, "RIGHT_OF")
                distance_text = self.cgrid.GetCellValue(row, self.CON_COL_DISTANCE).strip()
                description = self._constraint_description_from_values(
                    constrained_name,
                    constrained_edge,
                    anchor_name,
                    anchor_edge,
                    relation_id,
                    distance_text,
                )
                self.cgrid.SetCellValue(row, self.CON_COL_DESCRIPTION, description)
                self.cgrid.SetReadOnly(row, self.CON_COL_DESCRIPTION, True)
        finally:
            self._populating = False

    @staticmethod
    def _constraint_axis(relation: str) -> str:
        if relation in ("RIGHT_OF", "LEFT_OF", "ALIGN_HORIZONTALLY"):
            return "x"
        if relation in ("ABOVE", "BELOW", "ALIGN_VERTICALLY"):
            return "y"
        return "?"

    def _find_conflict_rows(self) -> set[int]:
        """Find rows where the same component pair is constrained on the same axis more than once.

        Two rows conflict when they share the same unordered pair of components AND
        the same movement axis (X for RIGHT_OF/LEFT_OF/ALIGN_HORIZONTALLY,
        Y for ABOVE/BELOW/ALIGN_VERTICALLY).
        """
        n = self.cgrid.GetNumberRows()
        if n < 2:
            return set()

        rows_by_pair_axis: dict[tuple, list[int]] = {}
        for row in range(n):
            con = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED).strip().casefold()
            anc = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR).strip().casefold()
            rel_label = self.cgrid.GetCellValue(row, self.CON_COL_RELATION).strip()
            rel_id = LABEL_TO_ID.get(rel_label, rel_label.upper().replace(" ", "_"))
            axis = self._constraint_axis(rel_id or "")
            if not con or not anc:
                continue
            pair = tuple(sorted((con, anc)))
            key = (pair, axis)
            rows_by_pair_axis.setdefault(key, []).append(row)

        conflict_rows: set[int] = set()
        for rows in rows_by_pair_axis.values():
            if len(rows) > 1:
                conflict_rows.update(rows)
        return conflict_rows

    def refresh_constraint_name_colours(self):
        if self.cgrid.GetNumberRows() <= 0:
            return

        component_names = set(self._component_display_names())
        anchor_names = set(self._anchor_display_names())
        default_colour = self.cgrid.GetDefaultCellTextColour()
        default_bg = self.cgrid.GetDefaultCellBackgroundColour()
        conflict_bg = wx.Colour(180, 140, 20)
        conflict_rows = self._find_conflict_rows()

        self._populating = True
        try:
            for row in range(self.cgrid.GetNumberRows()):
                constrained_name = self.cgrid.GetCellValue(row, 0).strip()
                anchor_name = self.cgrid.GetCellValue(row, 2).strip()

                constrained_colour = default_colour
                anchor_colour = default_colour

                if constrained_name and constrained_name not in component_names:
                    constrained_colour = wx.RED
                if anchor_name and anchor_name not in anchor_names and not is_board_outline_anchor(anchor_name):
                    anchor_colour = wx.RED

                self.cgrid.SetCellTextColour(row, 0, constrained_colour)
                self.cgrid.SetCellTextColour(row, 2, anchor_colour)

                bg = conflict_bg if row in conflict_rows else default_bg
                for col in range(self.cgrid.GetNumberCols()):
                    self.cgrid.SetCellBackgroundColour(row, col, bg)
        finally:
            self._populating = False
        self.cgrid.ForceRefresh()

    def _constraint_counts_by_ref(self):
        constraints = self._constraints_from_grid() if self.cgrid.GetNumberRows() else self.constraints
        counts = {}
        for c in constraints:
            names = {
                (c.get("constrained") or "").strip(),
                (c.get("anchor") or "").strip(),
            }
            for name in names:
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + 1
        return counts

    def refresh_component_constraint_counts(self):
        if not self.component_rows or self.grid.GetNumberRows() != len(self.component_rows):
            return
        counts = self._constraint_counts_by_ref()
        self._populating = True
        try:
            for row_idx, row in enumerate(self.component_rows):
                value = "0" if row.get("is_edge_cut") else str(
                    counts.get(row.get("display_name", row["values"][0]), 0)
                )
                row["values"][self.COL_CONSTRAINTS] = value
                self.grid.SetCellValue(row_idx, self.COL_CONSTRAINTS, value)
        finally:
            self._populating = False

    def _axis_for_edge(self, edge: str) -> str:
        if edge in _HORIZONTAL_EDGES:
            return "h"
        if edge in _VERTICAL_EDGES:
            return "v"
        return ""

    def _anchor_edge_choices_for_constrained_edge(self, constrained_edge: str) -> list[str]:
        axis = self._axis_for_edge(constrained_edge)
        if axis == "h":
            return ["", *_HORIZONTAL_EDGES, "Center"]
        if axis == "v":
            return ["", *_VERTICAL_EDGES, "Center"]
        return ["", *EDGE_POINT_LABELS]

    def _relation_choices_for_constraint_row(self, constrained_edge: str, anchor_edge: str) -> list[str]:
        axis = self._axis_for_edge(constrained_edge) or self._axis_for_edge(anchor_edge)
        if axis == "h":
            return [RELATION_LABELS[k] for k in _HORIZONTAL_RELATIONS]
        if axis == "v":
            return [RELATION_LABELS[k] for k in _VERTICAL_RELATIONS]
        return [RELATION_LABELS[k] for k in RELATION_IDS]

    @staticmethod
    def _relation_forces_zero_gap(relation_id: str) -> bool:
        return relation_id in ("ALIGN_HORIZONTALLY", "ALIGN_VERTICALLY")

    def _apply_constraint_row_editors(self, row: int):
        con_edge = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED_EDGE)
        anc_edge = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR_EDGE)
        con_choices = list(EDGE_POINT_LABELS)
        anc_choices = self._anchor_edge_choices_for_constrained_edge(con_edge)
        rel_choices = self._relation_choices_for_constraint_row(con_edge, anc_edge)

        con_attr = gridlib.GridCellAttr()
        con_attr.SetEditor(gridlib.GridCellChoiceEditor(con_choices))
        self.cgrid.SetAttr(row, self.CON_COL_CONSTRAINED_EDGE, con_attr)

        anc_attr = gridlib.GridCellAttr()
        anc_attr.SetEditor(gridlib.GridCellChoiceEditor(anc_choices))
        self.cgrid.SetAttr(row, self.CON_COL_ANCHOR_EDGE, anc_attr)

        rel_attr = gridlib.GridCellAttr()
        rel_attr.SetEditor(gridlib.GridCellChoiceEditor(rel_choices))
        self.cgrid.SetAttr(row, self.CON_COL_RELATION, rel_attr)

    def _coerce_constraint_row_axis(self, row: int, changed_col: int):
        con_edge = self.cgrid.GetCellValue(row, self.CON_COL_CONSTRAINED_EDGE)
        anc_edge = self.cgrid.GetCellValue(row, self.CON_COL_ANCHOR_EDGE)
        rel_label = self.cgrid.GetCellValue(row, self.CON_COL_RELATION)
        rel_id = LABEL_TO_ID.get(rel_label, "")

        valid_anchor_edges = set(self._anchor_edge_choices_for_constrained_edge(con_edge))
        if anc_edge not in valid_anchor_edges:
            self.cgrid.SetCellValue(row, self.CON_COL_ANCHOR_EDGE, "")
            anc_edge = ""

        rel_choices = self._relation_choices_for_constraint_row(con_edge, anc_edge)
        valid_rels = {LABEL_TO_ID[label] for label in rel_choices}
        if rel_choices and rel_id not in valid_rels:
            self.cgrid.SetCellValue(row, self.CON_COL_RELATION, rel_choices[0])
            rel_id = LABEL_TO_ID.get(rel_choices[0], rel_id)

        if self._relation_forces_zero_gap(rel_id or ""):
            self.cgrid.SetCellValue(row, self.CON_COL_DISTANCE, "0.000")

        self._apply_constraint_row_editors(row)

    def on_any_grid_changed(self, event):
        if not self._populating:
            if event.GetEventObject() == self.grid:
                self.on_components_grid_cell_changed(event)
            else:
                row = event.GetRow()
                col = event.GetCol()
                if col in (
                    self.CON_COL_CONSTRAINED_EDGE,
                    self.CON_COL_ANCHOR_EDGE,
                    self.CON_COL_RELATION,
                    self.CON_COL_DISTANCE,
                ):
                    self._coerce_constraint_row_axis(row, col)
                self.constraints = self._constraints_from_grid()
                self.populate_constraints()
                self.refresh_component_constraint_counts()
                self.refresh_constraint_name_colours()
                self.refresh_constraint_descriptions()
                self.persist_constraints()
        event.Skip()

    def _component_move_delta_from_value(self, fp, col: int, value: str):
        try:
            target_iu = pcbnew.FromMM(float(value))
        except (TypeError, ValueError):
            raise ValueError("Enter a numeric value in millimeters.")

        try:
            bbox = fp.GetBoundingBox(False, False)
        except Exception:
            bbox = fp.GetBoundingBox()
        left, right = _bbox_x_edges(bbox)
        top, bottom = _bbox_y_edges(bbox)
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2

        if col == self.COL_X_LEFT:
            return target_iu - left, 0
        if col == self.COL_X_RIGHT:
            return target_iu - right, 0
        if col == self.COL_Y_TOP:
            return 0, target_iu - top
        if col == self.COL_Y_BOTTOM:
            return 0, target_iu - bottom
        if col == self.COL_X_CENTER:
            return target_iu - center_x, 0
        if col == self.COL_Y_CENTER:
            return 0, target_iu - center_y
        return None

    def _edge_cut_move_delta(self, col: int, value: str):
        try:
            target_iu = pcbnew.FromMM(float(value))
        except (TypeError, ValueError):
            raise ValueError("Enter a numeric value in millimeters.")

        bbox = board_outline_bbox(self.board)
        if bbox is None:
            raise ValueError("No Edge.Cuts outline found on the board.")

        left = bbox.GetLeft()
        right = bbox.GetRight()
        top = bbox.GetTop()
        bottom = bbox.GetBottom()
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2

        if col == self.COL_X_LEFT:
            return target_iu - left, 0
        if col == self.COL_X_RIGHT:
            return target_iu - right, 0
        if col == self.COL_Y_TOP:
            return 0, target_iu - top
        if col == self.COL_Y_BOTTOM:
            return 0, target_iu - bottom
        if col == self.COL_X_CENTER:
            return target_iu - center_x, 0
        if col == self.COL_Y_CENTER:
            return 0, target_iu - center_y
        return None

    def _move_edge_cuts(self, dx: int, dy: int):
        if dx == 0 and dy == 0:
            return
        delta = pcbnew.VECTOR2I(int(dx), int(dy))
        for item in self.board.GetDrawings():
            try:
                if item.GetLayer() == pcbnew.Edge_Cuts:
                    if hasattr(item, "Move"):
                        item.Move(delta)
                    else:
                        pos = item.GetPosition()
                        x, y = vector2i_xy(pos)
                        item.SetPosition(pcbnew.VECTOR2I(x + int(dx), y + int(dy)))
            except Exception:
                continue

    def apply_components_cell_edit(self, row: int, col: int, value: str) -> bool:
        if row < 0 or row >= len(self.component_rows):
            return False

        row_data = self.component_rows[row]

        if row_data.get("is_edge_cut"):
            delta = self._edge_cut_move_delta(col, value)
            if delta is None:
                return False
            dx, dy = delta
            self._move_edge_cuts(dx, dy)
        else:
            fp = row_data.get("footprint")
            if fp is None:
                return False
            delta = self._component_move_delta_from_value(fp, col, value)
            if delta is None:
                return False
            dx, dy = delta
            _move_fp_by_delta(fp, dx, dy)

        if hasattr(self.board, "BuildConnectivity"):
            try:
                self.board.BuildConnectivity()
            except Exception:
                pass
        try:
            pcbnew.Refresh()
        except Exception:
            pass
        return True

    def on_components_grid_cell_changed(self, event):
        row = event.GetRow()
        col = event.GetCol()
        value = self.grid.GetCellValue(row, col)

        try:
            moved = self.apply_components_cell_edit(row, col, value)
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid component coordinate", wx.OK | wx.ICON_WARNING)
            self.populate()
            return

        if moved:
            self.populate()
        else:
            self._mark_dirty()

    def _grid_selection_bounds(self, grid):
        top_left = grid.GetSelectionBlockTopLeft()
        bottom_right = grid.GetSelectionBlockBottomRight()
        if top_left and bottom_right:
            tl = top_left[0]
            br = bottom_right[0]
            return tl[0], tl[1], br[0], br[1]

        row = grid.GetGridCursorRow()
        col = grid.GetGridCursorCol()
        if row >= 0 and col >= 0:
            return row, col, row, col
        return None

    def _selected_grid_rows(self, grid):
        rows = set()

        for row in grid.GetSelectedRows():
            rows.add(row)

        top_left = grid.GetSelectionBlockTopLeft()
        bottom_right = grid.GetSelectionBlockBottomRight()
        for tl, br in zip(top_left, bottom_right):
            for row in range(tl[0], br[0] + 1):
                rows.add(row)

        if not rows:
            row = grid.GetGridCursorRow()
            if row >= 0:
                rows.add(row)

        return sorted(rows)

    def _constraint_grid_rows_data(self):
        data = []
        for row in range(self.cgrid.GetNumberRows()):
            data.append(
                [self.cgrid.GetCellValue(row, col) for col in range(self.cgrid.GetNumberCols())]
            )
        return data

    def _write_constraint_grid_rows_data(self, data, selected_rows=None):
        self._populating = True
        try:
            current_rows = self.cgrid.GetNumberRows()
            if current_rows:
                self.cgrid.DeleteRows(0, current_rows)
            if data:
                self.cgrid.AppendRows(len(data))
            for row_idx, row_values in enumerate(data):
                for col_idx, value in enumerate(row_values):
                    self.cgrid.SetCellValue(row_idx, col_idx, value)
                    if col_idx == self.CON_COL_DESCRIPTION:
                        self.cgrid.SetReadOnly(row_idx, col_idx, True)
        finally:
            self._populating = False

        self.cgrid.ClearSelection()
        if selected_rows:
            for row in selected_rows:
                if 0 <= row < self.cgrid.GetNumberRows():
                    self.cgrid.SelectRow(row, addToSelected=True)
            first_row = selected_rows[0]
            if 0 <= first_row < self.cgrid.GetNumberRows():
                self.cgrid.SetGridCursor(first_row, 0)

        self.refresh_component_constraint_counts()
        self.refresh_constraint_name_colours()
        self.refresh_constraint_descriptions()
        self.persist_constraints()

    def _move_selected_constraint_rows(self, direction: int):
        self.constraints = self._constraints_from_grid()
        selected_indices = self._selected_constraint_source_indices()
        if not selected_indices:
            return

        data = list(self.constraints)
        selected_set = set(selected_indices)

        if direction < 0:
            for row in sorted(selected_indices):
                if row > 0 and (row - 1) not in selected_set:
                    data[row - 1], data[row] = data[row], data[row - 1]
                    selected_set.remove(row)
                    selected_set.add(row - 1)
        else:
            for row in reversed(sorted(selected_indices)):
                if row < len(data) - 1 and (row + 1) not in selected_set:
                    data[row + 1], data[row] = data[row], data[row + 1]
                    selected_set.remove(row)
                    selected_set.add(row + 1)

        self.constraints = data
        self.populate_constraints()
        visible_rows = self._visible_rows_for_source_indices(sorted(selected_set))
        self.cgrid.ClearSelection()
        for row in visible_rows:
            self.cgrid.SelectRow(row, addToSelected=True)
        if visible_rows:
            self.cgrid.SetGridCursor(visible_rows[0], 0)
        self.persist_constraints()

    def _sort_component_rows_by_col(self, col: int, ascending: bool = True):
        ec_rows = [r for r in self.component_rows if r.get("is_edge_cut")]
        fp_rows = [r for r in self.component_rows if not r.get("is_edge_cut")]

        numeric_cols = {
            self.COL_CONSTRAINTS, self.COL_WIDTH, self.COL_HEIGHT,
            self.COL_X_LEFT, self.COL_X_RIGHT, self.COL_Y_TOP,
            self.COL_Y_BOTTOM, self.COL_X_CENTER, self.COL_Y_CENTER,
        }

        if col in numeric_cols:
            def _num_key(r):
                try:
                    return float(r["values"][col])
                except (ValueError, TypeError, IndexError):
                    return 0.0
            fp_rows.sort(key=_num_key, reverse=not ascending)
        else:
            fp_rows.sort(
                key=lambda r: natural_sort_key(str(r["values"][col]) if col < len(r["values"]) else ""),
                reverse=not ascending,
            )

        self.component_rows = ec_rows + fp_rows
        self._repopulate_component_grid()

    def _repopulate_component_grid(self):
        self._populating = True
        try:
            constraint_counts = self._constraint_counts_by_ref()
            existing = self.grid.GetNumberRows()
            if existing:
                self.grid.DeleteRows(0, existing)
            rows = self.component_rows
            if rows:
                self.grid.AppendRows(len(rows))
            for r, row in enumerate(rows):
                if not row.get("is_edge_cut"):
                    row["values"][self.COL_CONSTRAINTS] = str(
                        constraint_counts.get(row.get("display_name", row["values"][0]), 0)
                    )
                for c, value in enumerate(row["values"]):
                    self.grid.SetCellValue(r, c, str(value))
                if row.get("is_edge_cut"):
                    self.grid.SetReadOnly(r, self.COL_NAME, True)
                    self.grid.SetReadOnly(r, self.COL_CONSTRAINTS, True)
                    self.grid.SetReadOnly(r, self.COL_WIDTH, True)
                    self.grid.SetReadOnly(r, self.COL_HEIGHT, True)
                    self.grid.SetReadOnly(r, self.COL_LOCKED, True)
                else:
                    self.grid.SetReadOnly(r, self.COL_NAME, True)
                    self.grid.SetReadOnly(r, self.COL_CONSTRAINTS, True)
                    self.grid.SetReadOnly(r, self.COL_WIDTH, True)
                    self.grid.SetReadOnly(r, self.COL_HEIGHT, True)
                    self.grid.SetReadOnly(r, self.COL_LOCKED, True)
        finally:
            self._populating = False
        self.refresh_constraint_name_colours()

    def _update_component_col_sort_indicators(self):
        for i, label in enumerate(self.COLS):
            if i == self._comp_sort_col:
                arrow = " \u25B2" if self._comp_sort_asc else " \u25BC"
                self.grid.SetColLabelValue(i, label + arrow)
            else:
                self.grid.SetColLabelValue(i, label)

    def on_component_col_header_click(self, evt):
        col = evt.GetCol()
        if col < 0:
            evt.Skip()
            return
        if self._comp_sort_col == col:
            self._comp_sort_asc = not self._comp_sort_asc
        else:
            self._comp_sort_col = col
            self._comp_sort_asc = True
        self._sort_component_rows_by_col(col, self._comp_sort_asc)
        self._update_component_col_sort_indicators()

    _CON_COL_SORT_KEYS = {
        0: "constrained",
        1: "constrained_edge",
        2: "anchor",
        3: "anchor_edge",
        4: "relation",
        5: "gap_mm",
        6: "description",
    }

    def _sort_constraints_by_col(self, col: int, ascending: bool = True):
        field = self._CON_COL_SORT_KEYS.get(col)
        if not field:
            return
        self.constraints = self._constraints_from_grid()
        self.constraints.sort(
            key=lambda c: self._constraint_sort_key(c, field),
            reverse=not ascending,
        )
        self.populate_constraints()
        if self.cgrid.GetNumberRows() > 0:
            self.cgrid.SetGridCursor(0, 0)
        self.persist_constraints()

    def _update_constraint_col_sort_indicators(self):
        for i, label in enumerate(self.CON_COLS):
            if i == self._con_sort_col:
                arrow = " \u25B2" if self._con_sort_asc else " \u25BC"
                self.cgrid.SetColLabelValue(i, label + arrow)
            else:
                self.cgrid.SetColLabelValue(i, label)

    def on_constraint_col_header_click(self, evt):
        col = evt.GetCol()
        if col < 0:
            evt.Skip()
            return
        if col not in self._CON_COL_SORT_KEYS:
            evt.Skip()
            return
        if self._con_sort_col == col:
            self._con_sort_asc = not self._con_sort_asc
        else:
            self._con_sort_col = col
            self._con_sort_asc = True
        self._sort_constraints_by_col(col, self._con_sort_asc)
        self._update_constraint_col_sort_indicators()

    def on_sort_constraints_az(self, _evt):
        self._con_sort_col = 0
        self._con_sort_asc = True
        self._sort_constraints_by_col(0, True)
        self._update_constraint_col_sort_indicators()

    def _copy_grid_selection(self, grid):
        bounds = self._grid_selection_bounds(grid)
        if bounds is None:
            return

        top, left, bottom, right = bounds
        lines = []
        for row in range(top, bottom + 1):
            values = []
            for col in range(left, right + 1):
                values.append(grid.GetCellValue(row, col))
            lines.append("\t".join(values))

        data = wx.TextDataObject("\n".join(lines))
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(data)
            finally:
                wx.TheClipboard.Close()

    def copy_components_selection(self):
        self._copy_grid_selection(self.grid)

    def paste_components_selection(self):
        if not wx.TheClipboard.Open():
            return
        try:
            data = wx.TextDataObject()
            if not wx.TheClipboard.GetData(data):
                return
            text = data.GetText()
        finally:
            wx.TheClipboard.Close()

        if not text:
            return

        start_row = self.grid.GetGridCursorRow()
        start_col = self.grid.GetGridCursorCol()
        if start_row < 0 or start_col < 0:
            return

        rows = [line.rstrip("\r") for line in text.splitlines()]
        changed = False
        invalid = False
        try:
            for row_offset, line in enumerate(rows):
                target_row = start_row + row_offset
                if target_row >= self.grid.GetNumberRows():
                    break
                cells = line.split("\t")
                for col_offset, value in enumerate(cells):
                    target_col = start_col + col_offset
                    if target_col >= self.grid.GetNumberCols():
                        break
                    if self.grid.IsReadOnly(target_row, target_col):
                        continue
                    try:
                        moved = self.apply_components_cell_edit(target_row, target_col, value)
                    except ValueError:
                        invalid = True
                        continue
                    if moved:
                        changed = True
                    else:
                        self.grid.SetCellValue(target_row, target_col, value)
                        changed = True
        finally:
            pass

        if changed:
            self.populate()
        if invalid:
            wx.MessageBox(
                "One or more pasted values were invalid. Use numeric millimeter values for X/Y cells.",
                "Paste components",
                wx.OK | wx.ICON_WARNING,
            )

    def on_components_grid_key_down(self, event):
        key = event.GetKeyCode()
        if event.ControlDown() and key in (ord("C"), ord("c")):
            self.copy_components_selection()
            return
        if event.ControlDown() and key in (ord("V"), ord("v")):
            self.paste_components_selection()
            return
        event.Skip()

    def paste_constraints_selection(self):
        if not wx.TheClipboard.Open():
            return
        try:
            data = wx.TextDataObject()
            if not wx.TheClipboard.GetData(data):
                return
            text = data.GetText()
        finally:
            wx.TheClipboard.Close()

        if not text:
            return

        bounds = self._grid_selection_bounds(self.cgrid)
        if bounds is None:
            return
        sel_top, sel_left, sel_bottom, sel_right = bounds

        start_row = self.cgrid.GetGridCursorRow()
        start_col = self.cgrid.GetGridCursorCol()
        if start_row < 0 or start_col < 0:
            start_row, start_col = sel_top, sel_left

        rows = [line.rstrip("\r") for line in text.splitlines()]
        cells_2d = [row.split("\t") for row in rows]
        single_value_paste = (
            len(cells_2d) == 1
            and len(cells_2d[0]) == 1
            and sel_bottom >= sel_top
            and sel_right >= sel_left
            and (sel_bottom > sel_top or sel_right > sel_left)
        )

        self._populating = True
        try:
            if single_value_paste:
                value = cells_2d[0][0]
                for target_row in range(sel_top, sel_bottom + 1):
                    for target_col in range(sel_left, sel_right + 1):
                        if self.cgrid.IsReadOnly(target_row, target_col):
                            continue
                        self.cgrid.SetCellValue(target_row, target_col, value)
            else:
                for row_offset, cells in enumerate(cells_2d):
                    target_row = start_row + row_offset
                    if target_row >= self.cgrid.GetNumberRows():
                        break
                    for col_offset, value in enumerate(cells):
                        target_col = start_col + col_offset
                        if target_col >= self.cgrid.GetNumberCols():
                            break
                        if self.cgrid.IsReadOnly(target_row, target_col):
                            continue
                        self.cgrid.SetCellValue(target_row, target_col, value)
        finally:
            self._populating = False

        self.refresh_component_constraint_counts()
        self.refresh_constraint_name_colours()
        self.refresh_constraint_descriptions()
        self.persist_constraints()

    def on_constraints_grid_key_down(self, event):
        key = event.GetKeyCode()
        if event.ControlDown() and key in (ord("C"), ord("c")):
            self._copy_grid_selection(self.cgrid)
            return
        if event.ControlDown() and key in (ord("V"), ord("v")):
            self.paste_constraints_selection()
            return
        event.Skip()

    def _constraints_from_grid(self) -> list:
        view_rows = []
        n = self.cgrid.GetNumberRows()
        for r in range(n):
            con = self.cgrid.GetCellValue(r, 0).strip()
            con_edge = self.cgrid.GetCellValue(r, 1).strip() or "Right"
            anc = self.cgrid.GetCellValue(r, 2).strip()
            anc_edge = self.cgrid.GetCellValue(r, 3).strip()
            rel_label = self.cgrid.GetCellValue(r, 4).strip()
            rel = LABEL_TO_ID.get(rel_label, "RIGHT_OF")
            gap_s = self.cgrid.GetCellValue(r, 5).strip()
            try:
                gap = float(gap_s) if gap_s else 0.0
            except ValueError:
                gap = 0.0
            if rel in ("ALIGN_HORIZONTALLY", "ALIGN_VERTICALLY"):
                gap = 0.0
            view_rows.append(
                {
                    "constrained": con,
                    "constrained_edge": con_edge if con_edge in EDGE_POINT_LABELS else "Right",
                    "anchor": anc,
                    "anchor_edge": anc_edge if anc_edge in EDGE_POINT_LABELS else "",
                    "relation": rel,
                    "gap_mm": gap,
                }
            )
        if not self.constraint_filter_name and not self._filter_duplicates_only:
            return self._normalize_constraint_orientations(view_rows)

        merged = list(self.constraints)
        flipped_flags = (
            list(self.constraint_view_flipped)
            if len(self.constraint_view_flipped) == len(view_rows)
            else [False] * len(view_rows)
        )
        for view_idx, source_idx in enumerate(self.constraint_view_indices):
            if view_idx < len(view_rows) and 0 <= source_idx < len(merged):
                merged[source_idx] = self._denormalize_constraint_view_row(
                    view_rows[view_idx],
                    flipped_flags[view_idx],
                )
        return self._normalize_constraint_orientations(merged)

    def populate_constraints(self):
        self._populating = True
        try:
            self.refresh_constraint_choice_editors()
            visible_constraints = []
            self.constraint_view_indices = []
            self.constraint_view_flipped = []
            dupe_indices = self._find_duplicate_constraint_indices() if self._filter_duplicates_only else None
            for idx, constraint in enumerate(self.constraints):
                if dupe_indices is not None:
                    if idx not in dupe_indices:
                        continue
                elif self.constraint_filter_name and not self._constraint_matches_filter(
                    constraint, self.constraint_filter_name
                ):
                    continue
                display_constraint = dict(constraint)
                flipped = False
                if self.constraint_filter_name and dupe_indices is None:
                    display_constraint, flipped = self._normalized_constraint_view_row(
                        constraint,
                        self.constraint_filter_name,
                    )
                self.constraint_view_indices.append(idx)
                self.constraint_view_flipped.append(flipped)
                visible_constraints.append(display_constraint)

            n = len(visible_constraints)
            cur = self.cgrid.GetNumberRows()
            if cur:
                self.cgrid.DeleteRows(0, cur)
            if n:
                self.cgrid.AppendRows(n)

            for r, c in enumerate(visible_constraints):
                self.cgrid.SetCellValue(r, 0, c.get("constrained", ""))
                con_edge = c.get("constrained_edge", "Right")
                if con_edge not in EDGE_POINT_LABELS:
                    con_edge = "Right"
                self.cgrid.SetCellValue(r, 1, con_edge)
                self.cgrid.SetCellValue(r, 2, c.get("anchor", ""))
                anc_edge = _normalize_optional_edge_name(
                    c["anchor_edge"] if "anchor_edge" in c else "Left",
                    "Left",
                )
                self.cgrid.SetCellValue(r, 3, anc_edge)
                rid = c.get("relation", "RIGHT_OF")
                if rid not in RELATION_IDS:
                    rid = "RIGHT_OF"
                self.cgrid.SetCellValue(r, 4, RELATION_LABELS[rid])
                g = float(c.get("gap_mm", 0))
                self.cgrid.SetCellValue(r, 5, f"{g:.3f}")
                self._coerce_constraint_row_axis(r, self.CON_COL_CONSTRAINED_EDGE)

            cw = [260, 120, 140, 120, 180, 90, 520]
            for i, w in enumerate(cw):
                self.cgrid.SetColSize(i, w)
        finally:
            self._populating = False
        self.refresh_constraint_name_colours()
        self.refresh_constraint_descriptions()

    def on_add_constraint_row(self, _evt):
        self.constraints = self._constraints_from_grid()
        new_constraint = {
            "constrained": self.constraint_filter_name or "",
            "constrained_edge": "Right",
            "anchor": "",
            "anchor_edge": "Left",
            "relation": "RIGHT_OF",
            "gap_mm": 0.0,
        }
        self.constraints.append(new_constraint)
        self.populate_constraints()
        if self.cgrid.GetNumberRows() > 0:
            last_row = self.cgrid.GetNumberRows() - 1
            self.cgrid.SetGridCursor(last_row, 0)
        self.persist_constraints()

    def on_copy_constraint_row(self, _evt):
        self.constraints = self._constraints_from_grid()
        source_indices = self._selected_constraint_source_indices()
        if not source_indices:
            return

        copied_rows = []
        for src in source_indices:
            copied_rows.append(dict(self.constraints[src]))

        insert_at = max(source_indices) + 1
        for row_offset, copied_row in enumerate(copied_rows):
            self.constraints.insert(insert_at + row_offset, copied_row)
        self.populate_constraints()
        visible_rows = self._visible_rows_for_source_indices(
            list(range(insert_at, insert_at + len(copied_rows)))
        )
        if visible_rows:
            self.cgrid.ClearSelection()
            for row in visible_rows:
                self.cgrid.SelectRow(row, addToSelected=True)
            self.cgrid.SetGridCursor(visible_rows[0], 0)
        self.persist_constraints()

    def on_move_constraint_rows_up(self, _evt):
        self._move_selected_constraint_rows(-1)

    def on_move_constraint_rows_down(self, _evt):
        self._move_selected_constraint_rows(1)

    def on_remove_constraint_row(self, _evt):
        self.constraints = self._constraints_from_grid()
        source_indices = self._selected_constraint_source_indices()
        if not source_indices:
            return
        for idx in reversed(sorted(source_indices)):
            del self.constraints[idx]
        self.populate_constraints()
        self.persist_constraints()

    def on_apply_constraints(self, _evt):
        self.constraints = self._constraints_from_grid()
        self.refresh_constraint_descriptions()
        self.persist_constraints(show_errors=True)
        self.push_apply_undo_snapshot()
        self._set_solver_controls_enabled(False)
        self._set_solver_status(running=True)
        try:
            errs = apply_all_constraints(
                self.board,
                self.constraints,
                progress_callback=self._update_solver_progress,
            )
            if errs:
                self._show_copyable_text_dialog(
                    "Apply constraints",
                    "Constraint solve failed or did not converge:\n\n" + "\n".join(errs),
                )
            self.populate()
            self.refresh_constraint_name_colours()
            self._refresh_constraint_drawings_if_visible()
        finally:
            self._set_solver_controls_enabled(True)
            self._set_solver_status(running=False)

    def on_debug_apply_constraints(self, _evt):
        self.constraints = self._constraints_from_grid()
        self.refresh_constraint_descriptions()
        self.persist_constraints(show_errors=True)
        self.push_apply_undo_snapshot()

        debug_log: list[str] = []
        debug_log.append("Board outline bbox (from board_outline_bbox):")
        ob = board_outline_bbox(self.board)
        if ob is None:
            debug_log.append("  (None — no Edge.Cuts found)")
        else:
            debug_log.append(f"  type:   {type(ob).__name__}")
            debug_log.append(f"  L (iu): {ob.GetLeft()}    R (iu): {ob.GetRight()}")
            debug_log.append(f"  T (iu): {ob.GetTop()}    B (iu): {ob.GetBottom()}")
            debug_log.append(f"  L (mm): {mm(ob.GetLeft()):.4f}  R (mm): {mm(ob.GetRight()):.4f}")
            debug_log.append(f"  T (mm): {mm(ob.GetTop()):.4f}  B (mm): {mm(ob.GetBottom()):.4f}")
            debug_log.append(f"  W (mm): {mm(ob.GetWidth()):.4f}  H (mm): {mm(ob.GetHeight()):.4f}")

        raw_bb = None
        try:
            if hasattr(self.board, "GetBoardEdgesBoundingBox"):
                raw_bb = self.board.GetBoardEdgesBoundingBox()
        except Exception:
            pass
        if raw_bb is not None:
            debug_log.append("")
            debug_log.append("Raw GetBoardEdgesBoundingBox (with line width):")
            debug_log.append(f"  L (mm): {mm(raw_bb.GetLeft()):.4f}  R (mm): {mm(raw_bb.GetRight()):.4f}")
            debug_log.append(f"  T (mm): {mm(raw_bb.GetTop()):.4f}  B (mm): {mm(raw_bb.GetBottom()):.4f}")

        debug_log.append("")
        debug_log.append(f"Edge.Cuts items on board:")
        try:
            for item in self.board.GetDrawings():
                if item.GetLayer() == pcbnew.Edge_Cuts:
                    w = _item_stroke_width(item)
                    ib = item.GetBoundingBox()
                    debug_log.append(
                        f"  {type(item).__name__}  stroke={mm(w):.3f}mm"
                        f"  bbox L={mm(ib.GetLeft()):.3f} R={mm(ib.GetRight()):.3f}"
                        f" T={mm(ib.GetTop()):.3f} B={mm(ib.GetBottom()):.3f}"
                    )
        except Exception as exc:
            debug_log.append(f"  (error: {exc})")

        debug_log.append("")
        self._set_solver_controls_enabled(False)
        self._set_solver_status(running=True)
        try:
            errs = apply_all_constraints(
                self.board,
                self.constraints,
                debug_log=debug_log,
                progress_callback=self._update_solver_progress,
            )
            if errs:
                debug_log.append("")
                debug_log.append("ERRORS:")
                debug_log.extend(f"  {e}" for e in errs)

            self.populate()
            self.refresh_constraint_name_colours()
            self._refresh_constraint_drawings_if_visible()
        finally:
            self._set_solver_controls_enabled(True)
            self._set_solver_status(running=False)

        self._set_debug_log_text("\n".join(debug_log), switch_to_tab=True)

    def on_undo_apply_constraints(self, _evt):
        if not self.apply_undo_stack:
            return

        snapshot = self.apply_undo_stack.pop()
        self.restore_apply_snapshot(snapshot)
        self.update_undo_apply_button()
        self.populate()
        self.populate_constraints()
        self._refresh_constraint_drawings_if_visible()

    def collect_rows(self):
        ec = edge_cut_row(self.board)
        foot = [footprint_row(fp) for fp in self.board.Footprints()]
        foot = sort_rows_top_to_bottom_then_left_to_right(foot)
        if ec:
            return [ec] + foot
        return foot

    def populate(self):
        self._populating = True
        try:
            rows = self.collect_rows()
            self.component_rows = rows
            self.refresh_constraint_choice_editors()
            constraint_counts = self._constraint_counts_by_ref()

            existing = self.grid.GetNumberRows()
            if existing:
                self.grid.DeleteRows(0, existing)

            if rows:
                self.grid.AppendRows(len(rows))

            for r, row in enumerate(rows):
                if not row.get("is_edge_cut"):
                    row["values"][self.COL_CONSTRAINTS] = str(
                        constraint_counts.get(row.get("display_name", row["values"][0]), 0)
                    )
                for c, value in enumerate(row["values"]):
                    self.grid.SetCellValue(r, c, str(value))
                if row.get("is_edge_cut"):
                    self.grid.SetReadOnly(r, self.COL_NAME, True)
                    self.grid.SetReadOnly(r, self.COL_CONSTRAINTS, True)
                    self.grid.SetReadOnly(r, self.COL_WIDTH, True)
                    self.grid.SetReadOnly(r, self.COL_HEIGHT, True)
                    self.grid.SetReadOnly(r, self.COL_LOCKED, True)
                else:
                    # Name, constraint count, width, and height are derived values; X/Y coordinates remain editable.
                    self.grid.SetReadOnly(r, self.COL_NAME, True)
                    self.grid.SetReadOnly(r, self.COL_CONSTRAINTS, True)
                    self.grid.SetReadOnly(r, self.COL_WIDTH, True)
                    self.grid.SetReadOnly(r, self.COL_HEIGHT, True)
                    self.grid.SetReadOnly(r, self.COL_LOCKED, True)

            widths = [260, 90, 100, 100, 100, 100, 100, 100, 100, 100, 80]
            for c, w in enumerate(widths):
                self.grid.SetColSize(c, w)
        finally:
            self._populating = False
        self.refresh_constraint_name_colours()

    def on_refresh(self, _event):
        self._constraints_path = board_constraints_path(self.board)
        self.constraints = load_constraints_from_file(self._constraints_path)
        self.constraints = self._normalize_constraint_orientations(self.constraints)
        self.populate()
        self.populate_constraints()

    def _remove_constraint_drawings(self):
        for item in self._constraint_drawings:
            try:
                self.board.Remove(item)
            except Exception:
                pass
        self._constraint_drawings = []
        try:
            pcbnew.Refresh()
        except Exception:
            pass

    def _add_pcb_item(self, item):
        self.board.Add(item)
        self._constraint_drawings.append(item)

    def _refresh_constraint_drawings_if_visible(self):
        if self._constraint_drawings:
            self._add_constraint_drawings()

    def _draw_constraint_line(self, x1, y1, x2, y2, layer, width_iu):
        line = pcbnew.PCB_SHAPE(self.board)
        line.SetShape(pcbnew.SHAPE_T_SEGMENT)
        line.SetStart(pcbnew.VECTOR2I(int(x1), int(y1)))
        line.SetEnd(pcbnew.VECTOR2I(int(x2), int(y2)))
        line.SetLayer(layer)
        line.SetWidth(int(width_iu))
        self._add_pcb_item(line)
        return line

    def _draw_constraint_tick(self, x, y, axis, layer, width_iu, tick_len):
        if axis == "x":
            self._draw_constraint_line(x, y - tick_len, x, y + tick_len, layer, width_iu)
        else:
            self._draw_constraint_line(x - tick_len, y, x + tick_len, y, layer, width_iu)

    def _draw_constraint_text(self, text, x, y, layer, size_iu, horizontal: bool):
        t = pcbnew.PCB_TEXT(self.board)
        t.SetText(text)
        t.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        t.SetLayer(layer)
        try:
            t.SetTextSize(pcbnew.VECTOR2I(int(size_iu), int(size_iu)))
        except Exception:
            try:
                t.SetTextWidth(int(size_iu))
                t.SetTextHeight(int(size_iu))
            except Exception:
                pass
        try:
            t.SetTextThickness(max(1, int(size_iu // 8)))
        except Exception:
            pass
        if not horizontal:
            try:
                t.SetTextAngle(pcbnew.EDA_ANGLE(900, pcbnew.TENTHS_OF_A_DEGREE_T))
            except Exception:
                try:
                    t.SetTextAngleDegrees(90)
                except Exception:
                    pass
        try:
            t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
            t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
        except Exception:
            pass
        self._add_pcb_item(t)
        return t

    def _add_constraint_drawings(self):
        self._remove_constraint_drawings()
        self.constraints = self._constraints_from_grid()

        show_dims = self.show_dimensions_cb.GetValue()
        show_labels = self.show_labels_cb.GetValue()
        active_constraint_idx = self._active_constraint_source_index()

        try:
            layer = self.board.GetLayerID("Cmts.User")
        except Exception:
            try:
                layer = pcbnew.Cmts_User
            except Exception:
                layer = 6

        line_w = pcbnew.FromMM(0.15)
        tick_len = pcbnew.FromMM(1.0)
        text_size = pcbnew.FromMM(1.0)
        text_offset = pcbnew.FromMM(1.5)

        for idx, c in enumerate(self.constraints):
            if self.constraint_filter_name:
                if not self._constraint_matches_filter(c, self.constraint_filter_name):
                    continue
            elif active_constraint_idx is not None and idx != active_constraint_idx:
                continue
            anchor_name = (c.get("anchor") or "").strip()
            constrained_name = (c.get("constrained") or "").strip()
            anchor_edge = (c.get("anchor_edge") or "").strip()
            constrained_edge = c.get("constrained_edge") or "Right"
            rel = c.get("relation") or "RIGHT_OF"

            if not anchor_edge:
                continue

            if is_board_outline_anchor(anchor_name):
                ab = board_outline_bbox(self.board)
                if ab is None:
                    continue
                if constrained_edge != "Center":
                    _near = {"RIGHT_OF": "Left", "LEFT_OF": "Right", "BELOW": "Top", "ABOVE": "Bottom"}
                    constrained_edge = _near.get(rel, constrained_edge)
            else:
                anchor_fp = _fp_by_display_name(self.board, anchor_name)
                if anchor_fp is None:
                    continue
                ab = footprint_bbox(anchor_fp)

            constrained_fp = _fp_by_display_name(self.board, constrained_name)
            if constrained_fp is None:
                continue
            cb = footprint_bbox(constrained_fp)

            ax, ay = _bbox_edge_point(ab, anchor_edge)
            cx, cy = _bbox_edge_point(cb, constrained_edge)

            is_h = rel in ("RIGHT_OF", "LEFT_OF", "ALIGN_HORIZONTALLY")

            if is_h:
                mid_y = (ay + cy) // 2
                self._draw_constraint_line(ax, mid_y, cx, mid_y, layer, line_w)
                self._draw_constraint_tick(ax, mid_y, "x", layer, line_w, tick_len)
                self._draw_constraint_tick(cx, mid_y, "x", layer, line_w, tick_len)
                mid_x = (ax + cx) // 2

                if show_dims:
                    dim_text = f"{mm(abs(cx - ax)):.2f} mm"
                    self._draw_constraint_text(dim_text, mid_x, mid_y - text_offset, layer, text_size, True)

                if show_labels:
                    label = f"[{idx + 1}] {constrained_name} \u2194 {anchor_name}"
                    self._draw_constraint_text(label, mid_x, mid_y + text_offset, layer, text_size * 3 // 4, True)

            elif rel in ("ABOVE", "BELOW", "ALIGN_VERTICALLY"):
                mid_x = (ax + cx) // 2
                self._draw_constraint_line(mid_x, ay, mid_x, cy, layer, line_w)
                self._draw_constraint_tick(mid_x, ay, "y", layer, line_w, tick_len)
                self._draw_constraint_tick(mid_x, cy, "y", layer, line_w, tick_len)
                mid_y = (ay + cy) // 2

                if show_dims:
                    dim_text = f"{mm(abs(cy - ay)):.2f} mm"
                    self._draw_constraint_text(dim_text, mid_x - text_offset, mid_y, layer, text_size, False)

                if show_labels:
                    label = f"[{idx + 1}] {constrained_name} \u2195 {anchor_name}"
                    self._draw_constraint_text(label, mid_x + text_offset, mid_y, layer, text_size * 3 // 4, False)

        try:
            pcbnew.Refresh()
        except Exception:
            pass

    def on_toggle_show_constraints(self, _evt):
        if self._constraint_drawings:
            self._remove_constraint_drawings()
            self.show_constraints_btn.SetLabel("Show constraints on PCB")
        else:
            self._add_constraint_drawings()
            self.show_constraints_btn.SetLabel("Hide constraints from PCB")

    def on_close_dialog(self, event):
        global _ACTIVE_DIALOG
        self._remove_constraint_drawings()
        self.reset_pick_state()
        self.persist_constraints()
        if _ACTIVE_DIALOG is self:
            _ACTIVE_DIALOG = None
        self.Destroy()


class AllFootprintsBBoxPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Footprint constraints and bounding boxes"
        self.category = "Inspection"
        self.description = (
            "View footprint bounding boxes and define relative placement constraints "
            "(e.g. keep one part a fixed gap to the right of another)."
        )
        self.show_toolbar_button = True
        _plugin_dir = os.path.dirname(os.path.abspath(__file__))
        _icon = os.path.join(_plugin_dir, "connstraints-icon.png")
        if os.path.isfile(_icon):
            self.icon_file_name = _icon
            self.dark_icon_file_name = _icon

    def Run(self):
        global _ACTIVE_DIALOG
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox(
                "Open a board in PCB Editor first.",
                "Footprint constraints",
                wx.OK | wx.ICON_WARNING,
            )
            return

        if _ACTIVE_DIALOG is not None and _ACTIVE_DIALOG:
            try:
                if _ACTIVE_DIALOG.IsShown():
                    _ACTIVE_DIALOG.board = board
                    _ACTIVE_DIALOG.Raise()
                    _ACTIVE_DIALOG.populate()
                    _ACTIVE_DIALOG.populate_constraints()
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

        dlg = AllFootprintsDialog(parent, board)
        _ACTIVE_DIALOG = dlg
        dlg.Show()


AllFootprintsBBoxPlugin().register()
