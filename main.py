import tkinter as tk
from tkinter import messagebox
import socket
import getpass
import shlex
import sys

APP_WIDTH, APP_HEIGHT = 720, 480

# RU (JCUKEN) -> QWERTY позиционное соответствие
RU_TO_QWERTY = {
    # верхний ряд
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p','х':'[','ъ':']',
    # средний ряд
    'ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k','д':'l','ж':';', 'э':"'",
    # нижний ряд
    'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m','б':',','ю':'.',
    # пробел и прочее оставляем как есть
}
# Добавим заглавные варианты
RU_TO_QWERTY.update({k.upper(): v.upper() for k, v in RU_TO_QWERTY.items()})

def translit_ru_to_qwerty(s: str) -> str:
    # Посимвольная замена: если символ есть в карте — заменяем, иначе оставляем
    return ''.join(RU_TO_QWERTY.get(ch, ch) for ch in s)

class ShellEmulatorGUI:
    def __init__(self, root):
        self.root = root
        self.username = getpass.getuser()
        self.hostname = socket.gethostname()
        self.prompt = f"{self.username}@{self.hostname}:~$"

        # режим "Латиница"
        self.latin_mode = True
        self._updating = False  # защита от рекурсии при заменах

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

        # Перевели Entry на StringVar + трассировку
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(bottom, textvariable=self.entry_var, font=("Consolas", 10))
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

        # Обработчики ввода
        self.entry_var.trace_add("write", self._on_entry_changed)
        self.entry.bind("<Return>", self.on_enter)

        # Горячая клавиша переключения режима латиницы: Ctrl+L
        root.bind_all("<Control-l>", self.toggle_latin_mode)

        self.println("Доступные команды: ls, cd, exit")
        self.println("Ctrl+L для переключения языка")
        self.print_prompt()
        self.entry.focus_set()

    def _update_title(self):
        self.root.title(f"[{self.username}@{self.hostname}]  —  Latin: {'ON' if self.latin_mode else 'OFF'}")

    def toggle_latin_mode(self, _event=None):
        self.latin_mode = not self.latin_mode
        self.println(f"[info] Latin mode: {'ON' if self.latin_mode else 'OFF'}")
        self._update_title()

    def _on_entry_changed(self, *_):
        if not self.latin_mode:
            return
        if self._updating:
            return
        s = self.entry_var.get()
        t = translit_ru_to_qwerty(s)
        if t != s:
            # Сохраним позицию каретки
            pos = self.entry.index("insert")
            self._updating = True
            try:
                self.entry_var.set(t)
            finally:
                self._updating = False
            # Вернём каретку на место
            try:
                self.entry.icursor(pos)
            except tk.TclError:
                pass

    def print_prompt(self):
        self.print_text(self.prompt + " ")

    def println(self, s=""):
        self.print_text(s + "\n")

    def print_text(self, s):
        self.output.configure(state="normal")
        self.output.insert("end", s)
        self.output.configure(state="disabled")
        self.output.see("end")

    def on_enter(self, _event):
        self.run_command()

    def run_command(self):
        line = self.entry.get().strip()
        self.println(self.prompt + " " + line)
        self.entry.delete(0, "end")

        if not line:
            self.print_prompt()
            return

        try:
            parts = shlex.split(line, posix=True)
        except ValueError as e:
            self.println(f"parse error: {e}")
            self.print_prompt()
            return

        cmd, *args = parts

        if cmd == "exit":
            self.cmd_exit()
            return
        elif cmd == "ls":
            self.cmd_ls(args)
        elif cmd == "cd":
            self.cmd_cd(args)
        else:
            self.println(f"{cmd}: command not found")

        self.print_prompt()

    def cmd_ls(self, args):
        self.println("[stub] ls called")
        self.println(f"args: {repr(args)}")

    def cmd_cd(self, args):
        self.println("[stub] cd called")
        self.println(f"args: {repr(args)}")
        if len(args) > 1:
            self.println("cd: too many arguments")

    def cmd_exit(self):
        answer = messagebox.askyesno("Выход", "Завершить работу эмулятора?")
        if answer:
            self.root.destroy()

def main():
    root = tk.Tk()
    app = ShellEmulatorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
