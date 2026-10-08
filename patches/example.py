"""Пример патча для «Аквы»."""

NAME = "Пример"
VERSION = "2.0"


def register(api):
    api.add_responses({
        "какая погода": "Нет доступа к интернету.",
        "что нового": "У меня — новый патч.",
    })

    def cmd_ping(text):
        api.app.add_message("Аква", "Понг!")
        return True
    api.add_command("пинг", cmd_ping)
    api.add_command("ping", cmd_ping)

    def hook(text):
        low = text.lower().strip()
        if low == "ку":
            return "Ку-ку!"
        return None
    api.on_message(hook)

    def ctx():
        return "Ты также знаешь: сегодня хороший день."
    api.on_llm_context(ctx)

    def on_start():
        api.log("патч готов")
    api.on_startup(on_start)
