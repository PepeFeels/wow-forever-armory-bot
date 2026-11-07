"""Offline tests for safe terminal logging configuration."""

from __future__ import annotations

import importlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wow_forever_armory import logging_config
from wow_forever_armory.client import ArmoryClient
from wow_forever_armory.discord_bot import ArmoryBot
from wow_forever_armory.guild_armory_store import GuildArmoryStore
from wow_forever_armory.logging_config import UTCLogFormatter, configure_logging

_HANDLER_MARKER = "_wow_forever_armory_console_handler"


class LoggingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.handlers = self.root.handlers[:]
        self.level = self.root.level
        self.loggers = {}
        for name in ("wow_forever_armory", "discord"):
            logger = logging.getLogger(name)
            self.loggers[name] = (
                logger.handlers[:],
                logger.level,
                logger.propagate,
            )

    def tearDown(self) -> None:
        self.root.handlers[:] = self.handlers
        self.root.setLevel(self.level)
        for name, state in self.loggers.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = state[0]
            logger.setLevel(state[1])
            logger.propagate = state[2]
        logging_config._CONFIGURED = False

    def test_default_and_configured_log_levels(self) -> None:
        stderr = io.StringIO()
        root_level = self.root.level
        root_handlers = self.root.handlers[:]
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            self.assertEqual(configure_logging(environ={}), ("INFO", "text"))
            self.assertEqual(
                logging.getLogger("wow_forever_armory").level, logging.INFO
            )
            self.assertEqual(logging.getLogger("discord").level, logging.INFO)
            self.assertEqual(
                configure_logging(environ={"ARMORY_LOG_LEVEL": "DEBUG"}),
                ("DEBUG", "text"),
            )
            self.assertEqual(
                logging.getLogger("wow_forever_armory").level, logging.DEBUG
            )
            self.assertEqual(logging.getLogger("discord").level, logging.DEBUG)
        self.assertEqual(self.root.level, root_level)
        self.assertEqual(self.root.handlers, root_handlers)
        self.assertIn("logging_configured", stderr.getvalue())

    def test_invalid_level_falls_back_to_info_with_clear_warning(self) -> None:
        stderr = io.StringIO()
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            self.assertEqual(
                configure_logging(environ={"ARMORY_LOG_LEVEL": "VERBOSE"}),
                ("INFO", "text"),
            )
        self.assertEqual(logging.getLogger("wow_forever_armory").level, logging.INFO)
        self.assertIn("invalid ARMORY_LOG_LEVEL; using INFO", stderr.getvalue())
        self.assertNotIn("VERBOSE", stderr.getvalue())

    def test_text_format_has_utc_timestamp_level_logger_and_event(self) -> None:
        record = logging.LogRecord(
            "wow_forever_armory.test",
            logging.WARNING,
            "test.py",
            12,
            "sample_event",
            (),
            None,
        )
        record.created = 0
        self.assertEqual(
            UTCLogFormatter().format(record),
            "1970-01-01T00:00:00Z WARNING wow_forever_armory.test sample_event",
        )

    def test_json_format_is_valid_and_contains_only_safe_metadata(self) -> None:
        stderr = io.StringIO()
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={"ARMORY_LOG_FORMAT": "json"})
            logger = logging.getLogger("wow_forever_armory.json_test")
            logger.info(
                "character_registered",
                extra={"character_count": 2, "user_id": 123, "status": 200},
            )
        payload = json.loads(stderr.getvalue().splitlines()[-1])
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["event"], "character_registered")
        self.assertEqual(payload["character_count"], 2)
        self.assertEqual(payload["status"], 200)
        self.assertNotIn("user_id", payload)
        self.assertTrue(payload["timestamp"].endswith("Z"))

    def test_repeated_configuration_emits_each_text_event_once(self) -> None:
        stderr = io.StringIO()
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={})
            configure_logging(environ={"ARMORY_LOG_LEVEL": "DEBUG"})
            logging.getLogger("wow_forever_armory.dedupe_test").info("single_event")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(sum("single_event" in line for line in lines), 1)
        self.assertEqual(sum("logging_configured" in line for line in lines), 1)
        for name in ("wow_forever_armory", "discord"):
            handlers = [
                handler
                for handler in logging.getLogger(name).handlers
                if getattr(handler, _HANDLER_MARKER, False)
            ]
            self.assertEqual(len(handlers), 1)

    def test_discord_py_logger_uses_its_own_handler_once(self) -> None:
        stderr = io.StringIO()
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={})
            logging.getLogger("wow_forever_armory.discord_split").warning(
                "armory_event"
            )
            logging.getLogger("discord.gateway").warning("discord_event")
        output = stderr.getvalue().splitlines()
        self.assertEqual(sum("armory_event" in line for line in output), 1)
        self.assertEqual(sum("discord_event" in line for line in output), 1)
        for name in ("wow_forever_armory", "discord"):
            self.assertEqual(
                sum(
                    getattr(handler, _HANDLER_MARKER, False)
                    for handler in logging.getLogger(name).handlers
                ),
                1,
            )

    def test_single_json_event_emits_exactly_one_json_object(self) -> None:
        stderr = io.StringIO()
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={"ARMORY_LOG_FORMAT": "json"})
            logging.getLogger("wow_forever_armory.json_dedupe").warning("one_event")
        objects = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(sum(item["event"] == "one_event" for item in objects), 1)
        self.assertEqual(len(objects), 2)  # one config event and one test event

    def test_external_root_and_namespace_handlers_are_not_changed(self) -> None:
        stderr = io.StringIO()
        root_handler = logging.StreamHandler(io.StringIO())
        namespace_handler = logging.StreamHandler(io.StringIO())
        namespace_handler.setLevel(logging.ERROR)
        external_formatter = logging.Formatter("%(message)s external format")
        namespace_handler.setFormatter(external_formatter)
        external_filter = logging.Filter("external")
        namespace_handler.addFilter(external_filter)
        self.root.addHandler(root_handler)
        application_logger = logging.getLogger("wow_forever_armory")
        application_logger.addHandler(namespace_handler)
        root_level = self.root.level
        root_handlers = self.root.handlers[:]
        external_level = namespace_handler.level
        external_filters = namespace_handler.filters[:]

        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={})

        self.assertEqual(self.root.level, root_level)
        self.assertEqual(self.root.handlers, root_handlers)
        self.assertIs(namespace_handler.formatter, external_formatter)
        self.assertEqual(namespace_handler.level, external_level)
        self.assertEqual(namespace_handler.filters, external_filters)
        owned = [
            handler
            for handler in application_logger.handlers
            if getattr(handler, _HANDLER_MARKER, False)
        ]
        self.assertEqual(len(owned), 1)

    def test_external_stderr_error_handler_does_not_hide_armory_info(self) -> None:
        stderr = io.StringIO()
        logger = logging.getLogger("wow_forever_armory")
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            external = logging.StreamHandler()
            external.setLevel(logging.ERROR)
            external.setFormatter(logging.Formatter("EXTERNAL: %(message)s"))
            logger.addHandler(external)
            old_formatter = external.formatter
            old_filters = external.filters[:]
            configure_logging(environ={})
            logger.info("info_probe")
            logger.error("error_probe")
        output = stderr.getvalue()
        self.assertEqual(sum("info_probe" in line for line in output.splitlines()), 1)
        self.assertEqual(sum("error_probe" in line for line in output.splitlines()), 2)
        self.assertNotIn("EXTERNAL: info_probe", output)
        self.assertIn("EXTERNAL: error_probe", output)
        self.assertEqual(external.level, logging.ERROR)
        self.assertIs(external.formatter, old_formatter)
        self.assertEqual(external.filters, old_filters)
        self.assertEqual(
            sum(
                getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers
            ),
            1,
        )

    def test_external_stdout_info_handler_gets_independent_copy(self) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        logger = logging.getLogger("wow_forever_armory")
        with (
            patch("wow_forever_armory.logging_config.sys.stderr", stderr),
            patch("wow_forever_armory.logging_config.sys.stdout", stdout),
        ):
            external = logging.StreamHandler(stdout)
            external.setLevel(logging.INFO)
            external.setFormatter(logging.Formatter("EXTERNAL: %(message)s"))
            logger.addHandler(external)
            old_formatter = external.formatter
            configure_logging(environ={})
            logger.info("stdout_probe")
        self.assertEqual(stderr.getvalue().count("stdout_probe"), 1)
        self.assertEqual(stdout.getvalue().count("EXTERNAL: stdout_probe"), 1)
        self.assertEqual(external.level, logging.INFO)
        self.assertIs(external.formatter, old_formatter)

    def test_external_console_copy_does_not_inherit_armory_redaction(self) -> None:
        stderr = io.StringIO()
        logger = logging.getLogger("wow_forever_armory")
        url = "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/test"
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            external = logging.StreamHandler(stderr)
            external.setFormatter(logging.Formatter("EXTERNAL: %(message)s"))
            logger.addHandler(external)
            configure_logging(environ={})
            logger.error("diagnostic %s", url)
        owned_record = next(
            line
            for line in stderr.getvalue().splitlines()
            if "diagnostic" in line and not line.startswith("EXTERNAL:")
        )
        self.assertIn("[REDACTED_URL]", owned_record)
        self.assertIn(f"EXTERNAL: diagnostic {url}", stderr.getvalue())

    def test_unexpected_exception_traceback_redacts_secret_and_urls(self) -> None:
        stderr = io.StringIO()
        token = (
            "MTAxMjM0NTY3ODkwMTIzNDU2.signaturesegment.abcdefghijklmnopqrstuvwxyz123456"
        )
        private_url = "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/secret"
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(environ={"ARMORY_LOG_FORMAT": "json"}, secrets=(token,))
            logger = logging.getLogger("wow_forever_armory.error_test")
            try:
                raise RuntimeError(f"Authorization: Bot {token}\n{private_url}")
            except RuntimeError:
                logger.exception("unexpected_operation_failure")
        output = stderr.getvalue()
        self.assertNotIn(token, output)
        self.assertNotIn(private_url, output)
        event = json.loads(output.splitlines()[-1])
        self.assertIn("Traceback (most recent call last)", event["exception"])
        self.assertIn("[REDACTED]", event["exception"])
        self.assertIn("[REDACTED_URL]", event["exception"])

    def test_redacts_headers_urls_paths_tokens_and_rejects_extra_metadata(self) -> None:
        stderr = io.StringIO()
        token = (
            "MTAxMjM0NTY3ODkwMTIzNDU2.signaturesegment.abcdefghijklmnopqrstuvwxyz123456"
        )
        private_secret = "fixture-private-secret-value"
        armory_url = "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/Éowyn"
        bare_path = "en-us/forever/eu/armory/character/private-name"
        with patch("wow_forever_armory.logging_config.sys.stderr", stderr):
            configure_logging(
                environ={"ARMORY_LOG_FORMAT": "json"}, secrets=(private_secret,)
            )
            logger = logging.getLogger("wow_forever_armory.redaction_test")
            logger.warning("discord_token=%s", token)
            for header in (
                "Authorization: Bot alpha-secret",
                "authorization: bearer beta-secret",
                "AUTHORIZATION=Bearer gamma-secret",
                "authorization : Basic delta-secret",
            ):
                logger.warning("header %s", header)
            logger.warning("submitted %s and %s", armory_url, bare_path)
            logger.warning("private field %s", private_secret)
            logger.info(
                "safe_event",
                extra={
                    "token": token,
                    "authorization": "Bearer not-allowed",
                    "armory_url": armory_url,
                    "character_path": bare_path,
                    "request": {"private": True},
                },
            )
        output = stderr.getvalue()
        for sensitive in (
            token,
            private_secret,
            armory_url,
            bare_path,
            "alpha-secret",
            "beta-secret",
            "gamma-secret",
            "delta-secret",
        ):
            self.assertNotIn(sensitive, output)
        records = [json.loads(line) for line in output.splitlines()]
        safe_event = next(
            record for record in records if record["event"] == "safe_event"
        )
        self.assertEqual(set(safe_event), {"timestamp", "level", "logger", "event"})

    def test_import_does_not_configure_global_logging(self) -> None:
        handlers = self.root.handlers[:]
        level = self.root.level
        logger_states = {
            name: (
                logging.getLogger(name).handlers[:],
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
            )
            for name in ("wow_forever_armory", "discord")
        }
        importlib.reload(logging_config)
        self.assertEqual(self.root.handlers, handlers)
        self.assertEqual(self.root.level, level)
        for name, state in logger_states.items():
            logger = logging.getLogger(name)
            self.assertEqual(logger.handlers, state[0])
            self.assertEqual(logger.level, state[1])
            self.assertEqual(logger.propagate, state[2])


class BotConstructionLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_construction_does_not_configure_global_logging(self) -> None:
        root = logging.getLogger()
        handlers = root.handlers[:]
        level = root.level
        logger_states = {
            name: (
                logging.getLogger(name).handlers[:],
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
            )
            for name in ("wow_forever_armory", "discord")
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = ArmoryBot(
                ArmoryClient(),
                GuildArmoryStore(Path(temp_dir) / "offline.sqlite3"),
                123,
            )
            await bot.close()
        self.assertEqual(root.handlers, handlers)
        self.assertEqual(root.level, level)
        for name, state in logger_states.items():
            logger = logging.getLogger(name)
            self.assertEqual(logger.handlers, state[0])
            self.assertEqual(logger.level, state[1])
            self.assertEqual(logger.propagate, state[2])
