"""Аква — умный чат-бот v2.0."""

import ast
import copy
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from difflib import SequenceMatcher

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput


AQUA_VERSION = "2.0"
BOT_NAME = "Аква"
USER_NAME = "Ты"

ALLOWED_EXTENSIONS = (".txt", ".py", ".json", ".csv", ".md", ".log", ".ini", ".cfg")
MAX_DOCUMENTS = 10
MAX_DOC_CHARS = 100_000
MAX_DOC_FILE_BYTES = 5_000_000
MAX_HISTORY = 20
MAX_CHAT_LINES = 400
MATCH_THRESHOLD = 0.70
LLM_TIMEOUT = 60

DEFAULT_ANSWERS = {
    "привет": "Привет! Я Аква.",
    "как тебя зовут": "Меня зовут Аква.",
    "что ты умеешь": "Считаю, читаю файлы, помню тебя. Напиши «помощь».",
    "пока": "До встречи!",
}

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Аква, полезный и дружелюбный ассистент. "
    "Отвечай кратко и по делу."
)

DEFAULT_MEMORY = {
    "answers": DEFAULT_ANSWERS,
    "documents": {},
    "user_facts": {},
    "chat_history": [],
    "settings": {
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "voice_on": True,
    },
    "patches": {},
}

SEED_ANSWERS = {
    "привет": "Привет! Чем помочь?",
    "здравствуй": "Здравствуй!",
    "как дела": "Отлично! Ты как?",
    "как ты": "Работаю. Ты как?",
    "спасибо": "Пожалуйста!",
    "до свидания": "До связи!",
    "твое имя": "Аква.",
    "кто ты": "Я Аква — локальный бот.",
    "ты тут": "Тут.",
    "аква": "Я здесь.",
    r"re:^сколько\s+будет\s+(.+)$": "Напиши: посчитай 2 + 2",
    "столица россии": "Москва.",
    "столица франции": "Париж.",
    "столица японии": "Токио.",
    "кто написал войну и мир": "Лев Толстой.",
    "расскажи шутку": "Oct 31 == Dec 25.",
    "мне грустно": "Побудь с этим. Если тяжело — поговори с близкими.",
    "как сосредоточиться": "Помодоро: 25 минут работы, 5 отдыха.",
    "что такое имхо": "IMHO — по моему скромному мнению.",
}


class PatchAPI:
    def __init__(self, app):
        self.app = app
        self.commands = {}
        self.responses = {}
        self.message_hooks = []
        self.startup_hooks = []
        self.llm_context_hooks = []
        self.avatar_states = {}
        self.loaded = []

    def add_response(self, q, a):
        self.responses[q] = a

    def add_responses(self, m):
        self.responses.update(m)

    def add_command(self, name, handler):
        self.commands[name.lower()] = handler

    def on_message(self, handler):
        self.message_hooks.append(handler)

    def on_startup(self, handler):
        self.startup_hooks.append(handler)

    def on_llm_context(self, handler):
        self.llm_context_hooks.append(handler)

    def set_avatar_state(self, state, image_path):
        self.avatar_states[state] = image_path

    def switch_avatar(self, state):
        self.app.set_avatar_state(state)

    def log(self, text):
        print(f"[patch] {text}")

    def system_prompt_extra(self):
        parts = []
        for fn in self.llm_context_hooks:
            try:
                v = fn()
                if v:
                    parts.append(v)
            except Exception:
                pass
        return "\n".join(parts)


def _load_patch_file(path, api):
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        spec = importlib.util.spec_from_file_location(f"aqua_patch_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[patch] {name}: {e}")
        return
    if not hasattr(mod, "register"):
        return
    try:
        mod.register(api)
    except Exception as e:
        print(f"[patch] {name} register: {e}")
        traceback.print_exc()
        return
    api.loaded.append((getattr(mod, "NAME", name), getattr(mod, "VERSION", "?"), path))


def load_all_patches(api):
    seen = set()
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
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            if fname in seen:
                continue
            seen.add(fname)
            _load_patch_file(os.path.join(folder, fname), api)
    for fn in api.startup_hooks:
        try:
            fn()
        except Exception as e:
            print(f"[startup hook] {e}")


_SYSTEM = platform.system()


def _is_android():
    keys = ("ANDROID_ARGUMENT", "ANDROID_ROOT", "ANDROID_DATA", "PREFIX")
    return any(k in os.environ for k in keys) or os.path.exists("/system/build.prop")


class VoiceEngine:
    def __init__(self):
        self.speak_fn = None
        self.kind = "none"
        self._detect()

    def _try(self, name, fn):
        try:
            r = fn()
            if r is None:
                raise RuntimeError("None")
            return r
        except Exception as e:
            print(f"[voice] {name}: {e}")
            return None

    def _find_activity(self):
        from jnius import autoclass
        for path in ("org.kivy.android.PythonActivity", "org.renpy.android.PythonActivity"):
            try:
                cls = autoclass(path)
                if cls.mActivity is not None:
                    return cls.mActivity
            except Exception:
                continue
        raise RuntimeError("no activity")

    def _init_android_tts(self):
        from jnius import autoclass, PythonJavaClass, java_method
        activity = self._find_activity()
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
        for _ in range(60):
            if holder["ready"]:
                break
            time.sleep(0.05)
        if not holder["ready"]:
            raise RuntimeError("no init")
        ru = Locale("ru", "RU")
        if tts.isLanguageAvailable(ru) < 0:
            raise RuntimeError("no ru")
        tts.setLanguage(ru)

        def speak(text):
            tts.speak(str(text), TTS.QUEUE_FLUSH, None, "aqua")
        return speak

    def _init_gtts_android(self):
        from gtts import gTTS
        from jnius import autoclass
        MP = autoclass("android.media.MediaPlayer")

        def speak(text):
            p = os.path.join(tempfile.gettempdir(), "aqua_tts.mp3")
            gTTS(text=str(text), lang="ru").save(p)
            player = MP()
            try:
                player.setDataSource(p)
                player.prepare()
                player.start()
                while player.isPlaying():
                    time.sleep(0.1)
            finally:
                player.release()
        return speak

    def _init_pyttsx3(self):
        import pyttsx3
        eng = pyttsx3.init()
        eng.setProperty("rate", 180)

        def speak(text):
            eng.say(str(text))
            eng.runAndWait()
        return speak

    def _init_gtts_desktop(self):
        from gtts import gTTS
        try:
            import pygame
            pygame.mixer.init()
        except Exception:
            pygame = None

        def speak(text):
            p = os.path.join(tempfile.gettempdir(), "aqua_tts.mp3")
            gTTS(text=str(text), lang="ru").save(p)
            if pygame:
                pygame.mixer.music.load(p)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.05)
                return
            for cmd in (["mpg123", "-q", p], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", p]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd)
                    return
        return speak

    def _init_say(self):
        def speak(text):
            subprocess.run(["say", str(text)])
        return speak

    def _init_espeak(self):
        for cmd in ("espeak-ng", "espeak", "spd-say"):
            if shutil.which(cmd):
                def speak(text, _cmd=cmd):
                    subprocess.run([_cmd, str(text)])
                return speak
        return None

    def _detect(self):
        if _is_android():
            fn = self._try("gtts_android", self._init_gtts_android)
            if fn:
                self.speak_fn = fn
                self.kind = "gTTS онлайн"
                return
            fn = self._try("android_tts", self._init_android_tts)
            if fn:
                self.speak_fn = fn
                self.kind = "Android TTS"
                return
        if _SYSTEM == "Darwin":
            fn = self._try("say", self._init_say)
            if fn:
                self.speak_fn = fn
                self.kind = "say"
                return
        fn = self._try("pyttsx3", self._init_pyttsx3)
        if fn:
            self.speak_fn = fn
            self.kind = "pyttsx3"
            return
        if _SYSTEM == "Linux":
            fn = self._try("espeak", self._init_espeak)
            if fn:
                self.speak_fn = fn
                self.kind = "espeak"
                return
        fn = self._try("gtts", self._init_gtts_desktop)
        if fn:
            self.speak_fn = fn
            self.kind = "gTTS"
            return

    def available(self):
        return self.speak_fn is not None

    def speak(self, text, async_mode=True):
        if not self.speak_fn:
            return False
        text = str(text).strip()
        if not text:
            return False
        if async_mode:
            threading.Thread(target=self.speak_fn, args=(text,), daemon=True).start()
        else:
            self.speak_fn(text)
        return True


_voice = VoiceEngine()


def clean_for_speech(text):
    t = str(text)
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)
    t = re.sub(r"[*_`#>]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 300:
        t = t[:300] + "…"
    return t


def normalize(text):
    return text.lower().strip().replace("ё", "е")


_ARITH_ALLOWED = re.compile(r"[0-9+\-*/%().,\s^]+")


def safe_calculate(expression):
    expr = expression.replace(",", ".").replace("^", "**").strip()
    if not expr or not _ARITH_ALLOWED.fullmatch(expr):
        raise ValueError("bad chars")
    tree = ast.parse(expr, mode="eval")

    def calc(node):
        if isinstance(node, ast.Expression):
            return calc(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                raise ValueError("bool")
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("not num")
        if isinstance(node, ast.UnaryOp):
            n = calc(node.operand)
            if isinstance(node.op, ast.USub):
                return -n
            if isinstance(node.op, ast.UAdd):
                return n
            raise ValueError("bad unary")
        if isinstance(node, ast.BinOp):
            a, b = calc(node.left), calc(node.right)
            op = node.op
            if isinstance(op, ast.Add): return a + b
            if isinstance(op, ast.Sub): return a - b
            if isinstance(op, ast.Mult): return a * b
            if isinstance(op, ast.Div): return a / b
            if isinstance(op, ast.FloorDiv): return a // b
            if isinstance(op, ast.Mod): return a % b
            if isinstance(op, ast.Pow):
                if abs(b) > 100:
                    raise ValueError("big pow")
                return a ** b
            raise ValueError("bad op")
        raise ValueError("bad node")

    return calc(tree)


def find_answer(question, answers):
    q = normalize(question)
    if not q:
        return None
    best_q, best_score = None, 0.0
    for saved in answers:
        score = SequenceMatcher(None, q, normalize(saved)).ratio()
        if score > best_score:
            best_score, best_q = score, saved
    if best_q and best_score >= MATCH_THRESHOLD:
        return answers[best_q]
    return None


def _time_of_day():
    h = time.localtime().tm_hour
    if 6 <= h < 12: return "morning"
    if 12 <= h < 18: return "day"
    if 18 <= h < 23: return "evening"
    return "night"


def find_answer_advanced(text, answers):
    q = normalize(text)
    if not q:
        return None
    for key, value in answers.items():
        if key.startswith("re:"):
            try:
                if re.search(key[3:], q):
                    return value
            except re.error:
                continue
    tod = _time_of_day()
    for key, value in answers.items():
        if key.startswith("time:"):
            try:
                cond, trigger = key[5:].split(" ", 1)
            except ValueError:
                continue
            if tod in cond.split("|"):
                if SequenceMatcher(None, q, normalize(trigger)).ratio() >= MATCH_THRESHOLD:
                    return value
    plain = {k: v for k, v in answers.items() if not k.startswith(("re:", "time:"))}
    return find_answer(text, plain)


def scrollable_popup(title, text, size_hint=(0.9, 0.7)):
    label = Label(text=text, halign="left", valign="top", size_hint_y=None)
    label.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
    label.bind(texture_size=lambda i, s: setattr(i, "height", s[1] + dp(20)))
    scroll = ScrollView()
    scroll.add_widget(label)
    return Popup(title=title, content=scroll, size_hint=size_hint)


class SmartBotApp(App):

    def build(self):
        self.title = f"Умный чат-бот {BOT_NAME}"
        self.memory_file = os.path.join(self.user_data_dir, "bot_memory.json")
        self.memory = self.load_memory()
        self.waiting_question = None
        self.chat_lines = []
        self.history = list(self.memory.get("chat_history", []))
        self.root_ref = None
        self.avatar_image = None

        self.patches = PatchAPI(self)
        load_all_patches(self.patches)
        for q, a in self.patches.responses.items():
            if q not in self.memory["answers"]:
                self.memory["answers"][q] = a

        want_voice = self.memory["settings"].get("voice_on", True)
        self.voice_on = bool(want_voice) and _voice.available()

        root = BoxLayout(orientation="vertical", padding=dp(6), spacing=dp(4))
        self.root_ref = root

        self.avatar_box = BoxLayout(size_hint_y=None, height=0)
        root.add_widget(self.avatar_box)

        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(4))
        for text, handler in [
            ("Файл", self.open_file_popup),
            ("Память", self.show_memory),
            ("Настройки", self.show_settings),
            ("Помощь", self.show_help),
            ("Очистить", self.clear_chat),
            ("🔊", self.toggle_voice),
        ]:
            b = Button(text=text)
            b.bind(on_press=handler)
            top.add_widget(b)
        self.top_bar = top
        root.add_widget(top)

        self.scroll = ScrollView()
        self.chat_label = Label(text="", halign="left", valign="top", size_hint_y=None)
        self.chat_label.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
        self.chat_label.bind(texture_size=lambda i, s: setattr(i, "height", s[1] + dp(20)))
        self.scroll.add_widget(self.chat_label)
        root.add_widget(self.scroll)

        self.bottom_custom_box = BoxLayout(size_hint_y=None, height=0)
        root.add_widget(self.bottom_custom_box)

        bottom = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(6))
        self.user_input = TextInput(hint_text="Сообщение...", multiline=False)
        self.user_input.bind(on_text_validate=self.send_message)
        send = Button(text="→", size_hint_x=None, width=dp(70))
        send.bind(on_press=self.send_message)
        bottom.add_widget(self.user_input)
        bottom.add_widget(send)
        root.add_widget(bottom)

        patch_info = f" Патчей: {len(self.patches.loaded)}." if self.patches.loaded else ""
        self.add_message(BOT_NAME, f"Привет! Я Аква v{AQUA_VERSION}.{patch_info}")
        Clock.schedule_once(lambda dt: self._voice_startup_report(), 0.5)
        return root

    def set_avatar_state(self, state):
        if not self.patches.avatar_states:
            return
        path = self.patches.avatar_states.get(state)
        if not path or not os.path.exists(path):
            return
        if self.avatar_image is None:
            self.avatar_image = Image(source=path, allow_stretch=True)
            self.avatar_box.height = dp(180)
            self.avatar_box.add_widget(self.avatar_image)
        else:
            self.avatar_image.source = path

    def _voice_startup_report(self):
        if _voice.available():
            self.add_message(BOT_NAME, f"Голос: {_voice.kind}.")
        else:
            self.add_message(BOT_NAME, "🔇 Голос не запустился.")

    def toggle_voice(self, *_):
        if not _voice.available():
            self.add_message(BOT_NAME, "🔇 Голос недоступен.")
            return
        self.voice_on = not self.voice_on
        self.memory["settings"]["voice_on"] = self.voice_on
        self.save_memory()
        state = "включён" if self.voice_on else "выключен"
        self.add_message(BOT_NAME, f"Голос {state}.")
        if self.voice_on:
            _voice.speak(f"Голос {state}.")

    def _refresh_chat(self):
        self.chat_label.text = "\n".join(self.chat_lines) + "\n"
        Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.05)

    def _trim_chat(self):
        if len(self.chat_lines) > MAX_CHAT_LINES:
            del self.chat_lines[: len(self.chat_lines) - MAX_CHAT_LINES]

    def add_message(self, author, text):
        for line in str(text).split("\n"):
            self.chat_lines.append(f"{author}: {line}")
        self._trim_chat()
        self._refresh_chat()
        if author == BOT_NAME and getattr(self, "voice_on", False):
            clean = clean_for_speech(text)
            if clean and not clean.startswith("__AQUA"):
                self.set_avatar_state("talking")
                _voice.speak(clean)

    def _push_history(self, role, content):
        self.history.append({"role": role, "content": content})
        if len(self.history) > MAX_HISTORY:
            del self.history[: len(self.history) - MAX_HISTORY]
        self.memory["chat_history"] = self.history[-MAX_HISTORY:]
        self.save_memory()

    def clear_chat(self, *_):
        self.chat_lines = []
        self.chat_label.text = ""
        self.history = []
        self.memory["chat_history"] = []
        self.save_memory()
        self.add_message(BOT_NAME, "Чат очищен.")

    def load_memory(self):
        data = None
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = None
        if not isinstance(data, dict):
            return copy.deepcopy(DEFAULT_MEMORY)
        for key, value in DEFAULT_MEMORY.items():
            data.setdefault(key, copy.deepcopy(value))
        for key, value in DEFAULT_MEMORY["settings"].items():
            data["settings"].setdefault(key, value)
        return data

    def save_memory(self):
        tmp = self.memory_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.memory_file)
        except OSError:
            pass

    def send_message(self, *_):
        text = self.user_input.text.strip()
        if not text:
            return
        self.user_input.text = ""
        self.add_message(USER_NAME, text)

        if self.waiting_question is not None:
            self.handle_learning_answer(text)
            return

        for hook in self.patches.message_hooks:
            try:
                result = hook(text)
            except Exception as e:
                print(f"[hook] {e}")
                continue
            if result:
                self.add_message(BOT_NAME, result)
                self._push_history("user", text)
                self._push_history("assistant", result)
                return

        if self.handle_command(text):
            return

        low = normalize(text)
        for name, fn in self.patches.commands.items():
            if low.startswith(name):
                try:
                    if fn(text):
                        return
                except Exception as e:
                    self.add_message(BOT_NAME, f"Патч: {e}")
                    return

        self._push_history("user", text)
        answer = find_answer_advanced(text, self.memory["answers"])
        if answer:
            self.add_message(BOT_NAME, answer)
            self._push_history("assistant", answer)
            return

        if self.llm_enabled():
            self._ask_llm_async()
            return

        self.waiting_question = text
        self.add_message(BOT_NAME, "Не знаю ответа. Напиши правильный — запомню. Или «отмена».")

    def handle_learning_answer(self, text):
        q = self.waiting_question
        self.waiting_question = None
        if normalize(text) in ("отмена", "стоп", "cancel"):
            self.add_message(BOT_NAME, "Отменено.")
            return
        self.memory["answers"][q] = text
        self.save_memory()
        self.add_message(BOT_NAME, "Запомнил.")

    def handle_command(self, text):
        low = normalize(text)

        if low in ("помощь", "/help", "help", "?"):
            self.show_help(None); return True
        if low in ("/clear", "очисти чат"):
            self.clear_chat(); return True
        if low in ("/reset", "очисти память"):
            self.confirm_reset_memory(); return True
        if low in ("настройки", "/settings"):
            self.show_settings(None); return True
        if low in ("/patches", "патчи"):
            self.show_patches(None); return True
        if low in ("/voice", "/голос"):
            self.add_message(BOT_NAME, "Движок: " + _voice.kind); return True
        if low in ("/voice_test", "тест голоса"):
            if _voice.available():
                self.add_message(BOT_NAME, "Говорю…")
                _voice.speak("Проверка. Раз, два, три.")
            return True
        if low in ("/seed", "залей базу"):
            added = 0
            answers = self.memory.setdefault("answers", {})
            for k, v in SEED_ANSWERS.items():
                if k not in answers:
                    answers[k] = v; added += 1
            for k, v in self.patches.responses.items():
                if k not in answers:
                    answers[k] = v; added += 1
            self.save_memory()
            self.add_message(BOT_NAME, f"Загружено: {added}. Всего: {len(answers)}.")
            return True

        m = re.fullmatch(r"запомни[,\s]+(.+?)\s*[:=]\s*(.+)", text, re.IGNORECASE)
        if m:
            self.memory["user_facts"][m.group(1).strip().lower()] = m.group(2).strip()
            self.save_memory()
            self.add_message(BOT_NAME, "Запомнил.")
            return True

        if low in ("что ты знаешь обо мне", "мои факты"):
            facts = self.memory.get("user_facts", {})
            if not facts:
                self.add_message(BOT_NAME, "Пока ничего.")
            else:
                self.add_message(BOT_NAME, "Знаю:\n" + "\n".join(f"• {k}: {v}" for k, v in facts.items()))
            return True

        if low in ("забудь обо мне", "очисти факты"):
            self.memory["user_facts"] = {}
            self.save_memory()
            self.add_message(BOT_NAME, "Забыл всё.")
            return True

        m = re.fullmatch(r"выучи\s+(.+?)\s*=\s*(.+)", text, re.IGNORECASE)
        if m:
            self.memory["answers"][m.group(1).strip()] = m.group(2).strip()
            self.save_memory()
            self.add_message(BOT_NAME, "Выучил.")
            return True

        if low.startswith("найди"):
            self.search_in_documents(text[len("найди"):].strip()); return True
        if low.startswith("посчитай"):
            self.calculate_expression(text[len("посчитай"):].strip()); return True
        if low in ("время", "сколько времени"):
            self.add_message(BOT_NAME, time.strftime("Сейчас %H:%M:%S, %d.%m.%Y")); return True
        if re.fullmatch(r"[0-9+\-*/%().,\s^]+", text) and any(c.isdigit() for c in text):
            self.calculate_expression(text); return True
        return False

    def calculate_expression(self, expr):
        if not expr:
            self.add_message(BOT_NAME, "Нужен пример."); return
        try:
            r = safe_calculate(expr)
            if isinstance(r, float):
                r = round(r, 10)
                if r.is_integer(): r = int(r)
            self.add_message(BOT_NAME, f"Ответ: {r}")
        except ZeroDivisionError:
            self.add_message(BOT_NAME, "На ноль нельзя.")
        except (ValueError, SyntaxError):
            self.add_message(BOT_NAME, "Не смог посчитать.")

    def confirm_reset_memory(self):
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        content.add_widget(Label(text="Удалить ответы и документы?"))
        row = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        yes = Button(text="Да"); no = Button(text="Отмена")
        row.add_widget(yes); row.add_widget(no)
        content.add_widget(row)
        popup = Popup(title="Подтверждение", content=content, size_hint=(0.8, 0.35))

        def do_reset(*_):
            self.memory["answers"] = copy.deepcopy(DEFAULT_ANSWERS)
            self.memory["documents"] = {}
            for q, a in self.patches.responses.items():
                self.memory["answers"][q] = a
            self.save_memory()
            popup.dismiss()
            self.add_message(BOT_NAME, "Память очищена.")

        yes.bind(on_press=do_reset)
        no.bind(on_press=popup.dismiss)
        popup.open()

    def open_file_popup(self, *_):
        start = "/storage/emulated/0"
        if not os.path.exists(start):
            start = os.path.expanduser("~")
        layout = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        chooser = FileChooserListView(path=start, filters=[f"*{e}" for e in ALLOWED_EXTENSIONS])
        row = BoxLayout(size_hint_y=None, height=dp(55), spacing=dp(8))
        read = Button(text="Прочитать"); cancel = Button(text="Отмена")
        row.add_widget(read); row.add_widget(cancel)
        layout.add_widget(chooser); layout.add_widget(row)
        popup = Popup(title="Файл", content=layout, size_hint=(0.95, 0.95))
        read.bind(on_press=lambda *_: self.read_file(chooser.selection, popup))
        cancel.bind(on_press=popup.dismiss)
        popup.open()

    def read_file(self, selected, popup):
        if not selected:
            self.add_message(BOT_NAME, "Выбери файл."); return
        path = selected[0]
        if os.path.splitext(path)[1].lower() not in ALLOWED_EXTENSIONS:
            self.add_message(BOT_NAME, "Такой тип нельзя."); return
        try:
            if os.path.getsize(path) > MAX_DOC_FILE_BYTES:
                self.add_message(BOT_NAME, "Слишком большой."); return
        except OSError:
            pass
        content = None
        for enc in ("utf-8", "cp1251"):
            try:
                with open(path, "r", encoding=enc) as f:
                    content = f.read(MAX_DOC_CHARS)
                break
            except UnicodeDecodeError:
                continue
            except OSError:
                self.add_message(BOT_NAME, "Не открыть файл."); return
        if content is None:
            self.add_message(BOT_NAME, "Не декодировать."); return
        name = os.path.basename(path)
        docs = self.memory["documents"]
        if len(docs) >= MAX_DOCUMENTS and name not in docs:
            oldest = min(docs, key=lambda k: docs[k]["loaded_at"])
            del docs[oldest]
        docs[name] = {"content": content, "loaded_at": time.time()}
        self.save_memory()
        self.add_message(BOT_NAME, f"«{name}» прочитан. Символов: {len(content)}.")
        popup.dismiss()

    def search_in_documents(self, query):
        if not query:
            self.add_message(BOT_NAME, "Что найти?"); return
        docs = self.memory["documents"]
        if not docs:
            self.add_message(BOT_NAME, "Сначала открой файл."); return
        q = normalize(query)
        found = []
        for name, info in docs.items():
            for n, line in enumerate(info["content"].splitlines(), 1):
                if q in normalize(line):
                    found.append(f"{name}:{n}: {line.strip()[:120]}")
                    if len(found) >= 5: break
            if len(found) >= 5: break
        if found:
            self.add_message(BOT_NAME, "Нашёл:\n" + "\n".join(found))
        else:
            self.add_message(BOT_NAME, "Ничего.")

    def llm_enabled(self):
        s = self.memory["settings"]
        return bool(s.get("llm_base_url") and s.get("llm_model"))

    def _ask_llm_async(self):
        token = f"__AQUA_{time.time_ns()}__"
        self.chat_lines.append(f"{BOT_NAME}: {token}")
        self._trim_chat()
        self._refresh_chat()
        self.set_avatar_state("thinking")

        def worker():
            try:
                answer = self._llm_request()
            except urllib.error.HTTPError as e:
                answer = f"[HTTP {e.code}] {e.reason}"
            except urllib.error.URLError as e:
                answer = f"[Сеть] {e.reason}"
            except Exception as e:
                answer = f"[Ошибка] {type(e).__name__}: {e}"
            Clock.schedule_once(lambda dt: self._finish_llm(token, answer), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_llm(self, token, answer):
        answer = (answer or "").strip() or "(пусто)"
        new_lines = [f"{BOT_NAME}: {ln}" for ln in answer.split("\n")]
        for i, line in enumerate(self.chat_lines):
            if token in line:
                self.chat_lines[i:i + 1] = new_lines
                break
        else:
            self.chat_lines.extend(new_lines)
        self._trim_chat()
        self._refresh_chat()
        self._push_history("assistant", answer)
        if getattr(self, "voice_on", False):
            clean = clean_for_speech(answer)
            if clean:
                self.set_avatar_state("talking")
                _voice.speak(clean)

    def _llm_request(self):
        s = self.memory["settings"]
        url = s["llm_base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if s.get("llm_api_key"):
            headers["Authorization"] = f"Bearer {s['llm_api_key']}"
        sys_prompt = s.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        facts = self.memory.get("user_facts", {})
        if facts:
            sys_prompt += "\n\nФакты о пользователе:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items())
        extra = self.patches.system_prompt_extra()
        if extra:
            sys_prompt += f"\n\nДополнительный контекст:\n{extra}"
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(self.history[-MAX_HISTORY:])
        data = json.dumps({"model": s["llm_model"], "messages": messages, "temperature": 0.7}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw)["choices"][0]["message"]["content"].strip()
        except Exception:
            raise RuntimeError(f"API: {raw[:200]}")

    def show_settings(self, *_):
        s = self.memory["settings"]
        layout = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6), size_hint_y=None)
        layout.bind(minimum_height=lambda i, h: setattr(i, "height", h))

        def field(label, value, password=False):
            layout.add_widget(Label(text=label, size_hint_y=None, height=dp(24), halign="left", valign="middle"))
            ti = TextInput(text=value, multiline=False, password=password, size_hint_y=None, height=dp(40))
            layout.add_widget(ti)
            return ti

        url_in = field("Base URL", s.get("llm_base_url", ""))
        key_in = field("API-ключ", s.get("llm_api_key", ""), password=True)
        model_in = field("Модель", s.get("llm_model", ""))
        layout.add_widget(Label(text="System prompt", size_hint_y=None, height=dp(24), halign="left", valign="middle"))
        sys_in = TextInput(text=s.get("system_prompt", DEFAULT_SYSTEM_PROMPT), multiline=True, size_hint_y=None, height=dp(80))
        layout.add_widget(sys_in)
        row = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        save = Button(text="Сохранить"); close = Button(text="Закрыть")
        row.add_widget(save); row.add_widget(close)
        layout.add_widget(row)
        scroll = ScrollView(); scroll.add_widget(layout)
        popup = Popup(title="Настройки", content=scroll, size_hint=(0.95, 0.9))

        def do_save(*_):
            s["llm_base_url"] = url_in.text.strip()
            s["llm_api_key"] = key_in.text.strip()
            s["llm_model"] = model_in.text.strip()
            s["system_prompt"] = sys_in.text.strip() or DEFAULT_SYSTEM_PROMPT
            self.save_memory()
            self.add_message(BOT_NAME, "Сохранено.")

        save.bind(on_press=do_save)
        close.bind(on_press=popup.dismiss)
        popup.open()

    def show_patches(self, *_):
        if not self.patches.loaded:
            text = "Патчи не найдены. Положи .py в /sdcard/Aqua/patches/"
        else:
            text = "Загружено:\n" + "\n".join(f"• {n} v{v}" for n, v, _ in self.patches.loaded)
        scrollable_popup("Патчи", text, (0.9, 0.7)).open()

    def show_help(self, *_):
        text = (
            f"Аква v{AQUA_VERSION}. Патчей: {len(self.patches.loaded)}.\n\n"
            "посчитай 5*(10-2)\n"
            "найди import\n"
            "время\n"
            "выучи X = Y\n"
            "запомни имя: Иван\n"
            "что ты знаешь обо мне\n"
            "/seed — база знаний\n"
            "/patches — патчи\n"
            "🔊 — голос"
        )
        scrollable_popup("Помощь", text, (0.95, 0.85)).open()

    def show_memory(self, *_):
        text = (
            f"Ответов: {len(self.memory['answers'])}\n"
            f"Файлов: {len(self.memory['documents'])}\n"
            f"Фактов: {len(self.memory.get('user_facts', {}))}\n"
            f"История: {len(self.history)}\n"
            f"LLM: {'вкл' if self.llm_enabled() else 'выкл'}\n"
            f"Голос: {'вкл' if getattr(self, 'voice_on', False) else 'выкл'}\n"
            f"Патчей: {len(self.patches.loaded)}"
        )
        scrollable_popup("Память", text, (0.9, 0.6)).open()


if __name__ == "__main__":
    SmartBotApp().run()
