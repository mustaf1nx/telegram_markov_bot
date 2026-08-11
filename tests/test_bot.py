import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from telegram.error import NetworkError, TelegramError, TimedOut
from bot import (
    AdminRegistry,
    is_connect_timeout,
    is_connection_error,
    log_error,
    parse_id_list,
    reply_with_connect_retry,
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

