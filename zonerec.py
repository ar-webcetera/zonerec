#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZoneRec — запись выбранной зоны экрана (Linux/X11).

Управление через трей, глобальные горячие клавиши и CLI:
    zonerec --start    выбрать зону мышкой и начать запись
    zonerec --stop     остановить запись (mp4 финализируется)
    zonerec --toggle   старт/стоп одной клавишей
    zonerec --screenshot сделать скриншот выбранной зоны
    zonerec --settings открыть настройки
Без аргументов — трей-демон (иконка + меню), следит за состоянием записи.

Состояние пишется в pidfile, поэтому CLI и демон видят одну запись.
Захват: встроенный выбор зоны + ffmpeg (x11grab).
"""
import os
import sys
import time
import signal
import shutil
import subprocess
import datetime
import configparser
import json

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf  # noqa: E402

try:
    gi.require_version("Keybinder", "3.0")
    from gi.repository import Keybinder
    _HAS_KEYBINDER = True
except Exception:
    Keybinder = None
    _HAS_KEYBINDER = False

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
    Notify.init("ZoneRec")
    _HAS_NOTIFY = True
except Exception:
    _HAS_NOTIFY = False

APP_ID = "zonerec"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), APP_ID)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), APP_ID)
STATE_PATH = os.path.join(CACHE_DIR, "state")  # строки: <pid>\n<outfile>


def get_default_videos_dir():
    path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    if path:
        return path
    if shutil.which("xdg-user-dir"):
        try:
            path = subprocess.check_output(
                ["xdg-user-dir", "VIDEOS"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if path:
                return path
        except Exception:
            pass
    return os.path.expanduser("~/Videos")


_videos = get_default_videos_dir()
_old_videos = os.path.expanduser("~/Видео")
DEFAULTS = {
    "hotkey_select_start": "<Super><Shift>r",
    "hotkey_stop": "<Super><Shift>s",
    "hotkey_screenshot": "<Super><Shift>p",
    "output_dir": os.path.join(_videos, "ZoneRec"),
    "screenshot_dir": os.path.join(_videos, "ZoneRec", "Screenshots"),
    "screenshot_format": "png",
    "screenshot_confirm": "true",
    "fps": "30",
    "preset": "veryfast",
    "selection_aspect": "free",
    # режим звука: none | mic | system | both (или произвольный источник PulseAudio)
    "audio": "none",
    "audio_mic": "",      # источник микрофона (pactl list sources short)
    "audio_system": "",   # источник «звук системы» (обычно <sink>.monitor)
}


def load_config():
    cfg = configparser.ConfigParser()
    cfg["zonerec"] = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.read(CONFIG_PATH)
    for k, v in DEFAULTS.items():
        if not cfg.has_option("zonerec", k):
            cfg.set("zonerec", k, v)
    old_output = os.path.join(_old_videos, "ZoneRec")
    if cfg.get("zonerec", "output_dir") == old_output and old_output != DEFAULTS["output_dir"]:
        cfg.set("zonerec", "output_dir", DEFAULTS["output_dir"])
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        cfg.write(f)


def config_bool(cfg, key, fallback=False):
    try:
        return cfg.getboolean("zonerec", key)
    except (configparser.Error, ValueError):
        return fallback


def notify(title, body=""):
    if _HAS_NOTIFY:
        try:
            Notify.Notification.new(title, body, APP_ID).show()
            return
        except Exception:
            pass
    print("[ZoneRec] %s — %s" % (title, body))


def find_icon_theme_path():
    candidates = (
        "/usr/share/pixmaps",
        "/usr/share/icons/hicolor/scalable/apps",
        os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps"),
    )
    for path in candidates:
        if os.path.exists(os.path.join(path, APP_ID + ".svg")):
            return path
    return None


def list_pulse_sources():
    """Return [(name, label, is_monitor)] from pactl."""
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sources", "short"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return []
    sources = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        is_monitor = name.endswith(".monitor")
        label = "%s%s" % (name, " (звук системы)" if is_monitor else " (микрофон)")
        sources.append((name, label, is_monitor))
    return sources


def _combo_get_text(combo):
    active_id = combo.get_active_id()
    if active_id:
        return active_id
    child = combo.get_child()
    return child.get_text().strip() if child is not None else ""


def _set_combo_value(combo, value):
    if value and combo.set_active_id(value):
        return
    child = combo.get_child()
    if child is not None:
        child.set_text(value or "")


def _make_source_combo(sources, active, want_monitor):
    combo = Gtk.ComboBoxText.new_with_entry()
    first = None
    for name, label, is_monitor in sources:
        if is_monitor == want_monitor:
            if first is None:
                first = name
            combo.append(name, label)
    _set_combo_value(combo, active or first or "")
    return combo


def _capture_accelerator(entry, event):
    keyval = Gdk.keyval_to_lower(event.keyval)
    mods = event.state & Gtk.accelerator_get_default_mod_mask()
    if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace, Gdk.KEY_Delete):
        entry.set_text("")
        return True
    if keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
                  Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
                  Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R):
        return True
    accel = Gtk.accelerator_name(keyval, mods)
    if Gtk.accelerator_valid(keyval, mods):
        entry.set_text(accel)
    return True


ASPECTS = (
    ("free", "Свободно"),
    ("16:9", "16:9"),
    ("9:16", "9:16"),
    ("4:3", "4:3"),
    ("3:4", "3:4"),
    ("1:1", "1:1"),
    ("21:9", "21:9"),
)


def aspect_ratio(value):
    if not value or value == "free" or ":" not in value:
        return None
    left, right = value.split(":", 1)
    try:
        w = float(left)
        h = float(right)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return w / h


def apply_aspect(x, y, w, h, aspect):
    ratio = aspect_ratio(aspect)
    if ratio is None:
        return x, y, w, h
    current = w / float(h)
    if current > ratio:
        new_w = int(h * ratio)
        x += (w - new_w) // 2
        w = new_w
    else:
        new_h = int(w / ratio)
        y += (h - new_h) // 2
        h = new_h
    return x, y, max(1, w), max(1, h)


def _root_geometry():
    screen = Gdk.Screen.get_default()
    if screen is None:
        return 0, 0, 1, 1
    root = screen.get_root_window()
    geom = root.get_geometry()
    return geom.x, geom.y, geom.width, geom.height


def _constrain_rect(x1, y1, x2, y2, aspect):
    ratio = aspect_ratio(aspect)
    dx = x2 - x1
    dy = y2 - y1
    if ratio is not None and dx and dy:
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        aw = abs(dx)
        ah = abs(dy)
        if aw / float(ah) > ratio:
            aw = int(ah * ratio)
        else:
            ah = int(aw / ratio)
        x2 = x1 + sx * aw
        y2 = y1 + sy * ah
    x = min(x1, x2)
    y = min(y1, y2)
    w = abs(x2 - x1)
    h = abs(y2 - y1)
    return x, y, w, h


MIN_REGION_SIZE = 10
HANDLE_SIZE = 8
EDGE_HIT_SIZE = 8
BUTTON_HEIGHT = 28
BUTTON_GAP = 8


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def _clamp_rect(x, y, w, h, max_w, max_h):
    w = _clamp(w, MIN_REGION_SIZE, max_w)
    h = _clamp(h, MIN_REGION_SIZE, max_h)
    x = _clamp(x, 0, max_w - w)
    y = _clamp(y, 0, max_h - h)
    return int(x), int(y), int(w), int(h)


def _clamp_selection_rect(x, y, w, h, max_w, max_h):
    x = _clamp(x, 0, max_w)
    y = _clamp(y, 0, max_h)
    w = max(0, min(w, max_w - x))
    h = max(0, min(h, max_h - y))
    return int(x), int(y), int(w), int(h)


class RegionSelector(Gtk.Window):
    def __init__(self, aspect, confirm=False):
        Gtk.Window.__init__(self, type=Gtk.WindowType.POPUP)
        self.aspect = aspect
        self.confirm = confirm
        self.result = None
        self._loop = GLib.MainLoop()
        self._dragging = False
        self._start = (0, 0)
        self._rect = None
        self._drag_mode = None
        self._drag_origin = (0, 0)
        self._drag_rect = None
        self._save_button = None
        self._cancel_button = None
        self._cursor_name = None
        self._root_x, self._root_y, width, height = _root_geometry()
        self._screen_w = width
        self._screen_h = height

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.move(self._root_x, self._root_y)
        self.resize(width, height)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.BUTTON1_MOTION_MASK |
            Gdk.EventMask.KEY_PRESS_MASK
        )
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._button_press)
        self.connect("button-release-event", self._button_release)
        self.connect("motion-notify-event", self._motion)
        self.connect("key-press-event", self._key_press)

    def run(self):
        self.show_all()
        window = self.get_window()
        if window is not None:
            self._set_cursor("crosshair")
            seat = self.get_display().get_default_seat()
            if seat:
                seat.grab(window, Gdk.SeatCapabilities.ALL, True, None, None, None)
        self.present()
        self.grab_focus()
        self._loop.run()
        if window is not None:
            seat = self.get_display().get_default_seat()
            if seat:
                seat.ungrab()
        self.destroy()
        return self.result

    def _finish(self):
        if self._loop.is_running():
            self._loop.quit()

    def _valid_rect(self):
        if self._rect is None:
            return False
        _x, _y, w, h = self._rect
        return w >= MIN_REGION_SIZE and h >= MIN_REGION_SIZE

    def _finish_current(self):
        if self._valid_rect():
            x, y, w, h = self._rect
            self.result = (x + self._root_x, y + self._root_y, w, h)
        else:
            self.result = None
        self._finish()

    def _set_cursor(self, name):
        if self._cursor_name == name:
            return
        window = self.get_window()
        if window is None:
            return
        cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name)
        if cursor is not None:
            window.set_cursor(cursor)
            self._cursor_name = name

    def _button_at(self, px, py):
        for name, rect in (("save", self._save_button), ("cancel", self._cancel_button)):
            if rect is None:
                continue
            x, y, w, h = rect
            if x <= px <= x + w and y <= py <= y + h:
                return name
        return None

    def _hit_test_rect(self, px, py):
        if self._rect is None:
            return None
        x, y, w, h = self._rect
        inside_x = x <= px <= x + w
        inside_y = y <= py <= y + h
        near_left = abs(px - x) <= EDGE_HIT_SIZE
        near_right = abs(px - (x + w)) <= EDGE_HIT_SIZE
        near_top = abs(py - y) <= EDGE_HIT_SIZE
        near_bottom = abs(py - (y + h)) <= EDGE_HIT_SIZE

        if inside_y and near_left:
            if near_top:
                return "resize-nw"
            if near_bottom:
                return "resize-sw"
            return "resize-w"
        if inside_y and near_right:
            if near_top:
                return "resize-ne"
            if near_bottom:
                return "resize-se"
            return "resize-e"
        if inside_x and near_top:
            return "resize-n"
        if inside_x and near_bottom:
            return "resize-s"
        if inside_x and inside_y:
            return "move"
        return None

    def _cursor_for_mode(self, mode):
        return {
            "move": "move",
            "resize-n": "ns-resize",
            "resize-s": "ns-resize",
            "resize-e": "ew-resize",
            "resize-w": "ew-resize",
            "resize-ne": "nesw-resize",
            "resize-sw": "nesw-resize",
            "resize-nw": "nwse-resize",
            "resize-se": "nwse-resize",
        }.get(mode, "crosshair")

    def _drag_adjust_rect(self, px, py):
        if self._drag_rect is None:
            return
        x, y, w, h = self._drag_rect
        dx = int(px - self._drag_origin[0])
        dy = int(py - self._drag_origin[1])

        if self._drag_mode == "move":
            self._rect = _clamp_rect(x + dx, y + dy, w, h, self._screen_w, self._screen_h)
            return

        left = x
        top = y
        right = x + w
        bottom = y + h
        mode = self._drag_mode or ""
        direction = mode.replace("resize-", "", 1)

        if aspect_ratio(self.aspect) is not None and mode.startswith("resize-"):
            if "w" in direction:
                anchor_x = right
                target_x = px
            else:
                anchor_x = left
                target_x = px
            if "n" in direction:
                anchor_y = bottom
                target_y = py
            else:
                anchor_y = top
                target_y = py
            rect = _constrain_rect(int(anchor_x), int(anchor_y), int(target_x), int(target_y), self.aspect)
            self._rect = _clamp_rect(*rect, self._screen_w, self._screen_h)
            return

        if "w" in direction:
            left = _clamp(left + dx, 0, right - MIN_REGION_SIZE)
        if "e" in direction:
            right = _clamp(right + dx, left + MIN_REGION_SIZE, self._screen_w)
        if "n" in direction:
            top = _clamp(top + dy, 0, bottom - MIN_REGION_SIZE)
        if "s" in direction:
            bottom = _clamp(bottom + dy, top + MIN_REGION_SIZE, self._screen_h)
        self._rect = (int(left), int(top), int(right - left), int(bottom - top))

    def _nudge_rect(self, keyval, state):
        if not self.confirm or self._rect is None:
            return False
        step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
        x, y, w, h = self._rect
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl:
            if keyval == Gdk.KEY_Left:
                w -= step
            elif keyval == Gdk.KEY_Right:
                w += step
            elif keyval == Gdk.KEY_Up:
                h -= step
            elif keyval == Gdk.KEY_Down:
                h += step
            else:
                return False
        else:
            if keyval == Gdk.KEY_Left:
                x -= step
            elif keyval == Gdk.KEY_Right:
                x += step
            elif keyval == Gdk.KEY_Up:
                y -= step
            elif keyval == Gdk.KEY_Down:
                y += step
            else:
                return False
        self._rect = _clamp_rect(x, y, w, h, self._screen_w, self._screen_h)
        self.queue_draw()
        return True

    def _button_press(self, _widget, event):
        if event.button == 3:
            self.result = None
            self._finish()
            return True
        if event.button != 1:
            return True
        if self.confirm and self._valid_rect():
            button = self._button_at(int(event.x), int(event.y))
            if button == "save":
                self._finish_current()
                return True
            if button == "cancel":
                self.result = None
                self._finish()
                return True
            if event.type == Gdk.EventType._2BUTTON_PRESS and self._hit_test_rect(int(event.x), int(event.y)):
                self._finish_current()
                return True
            mode = self._hit_test_rect(int(event.x), int(event.y))
            if mode is not None:
                self._dragging = True
                self._drag_mode = mode
                self._drag_origin = (int(event.x), int(event.y))
                self._drag_rect = self._rect
                self._set_cursor(self._cursor_for_mode(mode))
                return True
        self._dragging = True
        self._drag_mode = "select"
        self._start = (int(event.x), int(event.y))
        self._rect = (int(event.x), int(event.y), 0, 0)
        self.queue_draw()
        return True

    def _motion(self, _widget, event):
        if self._dragging:
            if self._drag_mode == "select":
                self._rect = _constrain_rect(self._start[0], self._start[1], int(event.x), int(event.y), self.aspect)
                self._rect = _clamp_selection_rect(*self._rect, self._screen_w, self._screen_h)
            else:
                self._drag_adjust_rect(event.x, event.y)
            self.queue_draw()
        elif self.confirm:
            mode = self._hit_test_rect(int(event.x), int(event.y))
            if self._button_at(int(event.x), int(event.y)):
                self._set_cursor("default")
            else:
                self._set_cursor(self._cursor_for_mode(mode))
        return True

    def _button_release(self, _widget, event):
        if event.button != 1 or not self._dragging:
            return True
        self._dragging = False
        if self._drag_mode == "select":
            self._rect = _constrain_rect(self._start[0], self._start[1], int(event.x), int(event.y), self.aspect)
            self._rect = _clamp_selection_rect(*self._rect, self._screen_w, self._screen_h)
        else:
            self._drag_adjust_rect(event.x, event.y)
        self._drag_mode = None
        self._drag_rect = None
        if self.confirm:
            if not self._valid_rect():
                self._rect = None
            self.queue_draw()
            return True
        self._finish_current()
        return True

    def _key_press(self, _widget, event):
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.result = None
            self._finish()
            return True
        if self.confirm and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self._finish_current()
            return True
        if self._nudge_rect(event.keyval, event.state):
            return True
        return True

    def _place_buttons(self, x, y, w, h, width, height):
        save_w = 98
        cancel_w = 78
        total_w = save_w + BUTTON_GAP + cancel_w
        bx = _clamp(x, 6, max(6, width - total_w - 6))
        by = y + h + 10
        if by + BUTTON_HEIGHT > height - 6:
            by = y - BUTTON_HEIGHT - 10
        by = _clamp(by, 6, max(6, height - BUTTON_HEIGHT - 6))
        self._save_button = (int(bx), int(by), save_w, BUTTON_HEIGHT)
        self._cancel_button = (int(bx + save_w + BUTTON_GAP), int(by), cancel_w, BUTTON_HEIGHT)

    def _draw_button(self, cr, rect, label, primary=False):
        x, y, w, h = rect
        if primary:
            cr.set_source_rgba(0.13, 0.58, 0.92, 0.96)
        else:
            cr.set_source_rgba(0.08, 0.10, 0.13, 0.92)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.18)
        cr.set_line_width(1)
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.96)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(13)
        ext = cr.text_extents(label)
        cr.move_to(x + (w - ext.width) / 2 - ext.x_bearing, y + (h - ext.height) / 2 - ext.y_bearing)
        cr.show_text(label)

    def _draw_handles(self, cr, x, y, w, h):
        points = (
            (x, y), (x + w / 2.0, y), (x + w, y),
            (x, y + h / 2.0), (x + w, y + h / 2.0),
            (x, y + h), (x + w / 2.0, y + h), (x + w, y + h),
        )
        cr.set_source_rgba(1, 1, 1, 0.98)
        for px, py in points:
            cr.rectangle(px - HANDLE_SIZE / 2.0, py - HANDLE_SIZE / 2.0, HANDLE_SIZE, HANDLE_SIZE)
            cr.fill()
        cr.set_source_rgba(0.08, 0.38, 0.62, 0.98)
        cr.set_line_width(1)
        for px, py in points:
            cr.rectangle(px - HANDLE_SIZE / 2.0 + 0.5, py - HANDLE_SIZE / 2.0 + 0.5, HANDLE_SIZE - 1, HANDLE_SIZE - 1)
            cr.stroke()

    def _draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        cr.set_source_rgba(0.02, 0.03, 0.05, 0.35)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if self._rect is None:
            return False
        x, y, w, h = self._rect
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgba(0.25, 0.75, 1.0, 0.95)
        cr.set_line_width(2)
        cr.rectangle(x + 1, y + 1, max(0, w - 2), max(0, h - 2))
        cr.stroke()
        if self.confirm and self._valid_rect():
            self._draw_handles(cr, x, y, w, h)
            self._place_buttons(x, y, w, h, width, height)
            self._draw_button(cr, self._save_button, "Сохранить", True)
            self._draw_button(cr, self._cancel_button, "Отмена", False)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(13)
        label = "%dx%d" % (w, h)
        if self.aspect and self.aspect != "free":
            label += "  %s" % self.aspect
        cr.move_to(x + 8, max(18, y - 8))
        cr.show_text(label)
        return False


def select_region(confirm=False):
    wait_modifiers_released()
    cfg = load_config()
    result = RegionSelector(cfg.get("zonerec", "selection_aspect"), confirm=confirm).run()
    if result is None:
        return None
    x, y, w, h = result
    if w < MIN_REGION_SIZE or h < MIN_REGION_SIZE:
        notify("Слишком маленькая зона")
        return None
    return x, y, w, h


def show_settings_dialog(on_saved=None):
    cfg = load_config()
    g = lambda k: cfg.get("zonerec", k)
    sources = list_pulse_sources()

    dialog = Gtk.Dialog(title="Настройки ZoneRec")
    dialog.set_default_size(620, 430)
    dialog.set_resizable(False)
    dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
    dialog.add_button("Сохранить", Gtk.ResponseType.OK)

    box = dialog.get_content_area()
    box.set_border_width(12)

    notebook = Gtk.Notebook()
    notebook.set_hexpand(True)
    notebook.set_vexpand(True)
    box.add(notebook)

    def make_page():
        grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        grid.set_border_width(14)
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        return grid

    def add_row(grid, row, text, widget):
        lab = Gtk.Label(label=text)
        lab.set_halign(Gtk.Align.START)
        lab.set_valign(Gtk.Align.CENTER)
        lab.set_size_request(150, -1)
        widget.set_hexpand(True)
        grid.attach(lab, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def add_note(grid, row, text):
        note = Gtk.Label(label=text)
        note.set_halign(Gtk.Align.START)
        note.set_line_wrap(True)
        note.set_max_width_chars(48)
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 1, row, 1, 1)

    def make_hotkey_entry(value):
        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_width_chars(28)
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "preferences-desktop-keyboard")
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.PRIMARY, "Глобальная горячая клавиша")
        entry.connect("key-press-event", _capture_accelerator)
        return entry

    def set_combo_active(combo, value, fallback):
        if not value or not combo.set_active_id(value):
            combo.set_active_id(fallback)

    video_page = make_page()
    output_dir = Gtk.FileChooserButton(title="Папка записей", action=Gtk.FileChooserAction.SELECT_FOLDER)
    output_dir.set_filename(g("output_dir"))
    add_row(video_page, 0, "Папка записей", output_dir)

    fps = Gtk.SpinButton()
    fps.set_range(1, 120)
    fps.set_increments(1, 10)
    fps.set_numeric(True)
    try:
        fps.set_value(int(g("fps")))
    except ValueError:
        fps.set_value(int(DEFAULTS["fps"]))
    add_row(video_page, 1, "Кадров в секунду", fps)

    preset = Gtk.ComboBoxText()
    for val, text in (
        ("ultrafast", "ultrafast"),
        ("superfast", "superfast"),
        ("veryfast", "veryfast"),
        ("faster", "faster"),
        ("fast", "fast"),
        ("medium", "medium"),
        ("slow", "slow"),
    ):
        preset.append(val, text)
    set_combo_active(preset, g("preset"), DEFAULTS["preset"])
    add_row(video_page, 2, "Пресет ffmpeg", preset)
    add_note(video_page, 3, "Быстрее = меньше нагрузка на процессор. Медленнее = обычно меньше размер файла.")
    notebook.append_page(video_page, Gtk.Label(label="Запись"))

    area_page = make_page()
    selection_aspect = Gtk.ComboBoxText()
    for key, text in ASPECTS:
        selection_aspect.append(key, text)
    set_combo_active(selection_aspect, g("selection_aspect"), "free")
    add_row(area_page, 0, "Пропорции области", selection_aspect)
    add_note(area_page, 1, "В свободном режиме записывается вся выделенная область. При выбранной пропорции область подрезается по центру.")
    notebook.append_page(area_page, Gtk.Label(label="Область"))

    screenshot_page = make_page()
    screenshot_dir = Gtk.FileChooserButton(title="Папка скриншотов", action=Gtk.FileChooserAction.SELECT_FOLDER)
    screenshot_dir.set_filename(g("screenshot_dir"))
    add_row(screenshot_page, 0, "Папка скриншотов", screenshot_dir)

    screenshot_format = Gtk.ComboBoxText()
    for key, text in (("png", "PNG"), ("jpg", "JPEG")):
        screenshot_format.append(key, text)
    set_combo_active(screenshot_format, g("screenshot_format"), "png")
    add_row(screenshot_page, 1, "Формат", screenshot_format)

    screenshot_confirm = Gtk.CheckButton(label="Можно поправить рамку перед сохранением")
    screenshot_confirm.set_active(config_bool(cfg, "screenshot_confirm", True))
    add_row(screenshot_page, 2, "Подтверждение", screenshot_confirm)
    notebook.append_page(screenshot_page, Gtk.Label(label="Скриншоты"))

    audio_page = make_page()
    audio = Gtk.ComboBoxText()
    audio_items = (
        ("none", "Без звука"),
        ("mic", "Микрофон"),
        ("system", "Звук системы"),
        ("both", "Микрофон + звук системы"),
    )
    for key, text in audio_items:
        audio.append(key, text)
    set_combo_active(audio, g("audio"), "none")
    add_row(audio_page, 0, "Режим", audio)

    audio_mic = _make_source_combo(sources, g("audio_mic"), False)
    add_row(audio_page, 1, "Микрофон", audio_mic)

    audio_system = _make_source_combo(sources, g("audio_system"), True)
    add_row(audio_page, 2, "Звук системы", audio_system)

    def refresh_audio_controls(*_):
        mode = audio.get_active_id() or "none"
        audio_mic.set_sensitive(mode in ("mic", "both"))
        audio_system.set_sensitive(mode in ("system", "both"))

    audio.connect("changed", refresh_audio_controls)
    refresh_audio_controls()
    notebook.append_page(audio_page, Gtk.Label(label="Аудио"))

    hotkeys_page = make_page()
    hotkey_start = make_hotkey_entry(g("hotkey_select_start"))
    hotkey_start.set_placeholder_text("Нажмите комбинацию")
    add_row(hotkeys_page, 0, "Начать запись", hotkey_start)

    hotkey_stop = make_hotkey_entry(g("hotkey_stop"))
    hotkey_stop.set_placeholder_text("Нажмите комбинацию")
    add_row(hotkeys_page, 1, "Остановить запись", hotkey_stop)
    hotkey_screenshot = make_hotkey_entry(g("hotkey_screenshot"))
    hotkey_screenshot.set_placeholder_text("Нажмите комбинацию")
    add_row(hotkeys_page, 2, "Скриншот", hotkey_screenshot)
    notebook.append_page(hotkeys_page, Gtk.Label(label="Хоткеи"))

    box.show_all()
    response = dialog.run()
    if response == Gtk.ResponseType.OK:
        cfg.set("zonerec", "hotkey_select_start", hotkey_start.get_text().strip())
        cfg.set("zonerec", "hotkey_stop", hotkey_stop.get_text().strip())
        cfg.set("zonerec", "hotkey_screenshot", hotkey_screenshot.get_text().strip())
        cfg.set("zonerec", "output_dir", output_dir.get_filename() or DEFAULTS["output_dir"])
        cfg.set("zonerec", "screenshot_dir", screenshot_dir.get_filename() or DEFAULTS["screenshot_dir"])
        cfg.set("zonerec", "screenshot_format", screenshot_format.get_active_id() or "png")
        cfg.set("zonerec", "screenshot_confirm", "true" if screenshot_confirm.get_active() else "false")
        cfg.set("zonerec", "fps", str(fps.get_value_as_int()))
        cfg.set("zonerec", "preset", preset.get_active_id() or DEFAULTS["preset"])
        cfg.set("zonerec", "selection_aspect", selection_aspect.get_active_id() or "free")
        cfg.set("zonerec", "audio", audio.get_active_id() or "none")
        cfg.set("zonerec", "audio_mic", _combo_get_text(audio_mic))
        cfg.set("zonerec", "audio_system", _combo_get_text(audio_system))
        save_config(cfg)
        notify("Настройки сохранены")
        if on_saved is not None:
            on_saved()
    dialog.destroy()


# --- состояние записи (pidfile) ---
def read_recording_state():
    try:
        with open(STATE_PATH) as f:
            raw = f.read()
        if raw.lstrip().startswith("{"):
            state = json.loads(raw)
            pid = int(state["pid"])
            if not pid_is_running(pid):
                return None
            return state
        lines = raw.splitlines()
        pid = int(lines[0])
        out = lines[1] if len(lines) > 1 else ""
        if not pid_is_running(pid):
            return None
        return {"pid": pid, "outfile": out, "video_temp": out,
                "audio_pids": [], "audio_temps": []}
    except Exception:
        return None


def read_state():
    state = read_recording_state()
    if state is None:
        return None, None
    return int(state["pid"]), state.get("outfile", "")


def _linux_pid_state(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            stat = f.read()
    except OSError:
        return None
    end = stat.rfind(")")
    if end == -1 or len(stat) <= end + 2:
        return None
    return stat[end + 2]


def pid_is_running(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if _linux_pid_state(pid) == "Z":
        try:
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
        return False
    return True


def write_state(pid, outfile, video_temp=None, audio_pids=None, audio_temps=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    state = {
        "pid": pid,
        "outfile": outfile,
        "video_temp": video_temp or outfile,
        "audio_pids": audio_pids or [],
        "audio_temps": audio_temps or [],
    }
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)
        f.write("\n")


def clear_state():
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


def is_recording():
    return read_state()[0] is not None


# --- ожидание отпускания клавиш хоткея ---
_MODS = (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
         | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.MOD4_MASK
         | Gdk.ModifierType.SUPER_MASK)


def wait_modifiers_released(timeout_s=3.0):
    """Ждём отпускания модификаторов хоткея перед выбором области."""
    try:
        disp = Gdk.Display.get_default()
        seat = disp.get_default_seat()
        pointer = seat.get_pointer()
        root = disp.get_default_screen().get_root_window()
    except Exception:
        time.sleep(0.25)  # фолбэк, если Gdk недоступен
        return
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            _, _, mask = root.get_device_position(pointer)[1:]
        except Exception:
            time.sleep(0.25)
            return
        if not (mask & _MODS):
            return
        time.sleep(0.03)


def copy_image_to_clipboard(path):
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_image(pixbuf)
        clipboard.store()
        while Gtk.events_pending():
            Gtk.main_iteration()
        return True
    except Exception:
        return False


# --- действия ---
def do_start():
    if is_recording():
        notify("Запись уже идёт")
        return
    if not shutil.which("ffmpeg"):
        notify("Нет зависимости", "Нужен ffmpeg")
        return
    region = select_region()
    if region is None:
        return

    # Принудительно очищаем события GTK, чтобы окно выделения успело закрыться
    while Gtk.events_pending():
        Gtk.main_iteration()
    time.sleep(0.1)

    x, y, w, h = region
    w -= w % 2
    h -= h % 2
    if w < 10 or h < 10:
        notify("Слишком маленькая зона")
        return
    cfg = load_config()
    g = lambda k: cfg.get("zonerec", k)
    outdir = g("output_dir")
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    outfile = os.path.join(outdir, "zapis-%s.mp4" % ts)
    display = os.environ.get("DISPLAY", ":0")
    # какие аудио-источники писать
    mode = g("audio").strip().lower()
    srcs = []
    if mode in ("mic", "both") and g("audio_mic").strip():
        srcs.append(g("audio_mic").strip())
    if mode in ("system", "both") and g("audio_system").strip():
        srcs.append(g("audio_system").strip())
    if mode and mode not in ("none", "mic", "system", "both"):
        srcs = [g("audio").strip()]  # произвольный источник pulse
    if mode == "mic" and not srcs:
        notify("Не выбран микрофон", "Откройте настройки ZoneRec")
        return
    if mode == "system" and not srcs:
        notify("Не выбран звук системы", "Откройте настройки ZoneRec")
        return
    if mode == "both" and len(srcs) < 2:
        notify("Не выбраны источники аудио", "Откройте настройки ZoneRec")
        return
    # Экран и каждый Pulse-источник пишутся отдельными процессами. ffmpeg
    # открывает входы последовательно, поэтому единый процесс накапливал кадры
    # в очереди, пока USB-микрофон инициализировался, и ролик шёл рывками.
    base = os.path.splitext(outfile)[0]
    video_temp = base + ".video.mp4" if srcs else outfile
    audio_temps = [base + ".audio%d.m4a" % (i + 1) for i in range(len(srcs))]
    audio_procs = []
    for source, audio_temp in zip(srcs, audio_temps):
        audio_cmd = [
            "ffmpeg", "-y", "-thread_queue_size", "64", "-f", "pulse",
            "-i", source, "-c:a", "aac", "-b:a", "128k", audio_temp,
        ]
        audio_procs.append(subprocess.Popen(
            audio_cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    cmd = [
        "ffmpeg", "-y", "-thread_queue_size", "64", "-f", "x11grab",
        "-framerate", g("fps"), "-video_size", "%dx%d" % (w, h),
        "-i", "%s+%d,%d" % (display, x, y), "-c:v", "libx264",
        "-preset", g("preset"), "-pix_fmt", "yuv420p", video_temp,
    ]
    notify_extra = {"none": "", "mic": " + микрофон", "system": " + звук ПК",
                    "both": " + звук+микрофон"}.get(mode, " + звук") if srcs else ""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    write_state(proc.pid, outfile, video_temp,
                [p.pid for p in audio_procs], audio_temps)
    notify("Запись пошла", "%dx%d, %s fps%s" % (w, h, g("fps"), notify_extra))

    # Запускаем красную рамку, чтобы видеть, что именно пишется
    subprocess.Popen([sys.argv[0], "--overlay", str(x), str(y), str(w), str(h)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def do_screenshot():
    if not shutil.which("ffmpeg"):
        notify("Нет зависимости", "Нужен ffmpeg")
        return
    cfg = load_config()
    g = lambda k: cfg.get("zonerec", k)
    region = select_region(confirm=config_bool(cfg, "screenshot_confirm", True))
    if region is None:
        return

    # Принудительно очищаем события GTK, чтобы окно выделения успело закрыться
    while Gtk.events_pending():
        Gtk.main_iteration()
    time.sleep(0.1)

    x, y, w, h = region
    fmt = g("screenshot_format").strip().lower()
    if fmt not in ("png", "jpg", "jpeg"):
        fmt = "png"
    ext = "jpg" if fmt == "jpeg" else fmt
    outdir = g("screenshot_dir")
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    outfile = os.path.join(outdir, "screenshot-%s.%s" % (ts, ext))
    display = os.environ.get("DISPLAY", ":0")
    cmd = [
        "ffmpeg", "-y", "-f", "x11grab", "-video_size", "%dx%d" % (w, h),
        "-i", "%s+%d,%d" % (display, x, y), "-frames:v", "1",
    ]
    if ext == "jpg":
        cmd += ["-q:v", "2"]
    cmd += [outfile]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        notify("Не удалось сделать скриншот")
        return
    if copy_image_to_clipboard(outfile):
        notify("Скриншот сохранён и скопирован", os.path.basename(outfile))
    else:
        notify("Скриншот сохранён", "Не удалось скопировать в буфер")


def media_duration(path):
    try:
        value = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return float(value)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 0.0


def mux_recording(video_path, audio_paths, outfile):
    video_duration = media_duration(video_path)
    if video_duration <= 0 or not audio_paths:
        return False
    cmd = ["ffmpeg", "-y", "-i", video_path]
    for audio_path in audio_paths:
        cmd += ["-i", audio_path]

    filters = []
    padded = []
    for index, audio_path in enumerate(audio_paths, 1):
        audio_duration = media_duration(audio_path)
        if audio_duration <= 0:
            return False
        label = "a%d" % index
        # Pulse и X11 стартуют почти одновременно, но Pulse раньше перестаёт
        # отдавать буфер при SIGINT. Выравниваем дорожки по началу, а недостающий
        # хвост дополняем тишиной. Сдвиг тишины в начало задерживал весь звук.
        filters.append("[%d:a]apad[%s]" % (index, label))
        padded.append("[%s]" % label)
    if len(padded) == 1:
        audio_label = padded[0]
    else:
        filters.append("%samix=inputs=%d:duration=longest:dropout_transition=0[a]" %
                       ("".join(padded), len(padded)))
        audio_label = "[a]"
    cmd += ["-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", audio_label,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-t", "%.3f" % video_duration, "-movflags", "+faststart", outfile]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def do_stop():
    state = read_recording_state()
    if state is None:
        notify("Сейчас ничего не записывается")
        return
    pid = int(state["pid"])
    outfile = state.get("outfile", "")
    audio_pids = [int(p) for p in state.get("audio_pids", [])]
    pids = [pid] + audio_pids
    try:
        for current_pid in pids:
            try:
                os.kill(current_pid, signal.SIGINT)
            except OSError:
                pass
        for _ in range(200):
            if not any(pid_is_running(current_pid) for current_pid in pids):
                break
            time.sleep(0.1)
    except Exception:
        pass
    audio_temps = state.get("audio_temps", [])
    video_temp = state.get("video_temp", outfile)
    if audio_temps:
        if not mux_recording(video_temp, audio_temps, outfile):
            clear_state()
            notify("Не удалось собрать запись", "Временные файлы сохранены рядом с видео")
            return
        for temp_path in [video_temp] + audio_temps:
            try:
                os.remove(temp_path)
            except OSError:
                pass
    clear_state()
    notify("Запись сохранена", os.path.basename(outfile or ""))


def do_toggle():
    do_stop() if is_recording() else do_start()


_BOUND_HOTKEYS = []


def _hotkey_callback(_key, action):
    if action == "start":
        do_start()
    elif action == "stop":
        do_stop()
    elif action == "screenshot":
        do_screenshot()


def apply_hotkeys():
    global _BOUND_HOTKEYS
    if not _HAS_KEYBINDER:
        notify("Хоткеи недоступны", "Не загружен Keybinder")
        return
    for hotkey in _BOUND_HOTKEYS:
        try:
            Keybinder.unbind(hotkey)
        except Exception:
            pass
    _BOUND_HOTKEYS = []

    cfg = load_config()
    pairs = (
        (cfg.get("zonerec", "hotkey_select_start").strip(), "start"),
        (cfg.get("zonerec", "hotkey_stop").strip(), "stop"),
        (cfg.get("zonerec", "hotkey_screenshot").strip(), "screenshot"),
    )
    for hotkey, action in pairs:
        if not hotkey:
            continue
        try:
            if Keybinder.bind(hotkey, _hotkey_callback, action):
                _BOUND_HOTKEYS.append(hotkey)
            else:
                notify("Хоткей занят", hotkey)
        except Exception:
            notify("Не удалось назначить хоткей", hotkey)


# --- трей-демон (иконка + меню) ---
def run_tray():
    AppIndicator = None
    for nm in ("AppIndicator3", "AyatanaAppIndicator3"):
        try:
            gi.require_version(nm, "0.1")
            AppIndicator = getattr(__import__("gi.repository", fromlist=[nm]), nm)
            break
        except (ValueError, ImportError):
            continue
    save_config(load_config())
    if _HAS_KEYBINDER:
        Keybinder.init()
        apply_hotkeys()
    else:
        notify("Хоткеи недоступны", "Установите gir1.2-keybinder-3.0")

    ind = None
    if AppIndicator is not None:
        ind = AppIndicator.Indicator.new(APP_ID, APP_ID,
                                         AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        icon_theme_path = find_icon_theme_path()
        if icon_theme_path is not None:
            ind.set_icon_theme_path(icon_theme_path)
            ind.set_icon_full(APP_ID, "готов")
        ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        mi_start = Gtk.MenuItem(label="Выбрать зону и записать")
        mi_start.connect("activate", lambda *_: do_start())
        menu.append(mi_start)
        mi_screenshot = Gtk.MenuItem(label="Скриншот зоны")
        mi_screenshot.connect("activate", lambda *_: do_screenshot())
        menu.append(mi_screenshot)
        mi_stop = Gtk.MenuItem(label="Остановить запись")
        mi_stop.connect("activate", lambda *_: do_stop())
        menu.append(mi_stop)
        menu.append(Gtk.SeparatorMenuItem())
        mi_dir = Gtk.MenuItem(label="Открыть папку записей")
        mi_dir.connect("activate", lambda *_: subprocess.Popen(
            ["xdg-open", load_config().get("zonerec", "output_dir")]))
        menu.append(mi_dir)
        mi_settings = Gtk.MenuItem(label="Настройки")
        mi_settings.connect("activate", lambda *_: show_settings_dialog(apply_hotkeys))
        menu.append(mi_settings)
        menu.append(Gtk.SeparatorMenuItem())
        mi_quit = Gtk.MenuItem(label="Выход")
        mi_quit.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(mi_quit)
        menu.show_all()
        ind.set_menu(menu)

    def refresh():
        if ind is not None:
            rec = is_recording()
            ind.set_icon_full("media-record" if rec else APP_ID,
                              "идёт запись" if rec else "готов")
        return True

    GLib.timeout_add_seconds(1, refresh)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                         lambda *_: (Gtk.main_quit(), False)[1])
    Gtk.main()


class RecordingBorder(Gtk.Window):
    def __init__(self, x, y, w, h):
        Gtk.Window.__init__(self, type=Gtk.WindowType.POPUP)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)

        import cairo
        empty_region = cairo.Region()
        self.input_shape_combine_region(empty_region)

        self.move(x, y)
        self.resize(w, h)

        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        import cairo
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0.72, 0.21, 0.15, 0.9)
        cr.paint()
        return False


def run_overlay(x, y, w, h):
    # Нельзя использовать одно прозрачное окно размером с область записи:
    # некоторые X11-композиторы замораживают находящееся под ним изображение,
    # и x11grab получает один и тот же кадр. Четыре узких окна не перекрывают
    # захватываемые пиксели вообще.
    border = 2
    root_x, root_y, screen_w, screen_h = _root_geometry()
    screen_right = root_x + screen_w
    screen_bottom = root_y + screen_h
    rects = (
        (max(root_x, x - border), max(root_y, y - border),
         min(screen_right, x + w + border) - max(root_x, x - border),
         min(border, max(0, y - root_y))),
        (max(root_x, x - border), y + h,
         min(screen_right, x + w + border) - max(root_x, x - border),
         min(border, max(0, screen_bottom - (y + h)))),
        (max(root_x, x - border), y, min(border, max(0, x - root_x)), h),
        (x + w, y, min(border, max(0, screen_right - (x + w))), h),
    )
    overlays = []
    for bx, by, bw, bh in rects:
        if bw <= 0 or bh <= 0:
            continue
        overlay = RecordingBorder(bx, by, bw, bh)
        overlay.show_all()
        overlays.append(overlay)

    def check_alive():
        if not is_recording():
            Gtk.main_quit()
            return False
        return True
    GLib.timeout_add_seconds(1, check_alive)
    Gtk.main()


def main():
    args = sys.argv[1:]
    if not args:
        run_tray()
        return
    # инициализируем Gdk, чтобы живой опрос модификаторов работал в CLI
    try:
        Gtk.init([])
    except Exception:
        pass
    cmd = args[0].lstrip("-")
    if cmd == "start":
        do_start()
    elif cmd == "stop":
        do_stop()
    elif cmd == "toggle":
        do_toggle()
    elif cmd in ("screenshot", "shot"):
        do_screenshot()
    elif cmd in ("settings", "prefs", "preferences"):
        show_settings_dialog()
    elif cmd == "overlay" and len(args) == 5:
        run_overlay(int(args[1]), int(args[2]), int(args[3]), int(args[4]))
    else:
        sys.stderr.write("usage: zonerec [--start|--stop|--toggle|--screenshot|--settings]\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
