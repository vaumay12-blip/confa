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
from typing import Optional, List, Tuple

APP_WIDTH, APP_HEIGHT = 720, 480

# RU (JCUKEN) -> QWERTY позиционное соответствие
RU_TO_QWERTY = {
    # верхний ряд
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p','х':'[','ъ':']',
    # средний ряд
    'ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k','д':'l','ж':';', 'э':"'",
    # нижний ряд
    'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m','б':',','ю':'.',
}
RU_TO_QWERTY.update({k.upper(): v.upper() for k, v in RU_TO_QWERTY.items()})

def translit_ru_to_qwerty(s: str) -> str:
    return ''.join(RU_TO_QWERTY.get(ch, ch) for ch in s)

def perms_to_string(mode: int) -> str:
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

def human_size(n: int) -> str:
    for unit in ['B','K','M','G','T','P']:
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}E"

def abspath_from(cwd: str, path: str) -> str:
    if not path or path == "~":
        return os.path.expanduser("~")
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(cwd, path))

def shorten_home(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path

class ShellEmulatorGUI:
    def __init__(self, root, vfs_root: Optional[str], startup_script: Optional[str]):
        self.root = root
        self.username = getpass.getuser()
        self.hostname = socket.gethostname()
        self.cwd = self._init_cwd(vfs_root)
        self.prompt = self._make_prompt()

        # Параметры запуска (для отладочного вывода)
        self.vfs_root = vfs_root
        self.startup_script = startup_script

        # режим "Латиница"
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
        self.prompt_label = tk.Label(bottom, text=self.prompt, font=("Consolas", 10))
        self.prompt_label.pack(side="left")

        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(bottom, textvariable=self.entry_var, font=("Consolas", 10))
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

        self.entry_var.trace_add("write", self._on_entry_changed)
        self.entry.bind("<Return>", self.on_enter)

        root.bind_all("<Control-l>", self.toggle_latin_mode)

        # Отладочный вывод параметров
        self.println("=== Shell Emulator ===")
        self.println(f"User: {self.username}  Host: {self.hostname}")
        self.println(f"VFS root: {self.vfs_root if self.vfs_root else '(not set)'}")
        self.println(f"Startup script: {self.startup_script if self.startup_script else '(not set)'}")
        self.println("Доступные команды: ls [-a] [-l] [path], cd [path], pwd, exit")
        self.println("Ctrl+L — переключение «латиницы» (на случай застрявшей RU-раскладки).")

        self.print_prompt()
        self.entry.focus_set()

        # После построения UI — если задан скрипт, выполнить его
        if self.startup_script:
            self.root.after(50, self._run_startup_script_safe)

    # ===== Инициализация CWD из VFS =====

    def _init_cwd(self, vfs_root: Optional[str]) -> str:
        if vfs_root:
            try:
                p = os.path.abspath(os.path.expanduser(vfs_root))
                st = os.stat(p)
                if not stat.S_ISDIR(st.st_mode):
                    # Не директория — сообщение дадим позже в логе
                    return os.path.expanduser("~")
                return p
            except Exception:
                # Не существует / нет прав — уйдём в ~, а причину выведем в лог при старте
                return os.path.expanduser("~")
        return os.path.expanduser("~")

    # ===== Вспомогательные UI =====

    def _make_prompt(self) -> str:
        return f"{self.username}@{self.hostname}:{shorten_home(self.cwd)}$"

    def _update_title(self):
        self.root.title(f"[{self.username}@{self.hostname}]  —  Latin: {'ON' if self.latin_mode else 'OFF'}")

    def _refresh_prompt(self):
        self.prompt = self._make_prompt()
        self.prompt_label.config(text=self.prompt)

    # ===== Latin mode =====

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

    # ===== Вывод =====

    def print_prompt(self):
        self.print_text(self.prompt + " ")

    def println(self, s=""):
        self.print_text(s + "\n")

    def print_text(self, s):
        self.output.configure(state="normal")
        self.output.insert("end", s)
        self.output.configure(state="disabled")
        self.output.see("end")

    # ===== Обработка команд =====

    def on_enter(self, _event):
        line = self.entry.get().strip()
        self.entry.delete(0, "end")
        self._execute_line_from_ui(line)

    def _execute_line_from_ui(self, line: str):
        # Имитация диалога: печатаем приглашение + команду, затем выполняем
        self.println(self.prompt + " " + line)
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

        if cmd == "exit":
            self.cmd_exit()
            return
        elif cmd == "ls":
            self.cmd_ls(args)
        elif cmd == "cd":
            self.cmd_cd(args)
        elif cmd == "pwd":
            self.cmd_pwd(args)
        else:
            self.println(f"{cmd}: command not found")

    # ===== Реализация команд =====

    def cmd_pwd(self, args: List[str]):
        if args:
            self.println("pwd: too many arguments")
            return
        self.println(self.cwd)

    def cmd_cd(self, args: List[str]):
        target = args[0] if args else "~"
        path = abspath_from(self.cwd, target)
        try:
            st = os.stat(path)
            if not stat.S_ISDIR(st.st_mode):
                self.println(f"cd: not a directory: {target}")
                return
        except FileNotFoundError:
            self.println(f"cd: no such file or directory: {target}")
            return
        except PermissionError:
            self.println(f"cd: permission denied: {target}")
            return
        self.cwd = path
        self._refresh_prompt()

    def cmd_ls(self, args: List[str]):
        show_all = False
        long_fmt = False
        paths = []

        for a in args:
            if a.startswith('-') and a != '-':
                if 'a' in a: show_all = True
                if 'l' in a: long_fmt = True
            else:
                paths.append(a)

        if not paths:
            paths = ["."]
        for i, p in enumerate(paths):
            abs_p = abspath_from(self.cwd, p)
            if len(paths) > 1:
                self.println(f"{p}:")
            try:
                st = os.stat(abs_p)
            except FileNotFoundError:
                self.println(f"ls: cannot access '{p}': No such file or directory")
                if i < len(paths) - 1:
                    self.println()
                continue
            except PermissionError:
                self.println(f"ls: cannot open directory '{p}': Permission denied")
                if i < len(paths) - 1:
                    self.println()
                continue

            if stat.S_ISDIR(st.st_mode):
                try:
                    items = os.listdir(abs_p)
                except PermissionError:
                    self.println(f"ls: cannot open directory '{p}': Permission denied")
                    if i < len(paths) - 1:
                        self.println()
                    continue
                entries = []
                for name in sorted(items):
                    if not show_all and name.startswith('.'):
                        continue
                    full = os.path.join(abs_p, name)
                    entries.append((name, full))
                if long_fmt:
                    for name, full in entries:
                        try:
                            st_i = os.lstat(full)
                            perms = perms_to_string(st_i.st_mode)
                            nlink = st_i.st_nlink
                            size = st_i.st_size
                            mtime = time.strftime("%b %d %H:%M", time.localtime(st_i.st_mtime))
                            display = name + ('/' if stat.S_ISDIR(st_i.st_mode) else '')
                            self.println(f"{perms} {nlink:3d} {size:>8} {mtime} {display}")
                        except OSError as e:
                            self.println(f"ls: error reading '{name}': {e}")
                else:
                    line = []
                    for name, full in entries:
                        try:
                            st_i = os.lstat(full)
                            display = name + ('/' if stat.S_ISDIR(st_i.st_mode) else '')
                        except OSError:
                            display = name
                        line.append(display)
                    self.println("  ".join(line))
            else:
                name = os.path.basename(abs_p)
                if long_fmt:
                    perms = perms_to_string(st.st_mode)
                    nlink = st.st_nlink
                    size = st.st_size
                    mtime = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
                    self.println(f"{perms} {nlink:3d} {size:>8} {mtime} {name}")
                else:
                    self.println(name)
            if i < len(paths) - 1:
                self.println()

    def cmd_exit(self):
        answer = messagebox.askyesno("Выход", "Завершить работу эмулятора?")
        if answer:
            self.root.destroy()

    # ===== Запуск стартового скрипта =====

    def _run_startup_script_safe(self):
        # Доп. диагностические сообщения про vfs/script корректность
        if self.vfs_root:
            p = os.path.abspath(os.path.expanduser(self.vfs_root))
            if not os.path.isdir(p):
                self.println(f"[warn] VFS path is not a directory or not accessible: {self.vfs_root}")
        if self.startup_script:
            sp = os.path.abspath(os.path.expanduser(self.startup_script))
            if not os.path.isfile(sp):
                self.println(f"[error] Startup script not found: {self.startup_script}")
                return
            try:
                self._execute_script(sp)
            except Exception as e:
                self.println(f"[script] unexpected error: {e!r}")

    def _execute_script(self, script_path: str):
        self.println(f"[script] executing: {script_path}")
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            self.println(f"[script] cannot read file: {e}")
            return

        for idx, raw in enumerate(lines, start=1):
            line = raw.rstrip("\n\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                # Комментарии/пустые строки — просто показать как есть (опционально)
                continue
            try:
                # Имитация ввода пользователя:
                self.println(self.prompt + " " + stripped)
                self._process_line(stripped)
            except Exception as e:
                self.println(f"[script:L{idx}] error: {e}")
            # После каждой команды показываем следующий prompt
            self.print_prompt()

        self.println(f"[script] done.")

# ====== CLI и точка входа ======

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Shell Emulator (Stage 2: Configuration)")
    p.add_argument("--vfs", dest="vfs_root", help="Путь к корню виртуальной файловой системы (стартовая директория).")
    p.add_argument("--script", dest="startup_script", help="Путь к стартовому скрипту команд.")
    return p.parse_args(argv)

def main():
    args = parse_args(sys.argv[1:])
    root = tk.Tk()
    app = ShellEmulatorGUI(root, vfs_root=args.vfs_root, startup_script=args.startup_script)
    root.mainloop()

if __name__ == "__main__":
    main()
