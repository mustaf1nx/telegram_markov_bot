import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from telegram.error import NetworkError, TelegramError, TimedOut
from bot import (
    AdminRegistry,
    OPRegistry,
    build_welcome_text,
    format_admin_tag,
    handle_op_message,
    is_connect_timeout,
    is_connection_error,
    log_error,
    parse_id_list,
    reply_with_connect_retry,
    swap_keyboard_layout,
)


class AdminRegistryTests(unittest.TestCase):
    def test_parse_id_list(self) -> None:
        self.assertEqual(parse_id_list("123, 456,123"), {123, 456})

    def test_admin_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admins.json"
            registry = AdminRegistry(path)
            self.assertTrue(registry.add(123))
            self.assertFalse(registry.add(123))
            self.assertTrue(AdminRegistry(path).contains(123))


class OPRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_default_ops_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            ops = registry.get_all()
            self.assertEqual(len(ops), 17)
            self.assertIn("SE", ops)
            self.assertIn("IT", ops)
            self.assertIn("BDA", ops)
            self.assertIn("CS", ops)
            self.assertEqual(ops["SE"].school, "School of Software Engineering")

    def test_find_matching_ops_short_code_and_full_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # Match code
            matches_se = registry.find_matching_ops("Привет, я поступаю на SE!")
            self.assertEqual([op.code for op in matches_se], ["SE"])

            # Match full name
            matches_bda = registry.find_matching_ops("Что насчет Big Data Analysis?")
            self.assertEqual([op.code for op in matches_bda], ["BDA"])

            # Multiple matches
            matches_multi = registry.find_matching_ops("Выбираю между SE и Cybersecurity")
            matched_codes = {op.code for op in matches_multi}
            self.assertEqual(matched_codes, {"SE", "CS"})

    def test_find_matching_ops_avoids_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # 'reset' should not match 'SE', 'with' should not match 'IT'
            matches = registry.find_matching_ops("Please reset my password with new suite")
            self.assertEqual(len(matches), 0)

    def test_set_admin_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            self.assertTrue(registry.set_admin("SE", "@new_se_admin"))
            self.assertEqual(registry.get("SE").admin, "@new_se_admin")

            # Reload from disk
            reloaded = OPRegistry(path)
            self.assertEqual(reloaded.get("SE").admin, "@new_se_admin")

    def test_format_admin_tag(self) -> None:
        self.assertEqual(format_admin_tag("@alex"), "@alex")
        self.assertEqual(format_admin_tag("alex"), "@alex")
        self.assertEqual(
            format_admin_tag("950705809"),
            '<a href="tg://user?id=950705809">Администратор</a>',
        )

    def test_find_matching_ops_aliases_and_cyrillic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            # Match Cyrillic slang
            matches_seshnik = registry.find_matching_ops("Я сешник")
            self.assertEqual([op.code for op in matches_seshnik], ["SE"])

            matches_aitishnik = registry.find_matching_ops("Привет я айтишник")
            self.assertEqual([op.code for op in matches_aitishnik], ["IT"])

            matches_kiberbez = registry.find_matching_ops("выбрал кибербез")
            self.assertEqual([op.code for op in matches_kiberbez], ["CS"])

            matches_bdashnik = registry.find_matching_ops("бдашник тут")
            self.assertEqual([op.code for op in matches_bdashnik], ["BDA"])

    async def test_handle_op_message_triggers_only_for_target_member(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)

            chat_id = 100
            welcome_msg_id = 555
            new_user_id = 123
            old_user_id = 456

            tracker.add_welcome_message(chat_id, welcome_msg_id, new_user_id)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            # Old user (amer) replies to welcome message meant for cHezHrr -> IGNORED
            update_old_user = MagicMock()
            update_old_user.effective_message.chat.id = chat_id
            update_old_user.effective_message.from_user.id = old_user_id
            update_old_user.effective_message.from_user.is_bot = False
            update_old_user.effective_message.reply_to_message.message_id = welcome_msg_id
            update_old_user.effective_message.text = "Software engineering"
            update_old_user.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_old_user, context)
            update_old_user.effective_message.reply_text.assert_not_called()

            # Target new user (cHezHrr) replies to welcome message -> MATCHED
            update_new_user = MagicMock()
            update_new_user.effective_message.chat.id = chat_id
            update_new_user.effective_message.from_user.id = new_user_id
            update_new_user.effective_message.from_user.is_bot = False
            update_new_user.effective_message.from_user.mention_html.return_value = "@cHezHrr"
            update_new_user.effective_message.reply_to_message.message_id = welcome_msg_id
            update_new_user.effective_message.text = "Electronic Engineering"
            update_new_user.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_new_user, context)
            update_new_user.effective_message.reply_text.assert_called_once()
            called_text = update_new_user.effective_message.reply_text.call_args[0][0]
            self.assertIn("EE", called_text)
            self.assertIn("@dhshrbrhr", called_text)

    async def test_question_patterns_are_ignored(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=5)
            tracker.add_user(100, 123)

            context = MagicMock()
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            for question_text in ["Привет кто с се?", "Есть кто с ИТ?", "Кто тут на BDA?"]:
                update_q = MagicMock()
                update_q.effective_message.chat.id = 100
                update_q.effective_message.from_user.id = 123
                update_q.effective_message.from_user.is_bot = False
                update_q.effective_message.reply_to_message = None
                update_q.effective_message.text = question_text
                update_q.effective_message.reply_text = AsyncMock()

                await handle_op_message(update_q, context)
                update_q.effective_message.reply_text.assert_not_called()

    def test_swap_keyboard_layout(self) -> None:
        self.assertEqual(swap_keyboard_layout("Ct"), "Се")
        self.assertEqual(swap_keyboard_layout("CT"), "СЕ")
        self.assertEqual(swap_keyboard_layout("ct"), "се")
        self.assertEqual(swap_keyboard_layout("ыу"), "se")
        self.assertEqual(swap_keyboard_layout("vrc"), "мкс")

    def test_find_matching_ops_keyboard_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)

            matches_ct = registry.find_matching_ops("Ct")
            self.assertEqual([op.code for op in matches_ct], ["SE"])

            matches_ct_upper = registry.find_matching_ops("CT")
            self.assertEqual([op.code for op in matches_ct_upper], ["SE"])

            matches_se_layout = registry.find_matching_ops("ыу")
            self.assertEqual([op.code for op in matches_se_layout], ["SE"])

            matches_mcs_layout = registry.find_matching_ops("vrc")
            self.assertEqual([op.code for op in matches_mcs_layout], ["MCS"])

    async def test_handle_op_message_unrecognized_reply_to_bot(self) -> None:
        from bot import WelcomeTracker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "op_admins.json"
            registry = OPRegistry(path)
            tracker = WelcomeTracker(max_messages=10)

            bot_id = 999
            context = MagicMock()
            context.bot.id = bot_id
            context.application.bot_data = {
                "op_registry": registry,
                "welcome_tracker": tracker,
            }

            update_reply = MagicMock()
            update_reply.effective_message.chat.id = 100
            update_reply.effective_message.from_user.id = 777
            update_reply.effective_message.from_user.is_bot = False
            update_reply.effective_message.from_user.mention_html.return_value = "@student"
            update_reply.effective_message.reply_to_message.message_id = 500
            update_reply.effective_message.reply_to_message.from_user.id = bot_id
            update_reply.effective_message.text = "не знаю какая у меня оп"
            update_reply.effective_message.reply_text = AsyncMock()

            await handle_op_message(update_reply, context)
            update_reply.effective_message.reply_text.assert_called_once()
            called_text = update_reply.effective_message.reply_text.call_args[0][0]
            self.assertIn("Не удалось распознать ОП", called_text)
            self.assertIn("/ops", called_text)

    def test_build_welcome_text_contains_reply_instruction(self) -> None:
        mock_chain = MagicMock()
        mock_chain.generate.return_value = "Приветственный текст"
        welcome_text = build_welcome_text(mock_chain, "@user", 28)
        self.assertIn("Ответь на это сообщение (Reply)", welcome_text)




class NetworkErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    def test_is_connection_error_detection(self) -> None:
        class DummyConnectError(Exception):
            pass

        class DummyRemoteProtocolError(Exception):
            pass

        connect_err = NetworkError("Connect failed")
        connect_err.__cause__ = DummyConnectError("No address associated with hostname")
        self.assertTrue(is_connection_error(connect_err))
        self.assertTrue(is_connect_timeout(connect_err))

        remote_err = NetworkError("Server disconnected")
        remote_err.__cause__ = DummyRemoteProtocolError("Server disconnected without sending a response")
        self.assertTrue(is_connection_error(remote_err))

        timeout_err = TimedOut("Timed out")
        self.assertTrue(is_connection_error(timeout_err))

        val_err = ValueError("Invalid parameter")
        self.assertFalse(is_connection_error(val_err))

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_reply_with_connect_retry_recovers(self, mock_sleep: AsyncMock) -> None:
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=[
                NetworkError("httpx.ConnectError: [Errno -5] No address associated with hostname"),
                None,
            ]
        )
        await reply_with_connect_retry(mock_message, "Hello")
        self.assertEqual(mock_message.reply_text.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_reply_with_connect_retry_fails_after_max_attempts(
        self, mock_sleep: AsyncMock
    ) -> None:
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=NetworkError("httpx.RemoteProtocolError: Server disconnected")
        )
        with self.assertRaises(NetworkError):
            await reply_with_connect_retry(mock_message, "Hello")
        self.assertEqual(mock_message.reply_text.call_count, 3)

    @patch("bot.LOGGER")
    async def test_log_error_formatting(self, mock_logger: MagicMock) -> None:
        context = MagicMock()
        context.error = NetworkError("Server disconnected")
        await log_error(None, context)
        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()

        mock_logger.reset_mock()
        context.error = ValueError("Something broke")
        await log_error(None, context)
        mock_logger.error.assert_called_once()
        mock_logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()

