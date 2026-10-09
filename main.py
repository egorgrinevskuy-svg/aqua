"""Аква — минимальное ядро v3.0. Всё остальное — патчами."""

import json
import os
import re
import threading
import time
import traceback
from difflib import SequenceMatcher

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

VERSION = "3.0"
BOT_NAME = "Аква"
USER_NAME = "Ты"
MAX_LINES = 400
MATCH = 0.75

DEFAULT_MEMORY = {
    "answers": {
        "привет": "Привет! Я Аква.",
        "как дела": "Отлично! Ты как?",
        "спасибо": "Пожалуйста.",
        "пока": "До встречи!",
    },
    "facts": {},
    "history": [],
    "settings": {"voice_on": True},
}


# ---- Голос: простой Android TTS через jnius ----

_voice_fn = None
_voice_kind = "none"


def _init_voice():
    global _voice_fn, _voice_kind
    try:
        from jnius import autoclass, PythonJavaClass, java_method
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        TTS = autoclass("android.speech.tts.TextToSpeech")
        Locale = autoclass("java.util.Locale")

        holder = {"ready": False}

        class L(PythonJavaClass):
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            @java_method("(I)V")
            def onInit(self, status):
                holder["ready"] = (status == 0)

        tts = TTS(activity, L())
        for _ in range(40):
            if holder["ready"]:
                break
            time.sleep(0.05)
        if not holder["ready"]:
            return

        ru = Locale("ru", "RU")
        if tts.isLanguageAvailable(ru) >= 0:
            tts.setLanguage(ru)

        def speak(text):
            tts.speak(str(text), TTS.QUEUE_FLUSH, None, "aqua")

        _voice_fn = speak
        _voice_kind = "Android TTS"
    except Exception as e:
        print(f"[voice] {e}")


def voice_speak(text):
    if _voice_fn is None:
        return
    text = re.sub(r"[*_`#>]", "", str(text)).strip()
    if not text:
        return
    if len(text) > 300:
        text = text[:300]
    threading.Thread(target=_voice_fn, args=(text,), daemon=True).start()


# ---- Утилиты ----

def norm(t):
    return t.lower().strip().replace("ё", "е")


def find_answer(q, answers):
    q = norm(q)
    best_k, best_s = None, 0.0
    for k in answers:
        s = SequenceMatcher(None, q, norm(k)).ratio()
        if s > best_s:
            best_s, best_k = s, k
    if best_k and best_s >= MATCH:
        return answers[best_k]
    return None


# ---- Загрузчик патчей ----

class PatchAPI:
    def __init__(self, app):
        self.app = app
        self.responses = {}
        self.commands = {}
        self.hooks = []
        self.startup = []
        self.loaded = []

    def add_response(self, q, a):
        self.responses[q] = a

    def add_responses(self, m):
        self.responses.update(m)

    def add_command(self, name, fn):
        self.commands[name.lower()] = fn

    def on_message(self, fn):
        self.hooks.append(fn)

    def on_startup(self, fn):
        self.startup.append(fn)

    def log(self, s):
        print(f"[patch] {s}")


def load_patches(api):
    folders = []
    here = os.path.dirname(os.path.abspath(__file__)) or "."
    folders.append(os.path.join(here, "patches"))
    for base in ("/sdcard/Aqua/patches", "/storage/emulated/0/Aqua/patches"):
        if os.path.isdir(base):
            folders.append(base)
            break

    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            path = os.path.join(folder, f)
            name = f[:-3]
            try:
                spec = __import__("importlib.util", fromlist=["util"]).util.spec_from_file_location(f"aqua_{name}", path)
                mod = spec.loader.load_module() if hasattr(spec.loader, "load_module") else None
                if mod is None:
                    import importlib.util as iu
                    spec = iu.spec_from_file_location(f"aqua_{name}", path)
                    mod = iu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                if hasattr(mod, "register"):
                    mod.register(api)
                    api.loaded.append((getattr(mod, "NAME", name), getattr(mod, "VERSION", "?")))
                    print(f"[patch] OK {name}")
            except Exception as e:
                print(f"[patch] {name}: {e}")
                traceback.print_exc()

    for fn in api.startup:
        try:
            fn()
        except Exception as e:
            print(f"[startup] {e}")


# ---- Приложение ----

class AquaApp(App):

    def build(self):
        self.title = "Аква"
        self.mem_file = os.path.join(self.user_data_dir, "mem.json")
        self.mem = self.load_mem()

        self.patches = PatchAPI(self)
        load_patches(self.patches)
        for q, a in self.patches.responses.items():
            self.mem["answers"].setdefault(q, a)

        self.waiting_q = None
        self.lines = []
        self.history = self.mem.get("history", [])[-20:]

        _init_voice()
        self.voice_on = self.mem["settings"].get("voice_on", True) and _voice_fn is not None

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(4))
        for text, handler in [
            ("Голос", self.toggle_voice),
            ("Память", self.show_memory),
            ("Патчи", self.show_patches),
            ("Очистить", self.clear),
        ]:
            b = Button(text=text)
            b.bind(on_press=handler)
            top.add_widget(b)
        root.add_widget(top)

        self.scroll = ScrollView()
        self.label = Label(text="", halign="left", valign="top", size_hint_y=None)
        self.label.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
        self.label.bind(texture_size=lambda i, s: setattr(i, "height", s[1] + dp(20)))
        self.scroll.add_widget(self.label)
        root.add_widget(self.scroll)

        bottom = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(6))
        self.input = TextInput(hint_text="Сообщение...", multiline=False)
        self.input.bind(on_text_validate=self.send)
        send = Button(text="→", size_hint_x=None, width=dp(70))
        send.bind(on_press=self.send)
        bottom.add_widget(self.input)
        bottom.add_widget(send)
        root.add_widget(bottom)

        info = f"Патчей: {len(self.patches.loaded)}. " if self.patches.loaded else ""
        voice = _voice_kind if _voice_fn else "нет"
        self.add(BOT_NAME, f"Аква v{VERSION}. {info}Голос: {voice}.")
        return root

    # ---- память ----

    def load_mem(self):
        import copy
        if os.path.exists(self.mem_file):
            try:
                with open(self.mem_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k, v in DEFAULT_MEMORY.items():
                        d.setdefault(k, copy.deepcopy(v))
                    return d
            except Exception:
                pass
        return copy.deepcopy(DEFAULT_MEMORY)

    def save_mem(self):
        tmp = self.mem_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.mem, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.mem_file)
        except Exception:
            pass

    # ---- чат ----

    def refresh(self):
        self.label.text = "\n".join(self.lines[-MAX_LINES:]) + "\n"
        Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.05)

    def add(self, who, text):
        for line in str(text).split("\n"):
            self.lines.append(f"{who}: {line}")
        self.refresh()
        if who == BOT_NAME and self.voice_on:
            voice_speak(text)

    def toggle_voice(self, *_):
        if _voice_fn is None:
            self.add(BOT_NAME, "Голос недоступен.")
            return
        self.voice_on = not self.voice_on
        self.mem["settings"]["voice_on"] = self.voice_on
        self.save_mem()
        self.add(BOT_NAME, f"Голос {'вкл' if self.voice_on else 'выкл'}.")
        if self.voice_on:
            voice_speak("Голос включён")

    def clear(self, *_):
        self.lines = []
        self.label.text = ""
        self.add(BOT_NAME, "Чат очищен.")

    # ---- входящее ----

    def send(self, *_):
        text = self.input.text.strip()
        if not text:
            return
        self.input.text = ""
        self.add(USER_NAME, text)

        if self.waiting_q is not None:
            self.learn(text)
            return

        # хуки патчей
        for h in self.patches.hooks:
            try:
                r = h(text)
                if r:
                    self.add(BOT_NAME, r)
                    return
            except Exception as e:
                print(f"[hook] {e}")

        # базовые команды
        low = norm(text)

        if low in ("время", "который час"):
            self.add(BOT_NAME, time.strftime("%H:%M:%S, %d.%m.%Y"))
            return
        if low.startswith("посчитай"):
            self.calc(text[8:].strip())
            return
        if low.startswith("запомни"):
            m = re.fullmatch(r"запомни[,\s]+(.+?)\s*[:=]\s*(.+)", text, re.IGNORECASE)
            if m:
                self.mem["facts"][m.group(1).strip().lower()] = m.group(2).strip()
                self.save_mem()
                self.add(BOT_NAME, "Запомнил.")
                return
        if low in ("что ты знаешь обо мне", "мои факты"):
            facts = self.mem.get("facts", {})
            if facts:
                self.add(BOT_NAME, "Знаю:\n" + "\n".join(f"• {k}: {v}" for k, v in facts.items()))
            else:
                self.add(BOT_NAME, "Пока ничего.")
            return
        if low.startswith("выучи"):
            m = re.fullmatch(r"выучи\s+(.+?)\s*=\s*(.+)", text, re.IGNORECASE)
            if m:
                self.mem["answers"][m.group(1).strip()] = m.group(2).strip()
                self.save_mem()
                self.add(BOT_NAME, "Выучил.")
                return

        # команды патчей
        for name, fn in self.patches.commands.items():
            if low.startswith(name):
                try:
                    if fn(text):
                        return
                except Exception as e:
                    self.add(BOT_NAME, f"Патч: {e}")
                    return

        # ответы
        ans = find_answer(text, self.mem["answers"])
        if ans:
            self.add(BOT_NAME, ans)
            return

        self.waiting_q = text
        self.add(BOT_NAME, "Не знаю. Напиши ответ — запомню. Или «отмена».")

    def learn(self, text):
        q = self.waiting_q
        self.waiting_q = None
        if norm(text) in ("отмена", "стоп", "cancel"):
            self.add(BOT_NAME, "Отменено.")
            return
        self.mem["answers"][q] = text
        self.save_mem()
        self.add(BOT_NAME, "Запомнил.")

    def calc(self, expr):
        expr = expr.replace(",", ".").replace("^", "**")
        if not re.fullmatch(r"[0-9+\-*/%().\s]+", expr):
            self.add(BOT_NAME, "Только цифры и знаки.")
            return
        try:
            r = eval(expr, {"__builtins__": {}}, {})
            self.add(BOT_NAME, f"Ответ: {r}")
        except Exception:
            self.add(BOT_NAME, "Не смог посчитать.")

    # ---- окна ----

    def show_memory(self, *_):
        facts = self.mem.get("facts", {})
        text = (
            f"Ответов: {len(self.mem['answers'])}\n"
            f"Фактов о тебе: {len(facts)}\n"
            f"Патчей: {len(self.patches.loaded)}\n"
            f"Голос: {'вкл' if self.voice_on else 'выкл'}\n"
        )
        if facts:
            text += "\nФакты:\n" + "\n".join(f"• {k}: {v}" for k, v in facts.items())
        popup = Popup(
            title="Память",
            content=Label(text=text, halign="left", valign="top"),
            size_hint=(0.9, 0.6),
        )
        popup.open()

    def show_patches(self, *_):
        if not self.patches.loaded:
            text = "Патчей нет.\nКидай .py в /sdcard/Aqua/patches/"
        else:
            text = "Загружено:\n" + "\n".join(f"• {n} v{v}" for n, v in self.patches.loaded)
        popup = Popup(
            title="Патчи",
            content=Label(text=text, halign="left", valign="top"),
            size_hint=(0.9, 0.7),
        )
        popup.open()


if __name__ == "__main__":
    AquaApp().run()
