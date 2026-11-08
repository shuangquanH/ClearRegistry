# -*- coding: utf-8 -*-
"""
注册表清理工具

功能：
  1. 输入关键字，搜索注册表“键名”和“值名”中包含该关键字的项目；
     搜索结果边扫描边显示，不必等全部扫描结束；
  2. 左侧勾选框用于选择要删除的项目；
     点击项目文字查看详情，右键在注册表编辑器中定位；
  3. 删除已勾选项目，删除前自动导出 .reg 备份（可通过双击备份文件恢复）。

搜索范围：
  - 当前用户 HKEY_CURRENT_USER（全部）
  - 可选：HKEY_LOCAL_MACHINE\\SOFTWARE
           HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet
"""

import ctypes
import datetime
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
import winreg

APP_TITLE = "注册表清理工具"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(APP_DIR, "备份")
SEP = "\\"
MAX_RESULTS = 500            # 列表最多显示条数，避免误搜常见词时界面卡死
PROGRESS_INTERVAL = 0.12     # 进度刷新间隔（秒）

# 界面配色
BG = "#f3f4f6"
CARD = "#ffffff"
TEXT = "#1f2328"
MUTED = "#6b7280"
PLACEHOLDER_FG = "#9aa1a9"
ACCENT = "#2563eb"
DANGER = "#c62828"

HIVE_INFO = {
    winreg.HKEY_CURRENT_USER: ("HKEY_CURRENT_USER", "HKCU"),
    winreg.HKEY_LOCAL_MACHINE: ("HKEY_LOCAL_MACHINE", "HKLM"),
}

SCAN_ROOTS = (
    (winreg.HKEY_CURRENT_USER, "", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet", "HKLM"),
)

REG_QWORD = getattr(winreg, "REG_QWORD", 11)
REGEDIT_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit"

VALUE_TYPE_NAMES = {
    winreg.REG_NONE: "REG_NONE",
    winreg.REG_SZ: "REG_SZ",
    winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
    winreg.REG_BINARY: "REG_BINARY",
    winreg.REG_DWORD: "REG_DWORD",
    winreg.REG_DWORD_BIG_ENDIAN: "REG_DWORD_BIG_ENDIAN",
    winreg.REG_LINK: "REG_LINK",
    winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
    winreg.REG_RESOURCE_LIST: "REG_RESOURCE_LIST",
    winreg.REG_FULL_RESOURCE_DESCRIPTOR: "REG_FULL_RESOURCE_DESCRIPTOR",
    winreg.REG_RESOURCE_REQUIREMENTS_LIST: "REG_RESOURCE_REQUIREMENTS_LIST",
    REG_QWORD: "REG_QWORD",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
class RegistryItem:
    """一个可删除的注册表项目：键（连同子项）或单个值。"""

    __slots__ = ("kind", "hive", "key_path", "value_name", "value_type")

    def __init__(self, kind, hive, key_path, value_name=None, value_type=None):
        self.kind = kind              # "key" | "value"
        self.hive = hive
        self.key_path = key_path      # 相对 hive 的路径
        self.value_name = value_name
        self.value_type = value_type

    @property
    def leaf_name(self):
        return self.key_path.rsplit(SEP, 1)[-1] if self.key_path else ""

    @property
    def primary(self):
        return self.value_name if self.kind == "value" else self.leaf_name

    @property
    def full_key(self):
        label = HIVE_INFO[self.hive][1]
        return label + (SEP + self.key_path if self.key_path else "")

    @property
    def meta(self):
        if self.kind == "value":
            type_name = VALUE_TYPE_NAMES.get(self.value_type, "类型 %s" % self.value_type)
            return "%s   ·   %s" % (self.full_key, type_name)
        return "%s   ·   注册表项（键），删除时连同全部子项" % self.full_key


class ScanResult:
    __slots__ = (
        "hits", "keys_scanned", "values_scanned",
        "total_found", "errors", "truncated", "canceled",
    )

    def __init__(self, hits, keys_scanned, values_scanned,
                 total_found, errors, truncated, canceled):
        self.hits = hits
        self.keys_scanned = keys_scanned
        self.values_scanned = values_scanned
        self.total_found = total_found
        self.errors = errors
        self.truncated = truncated
        self.canceled = canceled


class ResultRow:
    __slots__ = ("item", "var", "frame", "separator", "name_label", "meta_label")

    def __init__(self, item, var, frame, separator, name_label, meta_label):
        self.item = item
        self.var = var
        self.frame = frame
        self.separator = separator
        self.name_label = name_label
        self.meta_label = meta_label


class BackupError(Exception):
    pass


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def scan_registry(keyword, include_hklm, progress_cb, cancel_event, hit_cb=None):
    """递归扫描注册表；每命中一项立即通过 hit_cb 通知界面。"""
    needle = keyword.casefold()
    hits = []
    keys_scanned = 0
    values_scanned = 0
    total_found = 0
    errors = 0
    truncated = False
    last_report = 0.0
    roots = SCAN_ROOTS if include_hklm else SCAN_ROOTS[:1]

    def record(item):
        nonlocal total_found, truncated
        total_found += 1
        if len(hits) >= MAX_RESULTS:
            truncated = True
            return
        hits.append(item)
        if hit_cb is not None:
            hit_cb(item)

    for hive, start, _label in roots:
        stack = [start]
        while stack:
            if cancel_event.is_set():
                return ScanResult(hits, keys_scanned, values_scanned,
                                  total_found, errors, truncated, True)
            key_path = stack.pop()
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                    keys_scanned += 1
                    leaf = key_path.rsplit(SEP, 1)[-1] if key_path else ""
                    # 不把扫描起点本身（如 SOFTWARE）列为可删除项
                    if leaf and not (start and key_path == start) and needle in leaf.casefold():
                        record(RegistryItem("key", hive, key_path))

                    index = 0
                    while True:
                        try:
                            value_name, _data, value_type = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        values_scanned += 1
                        if needle in value_name.casefold():
                            record(RegistryItem("value", hive, key_path,
                                                value_name, value_type))
                        index += 1

                    index = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        stack.append(key_path + SEP + sub_name if key_path else sub_name)
                        index += 1
            except OSError:
                errors += 1

            now = time.monotonic()
            if now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                progress_cb(keys_scanned, total_found)

    return ScanResult(hits, keys_scanned, values_scanned,
                      total_found, errors, truncated, False)


# ---------------------------------------------------------------------------
# 项目详情
# ---------------------------------------------------------------------------
def _hex_dump(data):
    if not data:
        return "（空）"
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join("%02X" % byte for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append("%04X  %-47s  %s" % (offset, hex_part, ascii_part))
    return "\n".join(lines)


def describe_value(item):
    """读取并格式化一个注册表值的当前数据。"""
    try:
        with winreg.OpenKey(item.hive, item.key_path, 0, winreg.KEY_READ) as key:
            data, value_type = winreg.QueryValueEx(key, item.value_name)
    except OSError as exc:
        return "无法读取该值：%s" % _describe_error(exc)

    if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ, winreg.REG_LINK):
        text = str(data)
        if value_type == winreg.REG_EXPAND_SZ:
            text += "\n\n（REG_EXPAND_SZ：部分程序会展开其中的环境变量后再使用）"
        return text
    if value_type == winreg.REG_MULTI_SZ:
        parts = data if isinstance(data, list) else [str(data)]
        return "\n".join(parts) if parts else "（空）"
    if value_type == winreg.REG_DWORD:
        number = int(data)
        return "%d（0x%08X）" % (number, number & 0xFFFFFFFF)
    if value_type == winreg.REG_DWORD_BIG_ENDIAN:
        number = int(data) if isinstance(data, int) else int.from_bytes(data, "big")
        return "%d（大端，0x%08X）" % (number, number & 0xFFFFFFFF)
    if value_type == REG_QWORD:
        number = int(data)
        return "%d（0x%016X）" % (number, number & 0xFFFFFFFFFFFFFFFF)
    if isinstance(data, bytes):
        return _hex_dump(data)
    if isinstance(data, list):
        return "\n".join(str(part) for part in data)
    return repr(data)


def describe_key(item):
    """汇总一个注册表项的基本信息、子项/值预览。"""
    try:
        with winreg.OpenKey(item.hive, item.key_path, 0, winreg.KEY_READ) as key:
            sub_count, value_count, last_write = winreg.QueryInfoKey(key)
            subkeys = _enum_subkeys(key)[:20]
            values = [name for name, _data, _type in _enum_values(key)][:20]
    except OSError as exc:
        return ("无法读取该键：%s\n\n"
                "注意：删除此注册表项时，会连同其全部子项一起删除。"
                % _describe_error(exc))

    lines = [
        "子项数量：%d" % sub_count,
        "值数量：%d" % value_count,
    ]
    try:
        modified = datetime.datetime.fromtimestamp(
            last_write / 10000000 - 11644473600)
        lines.append("最后修改：%s" % modified.strftime("%Y-%m-%d %H:%M:%S"))
    except (OverflowError, OSError, ValueError):
        pass

    if subkeys:
        lines.append("")
        lines.append("子项预览：")
        lines.extend("  · %s" % name for name in subkeys)
        if sub_count > len(subkeys):
            lines.append("  … 另有 %d 个子项" % (sub_count - len(subkeys)))
    if values:
        lines.append("")
        lines.append("值预览：")
        lines.extend("  · %s" % (name if name else "（默认）") for name in values)
        if value_count > len(values):
            lines.append("  … 另有 %d 个值" % (value_count - len(values)))

    lines.append("")
    lines.append("注意：删除此注册表项时，会连同其全部子项一起删除。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 在注册表编辑器中定位
# ---------------------------------------------------------------------------
def _regedit_root_label():
    """读取 regedit 的 LastKey，得到当前语言下的根节点名称。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGEDIT_SETTINGS_KEY,
                            0, winreg.KEY_READ) as key:
            last_key, _type = winreg.QueryValueEx(key, "LastKey")
        if isinstance(last_key, str) and SEP in last_key:
            prefix = last_key.split(SEP, 1)[0].strip()
            if prefix:
                return prefix
    except OSError:
        pass

    try:
        lang = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:
        lang = 0
    if (lang & 0x3FF) == 0x004:      # 中文
        if (lang & 0x0FFF) in (0x404, 0xC04, 0x1404):
            return "電腦"
        return "计算机"
    return "Computer"


def regedit_target_path(item):
    hive_full = HIVE_INFO[item.hive][0]
    path = hive_full + (SEP + item.key_path if item.key_path else "")
    return _regedit_root_label() + SEP + path


def set_regedit_lastkey(item):
    """写入 regedit 下次打开时要定位的键路径。"""
    target = regedit_target_path(item)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGEDIT_SETTINGS_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "LastKey", 0, winreg.REG_SZ, target)
    return target


def regedit_executable():
    windows_dir = os.environ.get("WINDIR") or r"C:\Windows"
    candidate = os.path.join(windows_dir, "regedit.exe")
    return candidate if os.path.exists(candidate) else "regedit.exe"


# ---------------------------------------------------------------------------
# 备份（.reg）
# ---------------------------------------------------------------------------
def _full_reg_path(hive, key_path):
    return HIVE_INFO[hive][0] + (SEP + key_path if key_path else "")


def _short_key_path(hive, key_path):
    return HIVE_INFO[hive][1] + (SEP + key_path if key_path else "")


def _enum_values(key):
    values = []
    index = 0
    while True:
        try:
            values.append(winreg.EnumValue(key, index))
        except OSError:
            break
        index += 1
    return values


def _enum_subkeys(key):
    subkeys = []
    index = 0
    while True:
        try:
            subkeys.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return subkeys


def _reg_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _hex_csv(data):
    return ",".join("%02x" % byte for byte in data)


def _utf16(text):
    return text.encode("utf-16-le")


def _format_reg_value(name, value_type, data):
    """把单个注册表值格式化为 .reg 中的一行。"""
    value_name = "@" if name == "" else '"%s"' % _reg_escape(name)

    if value_type == winreg.REG_SZ:
        if isinstance(data, bytes):
            data = data.decode("utf-16-le", "surrogatepass").rstrip("\x00")
        if isinstance(data, str) and not any(ch in data for ch in "\r\n\x00"):
            return '%s="%s"' % (value_name, _reg_escape(data))
        raw = _utf16(data if isinstance(data, str) else str(data)) + b"\x00\x00"
        return "%s=hex(1):%s" % (value_name, _hex_csv(raw))

    if value_type == winreg.REG_EXPAND_SZ:
        if isinstance(data, bytes):
            raw = data
            if not raw.endswith(b"\x00\x00"):
                raw += b"\x00\x00"
        else:
            raw = _utf16(str(data)) + b"\x00\x00"
        return "%s=hex(2):%s" % (value_name, _hex_csv(raw))

    if value_type in (winreg.REG_BINARY, winreg.REG_NONE):
        if isinstance(data, str):
            data = _utf16(data)
        return "%s=hex:%s" % (value_name, _hex_csv(bytes(data)))

    if value_type == winreg.REG_DWORD:
        value = data if isinstance(data, int) else int.from_bytes(data[:4], "little")
        return "%s=dword:%08x" % (value_name, value & 0xFFFFFFFF)

    if value_type == winreg.REG_DWORD_BIG_ENDIAN:
        value = data if isinstance(data, int) else int.from_bytes(data[:4], "big")
        return "%s=hex(5):%s" % (value_name, _hex_csv(value.to_bytes(4, "big")))

    if value_type == REG_QWORD:
        value = data if isinstance(data, int) else int.from_bytes(data[:8], "little")
        return "%s=hex(b):%s" % (value_name, _hex_csv(value.to_bytes(8, "little")))

    if value_type == winreg.REG_MULTI_SZ:
        if isinstance(data, str):
            parts = [part for part in data.split("\x00") if part]
        else:
            parts = [str(part) for part in data]
        raw = b"".join(_utf16(part) + b"\x00\x00" for part in parts) + b"\x00\x00"
        return "%s=hex(7):%s" % (value_name, _hex_csv(raw))

    if value_type == winreg.REG_LINK:
        if isinstance(data, str):
            raw = _utf16(data)
        else:
            raw = bytes(data)
        return "%s=hex(6):%s" % (value_name, _hex_csv(raw))

    if isinstance(data, str):
        return "%s=hex:%s" % (value_name, _hex_csv(_utf16(data)))
    if isinstance(data, int):
        width = max(4, (data.bit_length() + 7) // 8)
        return "%s=hex:%s" % (value_name, _hex_csv(data.to_bytes(width, "little")))
    return "%s=hex:%s" % (value_name, _hex_csv(bytes(data)))


def _emit_key_backup(lines, hive, key_path):
    lines.append("[%s]" % _full_reg_path(hive, key_path))
    try:
        with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
            values = _enum_values(key)
            subkeys = _enum_subkeys(key)
    except OSError as exc:
        raise BackupError("无法读取注册表键：%s（%s）" % (
            _short_key_path(hive, key_path), exc)) from exc

    values.sort(key=lambda entry: (entry[0] != "", entry[0].casefold()))
    for name, data, value_type in values:
        lines.append(_format_reg_value(name, value_type, data))
    lines.append("")

    for sub_name in subkeys:
        _emit_key_backup(lines, hive, key_path + SEP + sub_name)


def write_backup(key_items, value_items, file_path):
    """把待删除的键（整棵子树）和值导出成 UTF-16LE 的 .reg 文件。"""
    lines = ["Windows Registry Editor Version 5.00", ""]

    grouped = {}
    for item in value_items:
        grouped.setdefault((item.hive, item.key_path), []).append(item.value_name)

    for (hive, key_path), names in grouped.items():
        lines.append("[%s]" % _full_reg_path(hive, key_path))
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                for name in names:
                    try:
                        data, value_type = winreg.QueryValueEx(key, name)
                    except OSError as exc:
                        raise BackupError("无法读取注册表值：%s\\%s（%s）" % (
                            _short_key_path(hive, key_path), name, exc)) from exc
                    lines.append(_format_reg_value(name, value_type, data))
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("无法打开注册表键：%s（%s）" % (
                _short_key_path(hive, key_path), exc)) from exc
        lines.append("")

    for item in key_items:
        _emit_key_backup(lines, item.hive, item.key_path)

    text = "\r\n".join(lines).rstrip("\r\n") + "\r\n"
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as handle:
        handle.write(b"\xff\xfe")                 # UTF-16LE BOM
        handle.write(text.encode("utf-16-le"))


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def effective_selection(items):
    """去掉被“已选父键”覆盖的子项，避免重复删除。"""
    keys = [item for item in items if item.kind == "key"]
    values = [item for item in items if item.kind == "value"]

    kept_keys = []
    for item in sorted(keys, key=lambda i: i.key_path.count(SEP)):
        path = item.key_path.casefold()
        covered = any(
            item.hive == kept.hive and (
                path == kept.key_path.casefold()
                or path.startswith(kept.key_path.casefold() + SEP)
            )
            for kept in kept_keys
        )
        if not covered:
            kept_keys.append(item)

    kept_values = []
    for item in values:
        path = item.key_path.casefold()
        covered = any(
            item.hive == hive and (
                path == key_path or path.startswith(key_path + SEP)
            )
            for hive, key_path in (
                (kept.hive, kept.key_path.casefold()) for kept in kept_keys
            )
        )
        if not covered:
            kept_values.append(item)

    return kept_keys, kept_values


def delete_key_tree(hive, key_path):
    """递归删除一个键及其全部子键。"""
    try:
        with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
            subkeys = _enum_subkeys(key)
    except FileNotFoundError:
        return
    for sub_name in subkeys:
        delete_key_tree(hive, key_path + SEP + sub_name)
    try:
        winreg.DeleteKey(hive, key_path)
    except FileNotFoundError:
        pass


def delete_value_item(item):
    try:
        with winreg.OpenKey(item.hive, item.key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, item.value_name)
    except FileNotFoundError:
        pass


def _describe_error(exc):
    if isinstance(exc, PermissionError):
        return "没有权限（需要管理员权限，或该项受系统保护）"
    if isinstance(exc, FileNotFoundError):
        return "该项已不存在"
    return getattr(exc, "strerror", None) or str(exc)


# ---------------------------------------------------------------------------
# 带占位提示的输入框
# ---------------------------------------------------------------------------
class PlaceholderEntry(tk.Entry):
    def __init__(self, master, placeholder, **kwargs):
        self._normal_fg = kwargs.pop("fg", TEXT)
        super().__init__(master, fg=self._normal_fg, **kwargs)
        self.placeholder = placeholder
        self._active = False
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._show_placeholder()

    def _show_placeholder(self):
        self._active = True
        self.delete(0, "end")
        self.insert(0, self.placeholder)
        self.configure(fg=PLACEHOLDER_FG)

    def _on_focus_in(self, _event=None):
        if self._active:
            self.delete(0, "end")
            self._active = False
            self.configure(fg=self._normal_fg)

    def _on_focus_out(self, _event=None):
        if not self.get().strip():
            self._show_placeholder()

    def get_text(self):
        return "" if self._active else self.get().strip()

    def set_text(self, text):
        if self._active:
            self._active = False
            self.configure(fg=self._normal_fg)
        self.delete(0, "end")
        self.insert(0, text)


# ---------------------------------------------------------------------------
# 项目详情窗口
# ---------------------------------------------------------------------------
class DetailWindow(tk.Toplevel):
    def __init__(self, master, item):
        super().__init__(master)
        self._item = item
        self.title("项目详情")
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(True, True)
        self.minsize(620, 440)

        width = min(780, max(620, self.winfo_screenwidth() - 120))
        height = min(600, max(460, self.winfo_screenheight() - 160))
        master.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 2)
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

        outer = ttk.Frame(self, style="App.TFrame", padding=(18, 16, 18, 14))
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text=item.primary, style="DialogTitle.TLabel",
            wraplength=720, justify="left",
        ).pack(anchor="w")
        kind_text = "注册表值" if item.kind == "value" else "注册表项（键）"
        ttk.Label(outer, text=kind_text, style="Sub.TLabel").pack(
            anchor="w", pady=(2, 10))

        fields = ttk.Frame(outer, style="App.TFrame")
        fields.pack(fill="x")
        rows = [("位置", item.full_key)]
        if item.kind == "value":
            rows.append(("值名称", item.value_name or ""))
            rows.append(("值类型", VALUE_TYPE_NAMES.get(
                item.value_type, "类型 %s" % item.value_type)))
        else:
            rows.append(("键名称", item.leaf_name))
        for index, (label, value) in enumerate(rows):
            ttk.Label(fields, text=label + "：", style="FieldName.TLabel").grid(
                row=index, column=0, sticky="nw", pady=2)
            ttk.Label(fields, text=value, style="FieldValue.TLabel",
                      wraplength=640, justify="left").grid(
                row=index, column=1, sticky="w", pady=2)
        fields.columnconfigure(1, weight=1)

        detail_text = (describe_value(item) if item.kind == "value"
                       else describe_key(item))
        ttk.Label(outer, text="值数据" if item.kind == "value" else "键信息",
                  style="FieldName.TLabel").pack(anchor="w", pady=(12, 4))

        text_frame = ttk.Frame(outer, style="App.TFrame")
        text_frame.pack(fill="both", expand=True)
        self.text = tk.Text(
            text_frame, wrap="char", font=("Consolas", 9),
            bg="#fbfbfc", fg=TEXT, relief="solid", bd=1,
            padx=8, pady=6, height=12,
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical",
                                  command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        try:
            self.text.insert("1.0", detail_text)
        except tk.TclError:
            self.text.insert("1.0", repr(detail_text))
        self.text.configure(state="disabled")

        buttons = ttk.Frame(outer, style="App.TFrame")
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="定位到注册表",
                   command=self._locate).pack(side="left")
        ttk.Button(buttons, text="关闭",
                   command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()

    def _locate(self):
        if self.master._open_in_regedit(self._item, parent=self):
            self.destroy()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class RegistryCleanerApp(tk.Tk):
    def __init__(self, auto_query=None):
        super().__init__()
        self._admin = is_user_admin()
        self._is_searching = False
        self._cancel_event = None
        self._queue = None
        self._poll_job = None
        self._resize_job = None
        self._rows = []
        self._empty_label = None
        self._last_keyword = ""

        self.title(APP_TITLE)
        self.configure(bg=BG)

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(1000, max(760, screen_w - 80))
        height = min(700, max(520, screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.minsize(760, 520)

        self._setup_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<MouseWheel>", self._on_mousewheel)

        if auto_query:
            self.after(300, lambda: self._auto_search(auto_query))

    # -- 外观 ---------------------------------------------------------------
    def _setup_style(self):
        families = set(tkfont.families(self))
        self.font_family = "Segoe UI"
        for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if candidate in families:
                self.font_family = candidate
                break

        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkIconFont"):
            try:
                tkfont.nametofont(name).configure(family=self.font_family, size=10)
            except tk.TclError:
                pass

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        family = self.font_family
        style.configure("App.TFrame", background=BG)
        style.configure("Row.TFrame", background=CARD)
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=(family, 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=(family, 9))
        style.configure("Status.TLabel", background=BG, foreground=MUTED,
                        font=(family, 9))
        style.configure("Admin.TLabel", background=BG, foreground="#15803d",
                        font=(family, 9, "bold"))
        style.configure("Item.TLabel", background=CARD, foreground=TEXT,
                        font=(family, 10))
        style.configure("Meta.TLabel", background=CARD, foreground=MUTED,
                        font=(family, 9))
        style.configure("Empty.TLabel", background=CARD, foreground="#9aa1a9",
                        font=(family, 10))
        style.configure("DialogTitle.TLabel", background=BG, foreground=TEXT,
                        font=(family, 13, "bold"))
        style.configure("FieldName.TLabel", background=BG, foreground=MUTED,
                        font=(family, 10, "bold"))
        style.configure("FieldValue.TLabel", background=BG, foreground=TEXT,
                        font=(family, 10))
        style.configure("App.TCheckbutton", background=BG, font=(family, 9))
        style.configure("Row.TCheckbutton", background=CARD)
        style.configure("TButton", font=(family, 10), padding=(12, 5))
        style.configure("Search.TButton", font=(family, 10, "bold"),
                        padding=(16, 6))
        style.configure("Danger.TButton", font=(family, 10, "bold"),
                        padding=(14, 6), foreground=DANGER)
        style.configure("Link.TButton", font=(family, 9), padding=(6, 2),
                        foreground=ACCENT)

    def _build_ui(self):
        outer = ttk.Frame(self, style="App.TFrame", padding=(18, 14, 18, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="按名称匹配键名和值名；勾选框选择删除，点击项目看详情，右键跳转注册表；删除前自动备份。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        # 搜索框 + 搜索按钮
        search_row = ttk.Frame(outer, style="App.TFrame")
        search_row.pack(fill="x")
        self.entry = PlaceholderEntry(
            search_row,
            placeholder="搜索你要删除的注册表名字",
            font=(self.font_family, 11),
            bg="#ffffff",
            disabledbackground="#f0f1f3",
            disabledforeground=MUTED,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#d5d9df",
            highlightcolor=ACCENT,
            insertbackground=TEXT,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.entry.bind("<Return>", lambda _e: self._on_search_button())

        self.search_btn = ttk.Button(
            search_row, text="搜索", width=8,
            style="Search.TButton", command=self._on_search_button,
        )
        self.search_btn.pack(side="left", padx=(10, 0), ipady=2)

        # 选项行
        options = ttk.Frame(outer, style="App.TFrame")
        options.pack(fill="x", pady=(10, 2))
        self.include_hklm = tk.BooleanVar(master=self, value=True)
        self.chk_hklm = ttk.Checkbutton(
            options,
            text="同时搜索系统级注册表 HKLM（较慢，约 10 秒）",
            variable=self.include_hklm,
            style="App.TCheckbutton",
        )
        self.chk_hklm.pack(side="left")

        if self._admin:
            ttk.Label(options, text="管理员权限", style="Admin.TLabel").pack(side="right")
        else:
            ttk.Button(
                options, text="以管理员身份重启",
                style="Link.TButton", command=self._ask_elevate,
            ).pack(side="right")

        # 状态行
        status_row = ttk.Frame(outer, style="App.TFrame")
        status_row.pack(fill="x", pady=(4, 8))
        self.status_var = tk.StringVar(
            value="输入关键字后点击“搜索”，将同时匹配键名和值名。")
        ttk.Label(
            status_row, textvariable=self.status_var,
            style="Status.TLabel", anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status_row, mode="indeterminate", length=120)
        # 搜索时才显示

        # 结果列表（可滚动）
        card = tk.Frame(outer, bg=CARD, highlightbackground="#dcdfe4",
                        highlightthickness=1)
        card.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(card, bg=CARD, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(card, orient="vertical",
                                       command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.inner = ttk.Frame(self.canvas, style="Row.TFrame")
        self._canvas_window = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # 底部：统计 + 全选 + 删除
        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.pack(fill="x", pady=(10, 0))
        self.count_var = tk.StringVar(value="共 0 项，已选 0 项")
        ttk.Label(bottom, textvariable=self.count_var,
                  style="Status.TLabel").pack(side="left")
        self.btn_delete = ttk.Button(bottom, text="删除", width=10,
                                     style="Danger.TButton",
                                     command=self._delete_selected)
        self.btn_delete.pack(side="right")
        self.btn_all = ttk.Button(bottom, text="全选", width=10,
                                  command=self._toggle_all)
        self.btn_all.pack(side="right", padx=(0, 8))

        self._show_empty("输入关键字后点击“搜索”，将同时匹配键名和值名。")
        self._update_count()

    # -- 列表辅助 -----------------------------------------------------------
    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._canvas_window, width=event.width)
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(120, self._apply_wrap)

    def _apply_wrap(self):
        self._resize_job = None
        wrap = max(260, self.canvas.winfo_width() - 120)
        for row in self._rows:
            row.name_label.configure(wraplength=wrap)
            row.meta_label.configure(wraplength=wrap)

    def _on_mousewheel(self, event):
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        in_list = False
        current = widget
        while current is not None:
            if current is self.canvas or current is self.inner:
                in_list = True
                break
            current = getattr(current, "master", None)
        if not in_list:
            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _clear_list(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self._rows = []
        self._empty_label = None
        self._update_count()

    def _show_empty(self, text):
        self._clear_list()
        label = ttk.Label(
            self.inner, text=text, style="Empty.TLabel",
            anchor="center", justify="center", wraplength=620,
        )
        label.pack(fill="x", pady=80)
        self._empty_label = label

    def _add_row(self, item):
        frame = ttk.Frame(self.inner, style="Row.TFrame")
        frame.pack(fill="x")

        var = tk.BooleanVar(master=self, value=False)
        check = ttk.Checkbutton(
            frame, variable=var, command=self._update_count,
            style="Row.TCheckbutton",
        )
        check.pack(side="left", anchor="n", padx=(12, 8), pady=(10, 10))

        body = ttk.Frame(frame, style="Row.TFrame")
        body.pack(side="left", fill="x", expand=True, pady=(8, 8), padx=(0, 14))

        name_label = ttk.Label(
            body, text=item.primary or "", style="Item.TLabel",
            anchor="w", justify="left",
        )
        name_label.pack(fill="x", anchor="w")
        meta_label = ttk.Label(
            body, text=item.meta, style="Meta.TLabel",
            anchor="w", justify="left",
        )
        meta_label.pack(fill="x", anchor="w", pady=(2, 0))

        wrap = max(260, self.canvas.winfo_width() - 120)
        name_label.configure(wraplength=wrap)
        meta_label.configure(wraplength=wrap)

        for widget in (frame, body, name_label, meta_label):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>",
                        lambda _e, it=item: self._show_details(it))
            widget.bind("<Button-3>",
                        lambda _e, it=item: self._open_in_regedit(it))

        separator = ttk.Separator(self.inner, orient="horizontal")
        separator.pack(fill="x", padx=12)
        self._rows.append(ResultRow(item, var, frame, separator,
                                    name_label, meta_label))

    def _append_hit(self, item):
        if self._empty_label is not None:
            try:
                self._empty_label.destroy()
            except tk.TclError:
                pass
            self._empty_label = None
        self._add_row(item)
        self._update_count()

    def _update_count(self):
        total = len(self._rows)
        selected = sum(1 for row in self._rows if row.var.get())
        self.count_var.set("共 %d 项，已选 %d 项" % (total, selected))
        if hasattr(self, "btn_all"):
            all_checked = total > 0 and selected == total
            self.btn_all.configure(text="取消全选" if all_checked else "全选")

    def _toggle_all(self):
        if not self._rows:
            return
        select = not all(row.var.get() for row in self._rows)
        for row in self._rows:
            row.var.set(select)
        self._update_count()

    # -- 详情与定位 ---------------------------------------------------------
    def _show_details(self, item):
        return DetailWindow(self, item)

    def _open_in_regedit(self, item, parent=None):
        parent = parent or self
        try:
            target = set_regedit_lastkey(item)
        except OSError as exc:
            messagebox.showerror(
                APP_TITLE,
                "无法写入注册表编辑器的定位信息：\n%s" % exc,
                parent=parent,
            )
            return False
        try:
            subprocess.Popen([regedit_executable(), "/m"])
        except OSError as exc:
            messagebox.showerror(
                APP_TITLE,
                "无法启动注册表编辑器：\n%s" % exc,
                parent=parent,
            )
            return False
        suffix = "（值所在键）" if item.kind == "value" else ""
        self.status_var.set("已在注册表编辑器中定位到：%s%s"
                            % (item.full_key, suffix))
        return True

    # -- 搜索流程 -----------------------------------------------------------
    def _on_search_button(self):
        if self._is_searching:
            self._stop_search()
        else:
            self._start_search()

    def _auto_search(self, query):
        self.entry.set_text(query)
        self._start_search()

    def _start_search(self):
        if self._is_searching:
            return
        keyword = self.entry.get_text()
        if not keyword:
            messagebox.showinfo(APP_TITLE, "请输入要搜索的注册表名称。", parent=self)
            try:
                self.entry.focus_set()
            except tk.TclError:
                pass
            return

        self._last_keyword = keyword
        self._is_searching = True
        self._cancel_event = threading.Event()
        self._queue = queue.Queue()

        self._clear_list()
        self._show_empty("正在搜索，请稍候…")
        self.canvas.yview_moveto(0)
        self._set_busy_ui(True)
        self.status_var.set("正在搜索…")
        self.progress.pack(side="right")
        self.progress.start(12)

        include = bool(self.include_hklm.get())
        worker = threading.Thread(
            target=self._run_scan,
            args=(keyword, include, self._queue, self._cancel_event),
            daemon=True,
        )
        worker.start()
        self._poll_job = self.after(60, self._poll_queue)

    def _stop_search(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.search_btn.configure(text="停止中…", state="disabled")
        self.status_var.set("正在停止搜索…")

    def _run_scan(self, keyword, include_hklm, result_queue, cancel_event):
        def report(scanned, found):
            result_queue.put_nowait(("progress", scanned, found))

        def hit(item):
            result_queue.put_nowait(("hit", item))

        try:
            result = scan_registry(keyword, include_hklm, report, cancel_event,
                                   hit_cb=hit)
            result_queue.put_nowait(("done", result))
        except Exception as exc:   # 让主线程统一提示
            result_queue.put_nowait(("error", exc))

    def _poll_queue(self):
        self._poll_job = None
        pending = self._queue
        if pending is None:
            return
        finished = False
        try:
            while True:
                message = pending.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, scanned, found = message
                    self.status_var.set(
                        "正在搜索… 已扫描 %s 个键，找到 %s 项"
                        % (format(scanned, ","), found))
                elif kind == "hit":
                    self._append_hit(message[1])
                elif kind == "done":
                    self._finish_search(message[1])
                    finished = True
                    break
                elif kind == "error":
                    self._handle_search_error(message[1])
                    finished = True
                    break
        except queue.Empty:
            pass
        if not finished and self._is_searching:
            self._poll_job = self.after(60, self._poll_queue)

    def _finish_search(self, result):
        self._is_searching = False
        self.progress.stop()
        self.progress.pack_forget()
        self._set_busy_ui(False)

        if not self._rows:
            if result.canceled:
                self._show_empty("搜索已停止，尚未找到匹配项。")
            else:
                self._show_empty(
                    "没有找到名称包含“%s”的注册表项。" % self._last_keyword)
        else:
            self._update_count()

        prefix = "搜索已停止" if result.canceled else "搜索完成"
        bits = ["扫描 %s 个键" % format(result.keys_scanned, ","),
                "找到 %s 项" % result.total_found]
        if result.errors:
            bits.append("%s 处无权访问" % result.errors)
        if result.truncated:
            bits.append("仅显示前 %s 项" % MAX_RESULTS)
        self.status_var.set(prefix + "：" + "，".join(bits))

    def _handle_search_error(self, exc):
        self._is_searching = False
        self.progress.stop()
        self.progress.pack_forget()
        self._set_busy_ui(False)
        if not self._rows:
            self._show_empty("搜索出错。")
        self._update_count()
        self.status_var.set("搜索出错。")
        messagebox.showerror(APP_TITLE, "搜索时发生错误：\n%s" % exc, parent=self)

    def _set_busy_ui(self, busy):
        state = "disabled" if busy else "normal"
        self.entry.configure(state=state)
        self.chk_hklm.configure(state=state)
        if busy:
            self.search_btn.configure(text="停止")
        else:
            self.search_btn.configure(text="搜索")
        self.search_btn.configure(state="normal")
        self.btn_all.configure(state=state)
        self.btn_delete.configure(state=state)

    # -- 删除流程 -----------------------------------------------------------
    def _next_backup_path(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(BACKUP_DIR, "注册表备份_%s.reg" % stamp)
        index = 1
        while os.path.exists(path):
            path = os.path.join(BACKUP_DIR, "注册表备份_%s_%d.reg" % (stamp, index))
            index += 1
        return path

    def _delete_selected(self):
        if self._is_searching:
            return
        selected = [row.item for row in self._rows if row.var.get()]
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先勾选要删除的注册表项。", parent=self)
            return

        key_items, value_items = effective_selection(selected)
        needs_admin = any(
            item.hive == winreg.HKEY_LOCAL_MACHINE
            for item in key_items + value_items
        )
        if needs_admin and not self._admin:
            if messagebox.askyesno(
                APP_TITLE,
                "所选项目包含系统级（HKLM）注册表，删除需要管理员权限。\n\n"
                "是否现在以管理员身份重新打开工具？\n"
                "（重新打开后会自动重新搜索，勾选需要重新进行。）",
                parent=self,
            ):
                self._elevate(self._last_keyword)
                return

        backup_path = self._next_backup_path()
        lines = ["将删除以下 %d 个已勾选的注册表项目："
                 % (len(key_items) + len(value_items))]
        if value_items:
            lines.append("  · 注册表值：%d 个" % len(value_items))
        if key_items:
            lines.append("  · 注册表项（键）：%d 个（会连同其下所有子项一起删除）"
                         % len(key_items))
        lines.append("")
        lines.append("删除前会自动备份到：\n%s" % backup_path)
        if needs_admin and not self._admin:
            lines.append("\n注意：当前不是管理员，系统级（HKLM）项目可能删除失败。")
        lines.append("\n删除后无法直接撤销，但可以通过备份文件恢复。确定继续吗？")

        if not messagebox.askyesno(
                "确认删除", "\n".join(lines), icon="warning", parent=self):
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            write_backup(key_items, value_items, backup_path)
        except Exception as exc:      # 备份失败绝不能继续删除
            self.configure(cursor="")
            messagebox.showerror(
                "备份失败",
                "无法完成备份，已取消删除。\n\n%s" % exc,
                parent=self,
            )
            return
        self.configure(cursor="")

        successes = []
        failures = []
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            for item in key_items:
                try:
                    delete_key_tree(item.hive, item.key_path)
                    successes.append(item)
                except OSError as exc:
                    failures.append((item, exc))
            for item in value_items:
                try:
                    delete_value_item(item)
                    successes.append(item)
                except OSError as exc:
                    failures.append((item, exc))
        finally:
            self.configure(cursor="")

        deleted_keys = [
            (item.hive, item.key_path.casefold())
            for item in successes if item.kind == "key"
        ]
        deleted_values = {id(item) for item in successes if item.kind == "value"}

        def is_gone(item):
            if id(item) in deleted_values:
                return True
            path = item.key_path.casefold()
            return any(
                item.hive == hive
                and (path == key_path or path.startswith(key_path + SEP))
                for hive, key_path in deleted_keys
            )

        self._remove_rows(is_gone)
        self._update_count()

        ok_count = len(successes)
        if failures:
            details = []
            for item, exc in failures[:5]:
                details.append("· %s\n  %s" % (item.primary, _describe_error(exc)))
            if len(failures) > 5:
                details.append("… 另有 %d 项失败" % (len(failures) - 5))

            message = (
                "成功删除 %d 项，失败 %d 项。\n\n备份文件：\n%s\n\n失败原因：\n%s"
                % (ok_count, len(failures), backup_path, "\n".join(details))
            )
            if any(isinstance(exc, PermissionError) for _item, exc in failures):
                message += "\n\n提示：可点击右上角“以管理员身份重启”后再试。"
            self.status_var.set(
                "删除完成：成功 %d 项，失败 %d 项" % (ok_count, len(failures)))
            messagebox.showwarning("删除完成", message, parent=self)
        else:
            self.status_var.set("删除完成：成功删除 %d 项" % ok_count)
            messagebox.showinfo(
                "删除完成",
                "已成功删除 %d 项。\n\n备份文件：\n%s" % (ok_count, backup_path),
                parent=self,
            )

    def _remove_rows(self, predicate):
        kept = []
        for row in self._rows:
            if predicate(row.item):
                row.frame.destroy()
                row.separator.destroy()
            else:
                kept.append(row)
        self._rows = kept
        if not self._rows:
            self._show_empty("列表已清空。")
        else:
            self._update_count()

    # -- 管理员权限 ---------------------------------------------------------
    def _ask_elevate(self):
        if self._is_searching:
            return
        keyword = self._last_keyword or self.entry.get_text() or None
        if messagebox.askyesno(
            APP_TITLE,
            "将以管理员身份重新打开工具，当前窗口会关闭。\n"
            "重新打开后会自动搜索上一次的关键字，勾选状态不会保留。\n\n继续吗？",
            parent=self,
        ):
            self._elevate(keyword)

    def _elevate(self, keyword=None):
        script = os.path.abspath(sys.argv[0])
        if not script.lower().endswith((".py", ".pyw")):
            script = os.path.abspath(__file__)
        args = [script]
        if keyword:
            args += ["--query", keyword]
        params = subprocess.list2cmdline(args)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, APP_DIR, 1)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE, "无法以管理员身份启动：\n%s" % exc, parent=self)
            return False
        if result <= 32:
            if result == 1223:      # 用户在 UAC 中点了“否”
                messagebox.showinfo(
                    APP_TITLE, "已取消以管理员身份启动。", parent=self)
            else:
                messagebox.showerror(
                    APP_TITLE,
                    "无法以管理员身份启动（返回码 %s）。" % result,
                    parent=self,
                )
            return False
        self.destroy()
        return True

    # -- 关闭 ---------------------------------------------------------------
    def _on_close(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.destroy()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_user_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    _enable_dpi_awareness()
    auto_query = None
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--query" and index + 1 < len(argv):
            auto_query = argv[index + 1]
    app = RegistryCleanerApp(auto_query=auto_query)
    app.mainloop()


if __name__ == "__main__":
    main()
