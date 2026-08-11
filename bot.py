"""Telegram bot that welcomes new members using Markov-chain text."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from markov import MarkovChain


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    greetings_path: Path
    admins_path: Path = BASE_DIR / "admins.json"
    initial_admin_ids: frozenset[int] = frozenset()
    markov_order: int = 2
    max_words: int = 28

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN не задан. Скопируйте .env.example в .env "
                "и добавьте токен от @BotFather."
            )

        def project_path(variable: str, default: str) -> Path:
            value = Path(os.getenv(variable, default))
            return value if value.is_absolute() else BASE_DIR / value

        admin_ids = parse_id_list(os.getenv("ADMIN_USER_IDS", ""))

        return cls(
            token=token,
            greetings_path=project_path("GREETINGS_FILE", "greetings.txt"),
            admins_path=project_path("ADMINS_FILE", "admins.json"),
            initial_admin_ids=frozenset(admin_ids),
            markov_order=int(os.getenv("MARKOV_ORDER", "2")),
            max_words=int(os.getenv("MAX_GREETING_WORDS", "28")),
        )


def parse_id_list(value: str) -> set[int]:
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ValueError("ADMIN_USER_IDS должен содержать ID через запятую") from error


class AdminRegistry:
    """Persistent allow-list for people who may use the bot in private."""

    def __init__(self, path: Path, initial_ids: frozenset[int] = frozenset()) -> None:
        self.path = path
        self._ids = set(initial_ids)
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                self._ids.update(int(user_id) for user_id in stored)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Не удалось прочитать список админов {path}") from error

    def contains(self, user_id: int) -> bool:
        return user_id in self._ids

    def add(self, user_id: int) -> bool:
        if user_id in self._ids:
            return False
        self._ids.add(user_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(sorted(self._ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return True


def build_welcome_text(chain: MarkovChain, mention: str, max_words: int) -> str:
    generated = html.escape(chain.generate(max_words=max_words))
    return (
        f"{generated}\n\n"
        f"Рады видеть тебя, {mention}! 👋\n\n"
        "Пожалуйста, ознакомься с правилами в описании группы, а также с гайдом."
    )


def is_connection_error(error: BaseException | None) -> bool:
    """Return True if the exception chain contains network or connection errors."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (NetworkError, OSError)):
            return True
        name = type(current).__name__
        if "Connect" in name or "Timeout" in name or "Network" in name or "Protocol" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


def is_connect_timeout(error: BaseException) -> bool:
    """Return True when the exception chain contains connection error or timeout."""
    return is_connection_error(error)


async def reply_with_connect_retry(message: object, text: str, **kwargs: object) -> None:
    """Retry failures that happened due to temporary connection or network issues."""
    retry_delays = (1.0, 2.0)
    for attempt in range(len(retry_delays) + 1):
        try:
            await message.reply_text(text, **kwargs)  # type: ignore[attr-defined]
            return
        except NetworkError as error:
            if not is_connection_error(error) or attempt == len(retry_delays):
                raise
            delay = retry_delays[attempt]
            LOGGER.warning(
                "Telegram недоступен при подключении; повтор отправки через %.0f с",
                delay,
            )
            await asyncio.sleep(delay)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Личные сообщения доступны администраторам группы. Выполните "
            "/allowpm в группе, где вы администратор."
        )
        return
    await reply_with_connect_retry(
        message,
        "Я подключён и приветствую новых участников фразами, созданными "
        "марковской цепью. Команда проверки: /preview"
    )


async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an example without waiting for a member to join."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    if message.chat.type == ChatType.PRIVATE and not registry.contains(user.id):
        await reply_with_connect_retry(
            message,
            "Доступ закрыт. Если вы администратор, выполните /allowpm в группе."
        )
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    text = build_welcome_text(chain, user.mention_html(), settings.max_words)
    await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)


async def allow_private_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Authorize a real group administrator for private chat with the bot."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await reply_with_connect_retry(message, "Эту команду нужно выполнить в группе.")
        return

    try:
        membership = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        await reply_with_connect_retry(
            message,
            "Не удалось проверить статус. Назначьте бота администратором группы "
            "и повторите /allowpm."
        )
        return
    if membership.status not in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        await reply_with_connect_retry(message, "Команда доступна только администраторам группы.")
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    added = registry.add(user.id)
    status = "Доступ к личным сообщениям открыт." if added else "Доступ уже был открыт."
    await reply_with_connect_retry(message, f"{status} Теперь напишите мне в личный чат.")


async def show_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message and user and chat:
        await reply_with_connect_retry(message, f"Ваш ID: {user.id}\nID этого чата: {chat.id}")


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.new_chat_members:
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    bot_id = context.bot.id

    for member in message.new_chat_members:
        if member.id == bot_id:
            await reply_with_connect_retry(
                message,
                "✅ Бот подключён. Я буду приветствовать новых участников. "
                "Администратор может выполнить /allowpm, чтобы открыть личные "
                "команды /start и /preview."
            )
            continue
        text = build_welcome_text(chain, member.mention_html(), settings.max_words)
        await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error and is_connection_error(context.error):
        LOGGER.warning("Сетевая ошибка при взаимодействии с Telegram: %s", context.error)
    else:
        LOGGER.error("Ошибка при обработке события Telegram", exc_info=context.error)


def create_application(settings: Settings) -> Application:
    greeting_chain = MarkovChain.from_file(
        settings.greetings_path,
        order=settings.markov_order,
    )
    admins = AdminRegistry(settings.admins_path, settings.initial_admin_ids)
    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=5,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=20,
        read_timeout=30,
        write_timeout=20,
        pool_timeout=5,
    )
    application = (
        Application.builder()
        .token(settings.token)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )
    application.bot_data.update(
        {
            "greeting_chain": greeting_chain,
            "admins": admins,
            "settings": settings,
        }
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("allowpm", allow_private_messages))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members)
    )
    application.add_error_handler(log_error)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx logs full Telegram request URLs, which include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_environment()
    LOGGER.info("Корпус приветствий: %s", settings.greetings_path)
    create_application(settings).run_polling()


if __name__ == "__main__":
    main()
