import asyncio
import importlib.util
import os
import sys
import types
import unittest


class _TelegramObject:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class TelegramCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_modules = {name: sys.modules.get(name) for name in ("telegram", "telegram.ext")}
        telegram = types.ModuleType("telegram")
        telegram.Update = _TelegramObject
        telegram.InlineKeyboardButton = _TelegramObject
        telegram.InlineKeyboardMarkup = _TelegramObject
        extension = types.ModuleType("telegram.ext")
        extension.Application = _TelegramObject
        extension.CallbackQueryHandler = _TelegramObject
        extension.CommandHandler = _TelegramObject
        extension.MessageHandler = _TelegramObject
        extension.filters = _TelegramObject
        extension.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
        sys.modules["telegram"] = telegram
        sys.modules["telegram.ext"] = extension
        cls._old_token = os.environ.get("HERDR_TG_TOKEN")
        os.environ["HERDR_TG_TOKEN"] = "test-token"
        spec = importlib.util.spec_from_file_location(
            "herdr_telegram_callback_test_module", "relay/herdr_telegram.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        if cls._old_token is None:
            os.environ.pop("HERDR_TG_TOKEN", None)
        else:
            os.environ["HERDR_TG_TOKEN"] = cls._old_token
        for name, value in cls._old_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def setUp(self):
        self.module.callback_targets.clear()
        self.module.agents = []

    def test_callback_tokens_fit_telegram_limit_with_max_length_ids(self):
        keyboard = self.module.make_keyboard("h" * 64, "p" * 128, ["1. Yes", "2. No"])
        tokens = [row[0].kwargs["callback_data"] for row in keyboard.args[0]]
        self.assertTrue(tokens)
        self.assertTrue(all(len(token.encode("utf-8")) <= 64 for token in tokens))
        self.assertEqual({"host_id": "h" * 64, "pane_id": "p" * 128, "response": "1. Yes"},
                         self.module.decode_callback(tokens[0]))

    def test_legacy_hostless_callback_requires_unique_current_pane(self):
        self.module.agents = [
            {"host_id": "alpha", "pane_id": "same"},
            {"host_id": "beta", "pane_id": "same"},
        ]
        self.assertIsNone(self.module.decode_callback('{"pane_id":"same","response":"yes"}'))
        self.module.agents.pop()
        self.assertEqual(
            {"pane_id": "same", "response": "yes", "host_id": "alpha"},
            self.module.decode_callback('{"pane_id":"same","response":"yes"}'),
        )

    def test_invalid_callback_is_answered_once(self):
        class Query:
            data = "expired-token"

            def __init__(self):
                self.answers = []

            async def answer(self, *args):
                self.answers.append(args)

        class Update:
            def __init__(self, query):
                self.callback_query = query

        query = Query()
        asyncio.run(self.module.handle_callback(Update(query), None))
        self.assertEqual([("Agent is no longer available",)], query.answers)


if __name__ == "__main__":
    unittest.main()
