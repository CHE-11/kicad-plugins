# stencil.py
# KiCAD Plugin — Stencil Generator
# Uses the kikit library to generate a stencil from a board

# 3D printed: kikit stencil createprinted [parameters] <boardFile> <outputDirectory>
# Steel:       kikit stencil create --jigsize W H [parameters] <boardFile> <outputDirectory>
#
# createprinted parameters include --pcbthickness, --thickness, --framewidth, --ignore,
# --frameclearance, --enlargeholes (and --cutout per KiKit).
# stencil create: see `kikit stencil create --help`; docs use --jigsize W H plus --ignore, --cutout.

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import wx  # type: ignore
import pcbnew  # type: ignore

STENCIL_MODE_PRINTED = 0
STENCIL_MODE_STEEL = 1


def _kikit_base_cmd():
    exe = shutil.which("kikit")
    if exe:
        return [exe]
    return [sys.executable, "-m", "kikit"]


def _optional_float(text: str):
    s = (text or "").strip()
    if not s:
        return None
    return float(s)


def _optional_str(text: str):
    s = (text or "").strip()
    return s if s else None


def _argv_to_shell(argv):
    """Single string safe to paste into a shell. Windows: cmd.exe double-quote rules."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def _win_quote_path_for_cmd(path: str) -> str:
    """One path/token quoted for cmd.exe using double quotes (not POSIX single quotes)."""
    return subprocess.list2cmdline([path])


def _find_kicad_cmd_bat():
    """Path to kicad-cmd.bat, or None. Override with env KICAD_CMD_BAT."""
    env = (os.environ.get("KICAD_CMD_BAT") or "").strip()
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    try:
        p = Path(pcbnew.__file__).resolve()
        for d in (p.parent, *p.parents):
            bat = d / "bin" / "kicad-cmd.bat"
            if bat.is_file():
                return str(bat)
    except Exception:
        pass
    return None


class StencilDialog(wx.Dialog):
    def __init__(self, parent, board):
        super().__init__(
            parent,
            title="Stencil (KiKit)",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.board = board

        bp = ""
        try:
            bp = board.GetFileName() if hasattr(board, "GetFileName") else ""
        except Exception:
            bp = ""

        default_out = ""
        if bp:
            default_out = os.path.join(os.path.dirname(os.path.abspath(bp)), "stencil")

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self.mode = wx.RadioBox(
            panel,
            label="Stencil type",
            choices=[
                "3D printed (SLA)",
                "Steel (fab + jig)",
            ],
            majorDimension=2,
            style=wx.RA_SPECIFY_COLS,
        )
        self.mode.SetSelection(STENCIL_MODE_PRINTED)
        self.mode.Bind(wx.EVT_RADIOBOX, self._on_mode_change)
        root.Add(self.mode, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        grid.AddGrowableCol(1, 1)

        board_display = (bp or "").strip()
        if board_display:
            board_display = os.path.abspath(board_display)
        else:
            board_display = "(not saved — save the board to a file first)"

        grid.Add(wx.StaticText(panel, label="Board file:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.board_path = wx.TextCtrl(
            panel,
            value=board_display,
            style=wx.TE_READONLY,
        )
        self.board_path.Enable(False)
        self.board_path.SetMinSize((320, -1))
        grid.Add(self.board_path, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Output directory:"), 0, wx.ALIGN_CENTER_VERTICAL)
        out_row = wx.BoxSizer(wx.HORIZONTAL)
        self.out_dir = wx.TextCtrl(panel, value=default_out)
        browse = wx.Button(panel, label="Browse…")
        browse.Bind(wx.EVT_BUTTON, self._on_browse)
        out_row.Add(self.out_dir, 1, wx.EXPAND | wx.RIGHT, 6)
        out_row.Add(browse, 0)
        grid.Add(out_row, 1, wx.EXPAND)

        root.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        self._printed_panel = wx.Panel(panel)
        pg = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        pg.AddGrowableCol(1, 1)

        pg.Add(
            wx.StaticText(self._printed_panel, label="PCB thickness (mm):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.pcb_thickness = wx.TextCtrl(self._printed_panel, value="1.6")
        pg.Add(self.pcb_thickness, 1, wx.EXPAND)

        pg.Add(
            wx.StaticText(self._printed_panel, label="Stencil thickness (mm):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.stencil_thickness = wx.TextCtrl(self._printed_panel, value="0.15")
        pg.Add(self.stencil_thickness, 1, wx.EXPAND)

        pg.Add(
            wx.StaticText(self._printed_panel, label="Frame width (mm, optional):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.frame_width = wx.TextCtrl(self._printed_panel, value="")
        pg.Add(self.frame_width, 1, wx.EXPAND)

        pg.Add(
            wx.StaticText(self._printed_panel, label="Frame clearance (mm, optional):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.frame_clearance = wx.TextCtrl(self._printed_panel, value="")
        pg.Add(self.frame_clearance, 1, wx.EXPAND)

        pg.Add(
            wx.StaticText(self._printed_panel, label="Enlarge holes (mm, optional):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.enlarge_holes = wx.TextCtrl(self._printed_panel, value="")
        pg.Add(self.enlarge_holes, 1, wx.EXPAND)

        self._printed_panel.SetSizer(pg)

        self._steel_panel = wx.Panel(panel)
        sg = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        sg.AddGrowableCol(1, 1)
        sg.Add(
            wx.StaticText(self._steel_panel, label="Jig size W (mm):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.jig_w = wx.TextCtrl(self._steel_panel, value="60")
        sg.Add(self.jig_w, 1, wx.EXPAND)
        sg.Add(
            wx.StaticText(self._steel_panel, label="Jig size H (mm):"),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        self.jig_h = wx.TextCtrl(self._steel_panel, value="60")
        sg.Add(self.jig_h, 1, wx.EXPAND)
        self._steel_panel.SetSizer(sg)
        self._steel_panel.Hide()

        root.Add(self._printed_panel, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
        root.Add(self._steel_panel, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        opt_grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        opt_grid.AddGrowableCol(1, 1)

        opt_grid.Add(wx.StaticText(panel, label="Ignore refs (optional):"), 0, wx.ALIGN_TOP)
        self.ignore = wx.TextCtrl(panel, value="", style=wx.TE_MULTILINE, size=(-1, 56))
        ignore_hint = wx.StaticText(
            panel,
            label="Comma-separated refs to exclude from the stencil.",
        )
        ignore_hint.Wrap(360)
        ignore_col = wx.BoxSizer(wx.VERTICAL)
        ignore_col.Add(self.ignore, 0, wx.EXPAND)
        ignore_col.Add(ignore_hint, 0, wx.TOP, 4)
        opt_grid.Add(ignore_col, 0, wx.EXPAND)

        opt_grid.Add(wx.StaticText(panel, label="Cutout refs (optional):"), 0, wx.ALIGN_TOP)
        self.cutout = wx.TextCtrl(panel, value="", style=wx.TE_MULTILINE, size=(-1, 56))
        cut_hint = wx.StaticText(
            panel,
            label="Comma-separated refs: courtyard cutouts (pre-populated areas, repairs).",
        )
        cut_hint.Wrap(360)
        cut_col = wx.BoxSizer(wx.VERTICAL)
        cut_col.Add(self.cutout, 0, wx.EXPAND)
        cut_col.Add(cut_hint, 0, wx.TOP, 4)
        opt_grid.Add(cut_col, 0, wx.EXPAND)

        root.Add(opt_grid, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        self.cli_open_text = None

        if sys.platform == "win32":
            lbl_open = wx.StaticText(
                panel,
                label="1. Open KiCad command prompt (run this first):",
            )
            lbl_open.Wrap(520)
            root.Add(lbl_open, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
            self.cli_open_text = wx.TextCtrl(
                panel,
                value="",
                style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_DONTWRAP,
                size=(-1, 40),
            )
            self._set_cli_mono_font(self.cli_open_text)
            root.Add(self.cli_open_text, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
            copy_open = wx.Button(panel, label="Copy open command")
            copy_open.Bind(wx.EVT_BUTTON, self._on_copy_open_cmd)
            root.Add(copy_open, 0, wx.LEFT | wx.RIGHT, 10)

        if sys.platform == "win32":
            lbl_kikit = wx.StaticText(
                panel,
                label="2. KiKit command (run inside that prompt):",
            )
        else:
            lbl_kikit = wx.StaticText(
                panel,
                label="Full command (copy and run in a terminal):",
            )
        lbl_kikit.Wrap(520)
        root.Add(lbl_kikit, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.cli_kikit_text = wx.TextCtrl(
            panel,
            value="",
            style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_DONTWRAP,
            size=(-1, 96),
        )
        self._set_cli_mono_font(self.cli_kikit_text)
        root.Add(self.cli_kikit_text, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        copy_kikit = wx.Button(panel, label="Copy KiKit command")
        copy_kikit.Bind(wx.EVT_BUTTON, self._on_copy_kikit_cmd)
        root.Add(copy_kikit, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(root)
        wrap = wx.BoxSizer(wx.VERTICAL)
        wrap.Add(panel, 1, wx.EXPAND)
        self.SetSizer(wrap)
        self.SetMinSize((818, 800))
        self.SetSize((818, 800))

        for w in (
            self.out_dir,
            self.pcb_thickness,
            self.stencil_thickness,
            self.frame_width,
            self.frame_clearance,
            self.enlarge_holes,
            self.jig_w,
            self.jig_h,
            self.ignore,
            self.cutout,
        ):
            w.Bind(wx.EVT_TEXT, self._on_any_field_change)

        self._sync_mode_ui()
        self._refresh_cli_preview()

    def _on_mode_change(self, _evt):
        self._sync_mode_ui()
        self._refresh_cli_preview()

    def _sync_mode_ui(self):
        printed = self.mode.GetSelection() == STENCIL_MODE_PRINTED
        self._printed_panel.Show(printed)
        self._steel_panel.Show(not printed)
        self.Layout()

    def _on_any_field_change(self, _evt=None):
        self._refresh_cli_preview()

    @staticmethod
    def _set_cli_mono_font(ctrl):
        ctrl.SetFont(
            wx.Font(
                9,
                wx.FONTFAMILY_TELETYPE,
                wx.FONTSTYLE_NORMAL,
                wx.FONTWEIGHT_NORMAL,
            )
        )

    def _copy_to_clipboard(self, text: str):
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(text))
                wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()

    def _on_copy_open_cmd(self, _evt):
        if self.cli_open_text is not None:
            self._copy_to_clipboard(self.cli_open_text.GetValue())

    def _on_copy_kikit_cmd(self, _evt):
        self._copy_to_clipboard(self.cli_kikit_text.GetValue())

    def _argv_for_current_fields(self, placeholders: bool, *, use_plain_kikit_exe: bool = False):
        """Build kikit argv; with placeholders=True use stand-ins for missing paths."""
        board_path = ""
        try:
            board_path = self.board.GetFileName() if hasattr(self.board, "GetFileName") else ""
        except Exception:
            board_path = ""

        board_path = (board_path or "").strip()
        if not board_path or not os.path.isfile(board_path):
            board_path = (
                "<path-to-board.kicad_pcb>" if placeholders else ""
            )

        out_dir = self.out_dir.GetValue().strip()
        if not out_dir:
            out_dir = "<output-directory>" if placeholders else ""

        ign = _optional_str(self.ignore.GetValue())
        cut = _optional_str(self.cutout.GetValue())

        if use_plain_kikit_exe:
            base = ["kikit"]
        else:
            base = _kikit_base_cmd()

        steel = self.mode.GetSelection() == STENCIL_MODE_STEEL

        if steel:
            try:
                jw = float(self.jig_w.GetValue().strip())
                jh = float(self.jig_h.GetValue().strip())
            except ValueError:
                if placeholders:
                    jw, jh = 60.0, 60.0
                else:
                    raise ValueError("jig size") from None

            cmd = base + ["stencil", "create", "--jigsize", str(jw), str(jh)]
            if ign is not None:
                cmd.extend(["--ignore", ign])
            if cut is not None:
                cmd.extend(["--cutout", cut])
        else:
            pcb_mm = _optional_float(self.pcb_thickness.GetValue())
            thick_mm = _optional_float(self.stencil_thickness.GetValue())
            fw = _optional_float(self.frame_width.GetValue())
            fc = _optional_float(self.frame_clearance.GetValue())
            eh = _optional_float(self.enlarge_holes.GetValue())

            cmd = base + ["stencil", "createprinted"]
            if pcb_mm is not None:
                cmd.extend(["--pcbthickness", str(pcb_mm)])
            if thick_mm is not None:
                cmd.extend(["--thickness", str(thick_mm)])
            if fw is not None:
                cmd.extend(["--framewidth", str(fw)])
            if fc is not None:
                cmd.extend(["--frameclearance", str(fc)])
            if eh is not None:
                cmd.extend(["--enlargeholes", str(eh)])
            if ign is not None:
                cmd.extend(["--ignore", ign])
            if cut is not None:
                cmd.extend(["--cutout", cut])

        if board_path:
            cmd.append(board_path)
        if out_dir:
            cmd.append(out_dir)

        return cmd

    def _refresh_cli_preview(self):
        err = "# Invalid number — fix numeric fields (or jig W/H for steel mode)."
        try:
            if sys.platform == "win32":
                kikit_line = _argv_to_shell(
                    self._argv_for_current_fields(
                        placeholders=True,
                        use_plain_kikit_exe=True,
                    )
                )
            else:
                argv = self._argv_for_current_fields(placeholders=True)
        except ValueError:
            self.cli_kikit_text.SetValue(err)
            if sys.platform == "win32" and self.cli_open_text is not None:
                bat = _find_kicad_cmd_bat()
                if not bat:
                    bat = r"<KiCad-install>\bin\kicad-cmd.bat"
                self.cli_open_text.SetValue(
                    f"{_win_quote_path_for_cmd(bat)}"
                )
            return

        if sys.platform == "win32":
            bat = _find_kicad_cmd_bat()
            if not bat:
                bat = r"<KiCad-install>\bin\kicad-cmd.bat"
            open_cmd = f"{_win_quote_path_for_cmd(bat)}"
            if self.cli_open_text is not None:
                self.cli_open_text.SetValue(open_cmd)
            self.cli_kikit_text.SetValue(kikit_line)
        else:
            self.cli_kikit_text.SetValue(_argv_to_shell(argv))

    def _on_browse(self, _evt):
        path = self.out_dir.GetValue().strip()
        if path and os.path.isdir(path):
            initial = path
        else:
            initial = os.path.expanduser("~")
        with wx.DirDialog(self, "Choose output directory", defaultPath=initial) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.out_dir.SetValue(dlg.GetPath())
                self._refresh_cli_preview()


class StencilPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Stencil"
        self.category = "Fabrication"
        self.description = (
            "Generate solder paste stencils with KiKit: 3D printed (`stencil createprinted`) "
            "or steel fab + jig (`stencil create`)."
        )
        self.show_toolbar_button = True
        _plugin_dir = os.path.dirname(os.path.abspath(__file__))
        _icon = os.path.join(_plugin_dir, "stencil-icon.png")
        if os.path.isfile(_icon):
            self.icon_file_name = _icon
            self.dark_icon_file_name = _icon

    def Run(self):
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox(
                "Open a board in PCB Editor first.",
                "Stencil",
                wx.OK | wx.ICON_WARNING,
            )
            return

        parent = None
        try:
            app = wx.GetApp()
            if app is not None:
                parent = app.GetTopWindow()
        except Exception:
            parent = None

        dlg = StencilDialog(parent, board)
        dlg.ShowModal()
        dlg.Destroy()


StencilPlugin().register()
