import tkinter as tk
from tkinter import messagebox
import socket
import getpass
import shlex
import sys
import os
import stat
import time
import argparse
import csv
import base64
import fnmatch
from typing import Optional, List, Dict, Tuple, Iterable

APP_WIDTH, APP_HEIGHT = 720, 480

# ========== Транслитерация RU->QWERTY ==========
RU_TO_QWERTY = {
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p','х':'[','ъ':']',
    'ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k','д':'l','ж':';','э':"'",
    'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m','б':',','ю':'.',
}
RU_TO_QWERTY.update({k.upper(): v.upper() for k, v in RU_TO_QWERTY.items()})

def translit_ru_to_qwerty(s: str) -> str:
    return ''.join(RU_TO_QWERTY.get(ch, ch) for ch in s)

# ========== Утиль ==========
def perms_to_string(mode: Optional[int], is_dir: bool) -> str:
    if mode is None:
        return 'drwxr-xr-x' if is_dir else '-rw-r--r--'
    if stat.S_ISDIR(mode): t = 'd'
    elif stat.S_ISLNK(mode): t = 'l'
    elif stat.S_ISCHR(mode): t = 'c'
    elif stat.S_ISBLK(mode): t = 'b'
    elif stat.S_ISSOCK(mode): t = 's'
    elif stat.S_ISFIFO(mode): t = 'p'
    else: t = '-'
    perm_bits = [
        (stat.S_IRUSR, 'r'), (stat.S_IWUSR, 'w'), (stat.S_IXUSR, 'x'),
        (stat.S_IRGRP, 'r'), (stat.S_IWGRP, 'w'), (stat.S_IXGRP, 'x'),
        (stat.S_IROTH, 'r'), (stat.S_IWOTH, 'w'), (stat.S_IXOTH, 'x'),
    ]
    out = []
    for bit, ch in perm_bits:
        out.append(ch if mode & bit else '-')
    return t + ''.join(out)

# ========== Абстракция ФС ==========
class IFs:
    def abspath(self, cwd: str, path: str) -> str: ...
    def is_dir(self, path: str) -> bool: ...
    def list_dir(self, path: str) -> List[str]: ...
    def lstat(self, path: str) -> Tuple[bool, Optional[int], int, float, str]: ...
    def exists(self, path: str) -> bool: ...
    def read_file(self, path: str) -> bytes: ...
    def walk(self, start: str) -> Iterable[Tuple[str, List[str], List[str]]]:
        """
        Аналог os.walk: yield (dirpath, dirnames, filenames), где dirpath — абсолютный путь в терминах этой ФС.
        """
        ...

# ========== Реальная ФС ==========
class OsFs(IFs):
    def abspath(self, cwd: str, path: str) -> str:
        if not path or path == "~":
            return os.path.expanduser("~")
        path = os.path.expanduser(path)
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(cwd, path))

    def is_dir(self, path: str) -> bool:
        try:
            st = os.stat(path)
            return stat.S_ISDIR(st.st_mode)
        except Exception:
            return False

    def list_dir(self, path: str) -> List[str]:
        return sorted(os.listdir(path))

    def lstat(self, path: str) -> Tuple[bool, Optional[int], int, float, str]:
        st = os.lstat(path)
        isdir = stat.S_ISDIR(st.st_mode)
        return (isdir, st.st_mode, st.st_size, st.st_mtime, os.path.basename(path))

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def read_file(self, path: str) -> bytes:
        with open(path, 'rb') as f:
            return f.read()

    def walk(self, start: str) -> Iterable[Tuple[str, List[str], List[str]]]:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames.sort()
            filenames.sort()
            yield dirpath, dirnames, filenames

# ========== VFS в памяти ==========
class VfsNode:
    def __init__(self, name: str, is_dir: bool):
        self.name = name
        self.is_dir = is_dir
        self.children: Dict[str, 'VfsNode'] = {}
        self.content: bytes = b''
        self.mode: Optional[int] = None
        self.mtime: float = time.time()

class MemoryVfs(IFs):
    """
    CSV-формат с заголовками: path,type,data_b64,mode,mtime
    type: dir|file, data_b64 — base64 для файлов (может быть пусто).
    """
    def __init__(self):
        self.root = VfsNode('/', True)

    def _norm(self, path: str) -> str:
        if not path:
            return '/'
        if not path.startswith('/'):
            path = '/' + path
        parts = []
        for chunk in path.split('/'):
            if chunk in ('', '.'):
                continue
            if chunk == '..':
                if parts: parts.pop()
                continue
            parts.append(chunk)
        return '/' + '/'.join(parts)

    def _ensure_dir(self, path: str) -> VfsNode:
        path = self._norm(path)
        if path == '/':
            return self.root
        node = self.root
        for part in path.strip('/').split('/'):
            nxt = node.children.get(part)
            if nxt is None:
                nxt = VfsNode(part, True)
                node.children[part] = nxt
            elif not nxt.is_dir:
                nxt.is_dir = True
                nxt.content = b''
            node = nxt
        return node

    def _get_node(self, path: str) -> Optional[VfsNode]:
        path = self._norm(path)
        if path == '/':
            return self.root
        node = self.root
        for part in path.strip('/').split('/'):
            node = node.children.get(part)
            if node is None:
                return None
        return node

    def load_from_csv(self, csv_path: str) -> None:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                p = (row.get('path') or '').strip()
                t = (row.get('type') or '').strip().lower()
                data_b64 = (row.get('data_b64') or '').strip()
                mode_raw = (row.get('mode') or '').strip()
                mtime_raw = (row.get('mtime') or '').strip()
                if not p or not t:
                    continue

                norm = self._norm(p)
                parent = os.path.dirname(norm)
                name = os.path.basename(norm)

                if t == 'dir':
                    d = self._ensure_dir(norm)
                    d.mtime = float(mtime_raw) if mtime_raw else d.mtime
                    if mode_raw:
                        try: d.mode = int(mode_raw, 0)
                        except ValueError: d.mode = None
                    continue

                if t == 'file':
                    dparent = self._ensure_dir(parent)
                    node = dparent.children.get(name)
                    if node is None:
                        node = VfsNode(name, False)
                        dparent.children[name] = node
                    node.is_dir = False
                    node.content = base64.b64decode(data_b64) if data_b64 else b''
                    node.mtime = float(mtime_raw) if mtime_raw else node.mtime
                    if mode_raw:
                        try: node.mode = int(mode_raw, 0)
                        except ValueError: node.mode = None
                    continue

    # ---- IFs ----
    def abspath(self, cwd: str, path: str) -> str:
        if not path or path == "~":
            return '/'
        if path.startswith('~'):
            path = path.replace('~', '/', 1)
        if path.startswith('/'):
            return self._norm(path)
        base = cwd if cwd else '/'
        if not base.startswith('/'):
            base = '/' + base
        return self._norm(os.path.join(base, path))

    def is_dir(self, path: str) -> bool:
        node = self._get_node(path)
        return bool(node and node.is_dir)

    def list_dir(self, path: str) -> List[str]:
        node = self._get_node(path)
        if not node or not node.is_dir:
            raise FileNotFoundError(path)
        return sorted(node.children.keys())

    def lstat(self, path: str) -> Tuple[bool, Optional[int], int, float, str]:
        node = self._get_node(path)
        if node is None:
            raise FileNotFoundError(path)
        size = len(node.content) if not node.is_dir else 0
        return (node.is_dir, node.mode, size, node.mtime, (node.name if node.name else '/'))

    def exists(self, path: str) -> bool:
        return self._get_node(path) is not None

    def read_file(self, path: str) -> bytes:
        node = self._get_node(path)
        if node is None:
            raise FileNotFoundError(path)
        if node.is_dir:
            raise IsADirectoryError(path)
        return node.content

    def walk(self, start: str) -> Iterable[Tuple[str, List[str], List[str]]]:
        # DFS обход
        start_node = self._get_node(start)
        if start_node is None or not start_node.is_dir:
            return
        def fulljoin(p, name):
            if p == '/': return '/' + name
            return p.rstrip('/') + '/' + name
        stack = [self._norm(start)]
        while stack:
            dpath = stack.pop()
            node = self._get_node(dpath)
            dirnames, filenames = [], []
            for name, child in sorted(node.children.items()):
                (dirnames if child.is_dir else filenames).append(name)
            # Имитация os.walk сверху-вниз: сначала текущая, потом дети
            yield dpath, dirnames, filenames
            # добавляем директории в стек в обратном порядке, чтобы лексикографически идти
            for dn in reversed(dirnames):
                stack.append(fulljoin(dpath, dn))

# ========== Укор. отображение пути в prompt ==========
def shorten_home_os(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path

# ========== Приложение ==========
class ShellEmulatorGUI:
    def __init__(self, root, fs: IFs, vfs_mode: bool, startup_script: Optional[str], args_debug: str):
        self.root = root
        self.username = getpass.getuser()
        self.hostname = socket.gethostname()
        self.fs = fs
        self.vfs_mode = vfs_mode
        self.cwd = '/' if vfs_mode else os.path.expanduser("~")
        self.startup_script = startup_script

        self.latin_mode = True
        self._updating = False

        self._update_title()

        root.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        root.minsize(560, 360)

        top = tk.Frame(root)
        top.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.output = tk.Text(top, state="disabled", wrap="word")
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(top, command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.config(yscrollcommand=scrollbar.set)

        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=8, pady=(4, 8))
        self.prompt_label = tk.Label(bottom, text=self._make_prompt(), font=("Consolas", 10))
        self.prompt_label.pack(side="left")

        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(bottom, textvariable=self.entry_var, font=("Consolas", 10))
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

        self.entry_var.trace_add("write", self._on_entry_changed)
        self.entry.bind("<Return>", self.on_enter)
        root.bind_all("<Control-l>", self.toggle_latin_mode)

        # Отладка
        self.println("=== Shell Emulator (Stage 4: Commands) ===")
        self.println(f"User: {self.username}  Host: {self.hostname}")
        self.println(args_debug)
        self.println(f"Mode: {'VFS(in-memory from CSV)' if self.vfs_mode else 'OS filesystem'}")
        self.println("Commands: ls [-a] [-l] [path...], cd [path], pwd, cat FILE..., find [PATH...] [-name PATTERN] [-type f|d] [-maxdepth N], exit")
        self.println("Ctrl+L — переключение «латиницы».")
        self.print_prompt()
        self.entry.focus_set()

        if self.startup_script:
            self.root.after(50, self._run_startup_script_safe)

    # ---- UI ----
    def _make_prompt(self) -> str:
        if self.vfs_mode:
            path_display = self.cwd if self.cwd != '/' else '~'
        else:
            path_display = shorten_home_os(self.cwd)
        return f"{self.username}@{self.hostname}:{path_display}$"

    def _update_title(self):
        self.root.title(f"[{self.username}@{self.hostname}]  —  Latin: {'ON' if self.latin_mode else 'OFF'}")

    def _refresh_prompt(self):
        self.prompt_label.config(text=self._make_prompt())

    def toggle_latin_mode(self, _event=None):
        self.latin_mode = not self.latin_mode
        self.println(f"[info] Latin mode: {'ON' if self.latin_mode else 'OFF'}")
        self._update_title()

    def _on_entry_changed(self, *_):
        if not self.latin_mode or self._updating:
            return
        s = self.entry_var.get()
        t = translit_ru_to_qwerty(s)
        if t != s:
            pos = self.entry.index("insert")
            self._updating = True
            try:
                self.entry_var.set(t)
            finally:
                self._updating = False
            try:
                self.entry.icursor(pos)
            except tk.TclError:
                pass

    def print_prompt(self):
        self.print_text(self._make_prompt() + " ")

    def println(self, s=""):
        self.print_text(s + "\n")

    def print_text(self, s):
        self.output.configure(state="normal")
        self.output.insert("end", s)
        self.output.configure(state="disabled")
        self.output.see("end")

    # ---- Обработка ввода ----
    def on_enter(self, _event):
        line = self.entry.get().strip()
        self.entry.delete(0, "end")
        self._execute_line_from_ui(line)

    def _execute_line_from_ui(self, line: str):
        self.println(self._make_prompt() + " " + line)
        if line:
            self._process_line(line)
        self.print_prompt()

    def _process_line(self, line: str):
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as e:
            self.println(f"parse error: {e}")
            return
        if not parts:
            return
        cmd, *args = parts

        if cmd == "exit": self.cmd_exit(); return
        if cmd == "pwd":  self.cmd_pwd(args); return
        if cmd == "cd":   self.cmd_cd(args);  return
        if cmd == "ls":   self.cmd_ls(args);  return
        if cmd == "cat":  self.cmd_cat(args); return
        if cmd == "find": self.cmd_find(args);return
        self.println(f"{cmd}: command not found")

    # ---- Команды ----
    def cmd_pwd(self, args: List[str]):
        if args:
            self.println("pwd: too many arguments"); return
        self.println(self.cwd)

    def cmd_cd(self, args: List[str]):
        target = args[0] if args else "~"
        path = self.fs.abspath(self.cwd, target)
        if not self.fs.exists(path):
            self.println(f"cd: no such file or directory: {target}"); return
        if not self.fs.is_dir(path):
            self.println(f"cd: not a directory: {target}"); return
        self.cwd = path
        self._refresh_prompt()

    def cmd_ls(self, args: List[str]):
        show_all = False
        long_fmt = False
        paths: List[str] = []
        for a in args:
            if a.startswith('-') and a != '-':
                if 'a' in a: show_all = True
                if 'l' in a: long_fmt = True
            else:
                paths.append(a)
        if not paths:
            paths = ["."]
        for i, p in enumerate(paths):
            abs_p = self.fs.abspath(self.cwd, p)
            if len(paths) > 1:
                self.println(f"{p}:")
            try:
                isdir, mode, size, mtime, name = self.fs.lstat(abs_p)
            except FileNotFoundError:
                self.println(f"ls: cannot access '{p}': No such file or directory")
                if i < len(paths) - 1: self.println()
                continue

            if isdir:
                try:
                    items = self.fs.list_dir(abs_p)
                except PermissionError:
                    self.println(f"ls: cannot open directory '{p}': Permission denied")
                    if i < len(paths) - 1: self.println()
                    continue
                entries: List[Tuple[str, str]] = []
                for it in items:
                    if not show_all and it.startswith('.'): continue
                    entries.append((it, self.fs.abspath(abs_p, it)))
                if long_fmt:
                    for nm, full in entries:
                        try:
                            isd, md, sz, mt, disp = self.fs.lstat(full)
                            perms = perms_to_string(md, isd)
                            nlink = 1
                            timestr = time.strftime("%b %d %H:%M", time.localtime(mt))
                            display = nm + ('/' if isd else '')
                            self.println(f"{perms} {nlink:3d} {sz:>8} {timestr} {display}")
                        except Exception as e:
                            self.println(f"ls: error reading '{nm}': {e}")
                else:
                    line = []
                    for nm, full in entries:
                        try:
                            isd, md, sz, mt, disp = self.fs.lstat(full)
                            line.append(nm + ('/' if isd else ''))
                        except Exception:
                            line.append(nm)
                    self.println("  ".join(line))
            else:
                if long_fmt:
                    perms = perms_to_string(mode, False)
                    nlink = 1
                    timestr = time.strftime("%b %d %H:%M", time.localtime(mtime))
                    self.println(f"{perms} {nlink:3d} {size:>8} {timestr} {name}")
                else:
                    self.println(name)
            if i < len(paths) - 1:
                self.println()

    def cmd_cat(self, args: List[str]):
        if not args:
            self.println("cat: missing operand"); return
        for i, p in enumerate(args):
            abs_p = self.fs.abspath(self.cwd, p)
            if not self.fs.exists(abs_p):
                self.println(f"cat: {p}: No such file or directory"); continue
            if self.fs.is_dir(abs_p):
                self.println(f"cat: {p}: Is a directory"); continue
            try:
                data = self.fs.read_file(abs_p)
            except Exception as e:
                self.println(f"cat: {p}: {e}"); continue
            try:
                text = data.decode('utf-8', errors='replace')
            except Exception:
                text = "<binary data>\n"
            # Печатаем содержимое как есть
            if i > 0:
                # как concat — просто продолжаем без лишних заголовков
                pass
            self.print_text(text if text.endswith('\n') else text + '\n')

    def cmd_find(self, args: List[str]):
        # Поддержка: find [PATH ...] [-name PATTERN] [-type f|d] [-maxdepth N]
        paths: List[str] = []
        name_pat: Optional[str] = None
        type_filter: Optional[str] = None  # 'f'|'d'|None
        maxdepth: Optional[int] = None

        it = iter(args)
        for a in it:
            if a == "-name":
                try: name_pat = next(it)
                except StopIteration:
                    self.println("find: missing argument to `-name'"); return
            elif a == "-type":
                try:
                    t = next(it)
                except StopIteration:
                    self.println("find: missing argument to `-type'"); return
                if t not in ("f","d"):
                    self.println("find: invalid argument to `-type' (use f|d)"); return
                type_filter = t
            elif a == "-maxdepth":
                try:
                    md = int(next(it))
                    if md < 0: raise ValueError()
                except (StopIteration, ValueError):
                    self.println("find: `-maxdepth' expects non-negative integer"); return
                maxdepth = md
            elif a.startswith('-'):
                self.println(f"find: unknown predicate: {a}"); return
            else:
                paths.append(a)

        if not paths:
            paths = ["."]
        for raw_start in paths:
            start = self.fs.abspath(self.cwd, raw_start)
            if not self.fs.exists(start):
                self.println(f"find: `{raw_start}': No such file or directory")
                continue

            # Вспомогательная функция для depth
            def depth_of(base: str, sub: str) -> int:
                b = base.rstrip('/')
                s = sub.rstrip('/')
                if b == '/':  # корень
                    return 0 if s=='/' else len([x for x in s.split('/') if x])
                if s == b: return 0
                rel = s[len(b):] if s.startswith(b) else s
                rel = rel.lstrip('/')
                return 0 if rel=='' else len(rel.split('/'))

            # Для файлов — поведение как в find: печатаем сам путь
            try:
                isdir, _, _, _, _ = self.fs.lstat(start)
            except FileNotFoundError:
                self.println(f"find: `{raw_start}': No such file or directory")
                continue

            if not isdir:
                if self._find_match(start, name_pat, type_filter, want_dir=None):
                    self.println(start)
                continue

            for dirpath, dirnames, filenames in self.fs.walk(start):
                # ограничение глубины: пропускаем детей, если глубина выше maxdepth
                if maxdepth is not None and depth_of(start, dirpath) > maxdepth:
                    continue

                # сначала сам dirpath (как обычный find)
                if self._find_match(dirpath, name_pat, type_filter, want_dir=True):
                    self.println(dirpath)

                # дети
                if maxdepth is None or depth_of(start, dirpath) < maxdepth:
                    for d in dirnames:
                        p = self.fs.abspath(dirpath, d)
                        if self._find_match(p, name_pat, type_filter, want_dir=True):
                            self.println(p)
                    for f in filenames:
                        p = self.fs.abspath(dirpath, f)
                        if self._find_match(p, name_pat, type_filter, want_dir=False):
                            self.println(p)

    def _find_match(self, path: str, name_pat: Optional[str], type_filter: Optional[str], want_dir: Optional[bool]) -> bool:
        try:
            isdir, _, _, _, name = self.fs.lstat(path)
        except FileNotFoundError:
            return False
        if want_dir is not None and isdir != want_dir:
            return False
        if type_filter == 'f' and isdir:
            return False
        if type_filter == 'd' and not isdir:
            return False
        if name_pat is not None and not fnmatch.fnmatch(name, name_pat):
            return False
        return True

    def cmd_exit(self):
        answer = messagebox.askyesno("Выход", "Завершить работу эмулятора?")
        if answer:
            self.root.destroy()

    # ---- Стартовый скрипт ----
    def _run_startup_script_safe(self):
        sp = self.startup_script
        if not sp:
            return
        try:
            with open(sp, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            self.println(f"[error] Cannot read startup script: {e}")
            return

        self.println(f"[script] executing: {sp}")
        for idx, raw in enumerate(lines, start=1):
            line = raw.rstrip("\n\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                self.println(self._make_prompt() + " " + stripped)
                self._process_line(stripped)
            except Exception as e:
                self.println(f"[script:L{idx}] error: {e}")
            self.print_prompt()
        self.println("[script] done.")

# ========== CLI / init ==========
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Shell Emulator (Stage 4)")
    p.add_argument("--vfs", dest="vfs_csv", help="Путь к CSV-файлу VFS (в памяти). Если не указан — используется реальная ФС.")
    p.add_argument("--script", dest="startup_script", help="Путь к стартовому скрипту команд.")
    return p.parse_args(argv)

def init_fs(vfs_csv: Optional[str]):
    logs = []
    if vfs_csv:
        try:
            if not os.path.isfile(vfs_csv):
                logs.append(f"[error] VFS CSV not found: {vfs_csv}")
                return OsFs(), False, logs
            vfs = MemoryVfs()
            vfs.load_from_csv(vfs_csv)
            logs.append(f"[info] VFS loaded from CSV: {vfs_csv}")
            return vfs, True, logs
        except Exception as e:
            logs.append(f"[error] Failed to load VFS CSV: {e!r}")
            return OsFs(), False, logs
    return OsFs(), False, logs

def main():
    args = parse_args(sys.argv[1:])
    fs, vfs_mode, vfs_logs = init_fs(args.vfs_csv)
    args_debug = f"Args: --vfs={args.vfs_csv or '(none)'}  --script={args.startup_script or '(none)'}"
    if vfs_logs:
        args_debug += "\n" + "\n".join(vfs_logs)
    root = tk.Tk()
    app = ShellEmulatorGUI(root, fs=fs, vfs_mode=vfs_mode, startup_script=args.startup_script, args_debug=args_debug)
    root.mainloop()

if __name__ == "__main__":
    main()
