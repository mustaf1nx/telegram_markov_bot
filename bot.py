"""Telegram bot that welcomes new members using Markov-chain text."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
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
    op_admins_path: Path = BASE_DIR / "op_admins.json"
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
            op_admins_path=project_path("OP_ADMINS_FILE", "op_admins.json"),
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


EN_LAYOUT = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
RU_LAYOUT = "ёйцукенгшщзхъфывапролджэячсмитьбю.ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"

TRANS_EN_TO_RU = str.maketrans(EN_LAYOUT, RU_LAYOUT)
TRANS_RU_TO_EN = str.maketrans(RU_LAYOUT, EN_LAYOUT)


def swap_keyboard_layout(text: str) -> str:
    """Convert text between English QWERTY and Russian JCUKEN layouts."""
    res = []
    for char in text:
        if char in EN_LAYOUT:
            res.append(char.translate(TRANS_EN_TO_RU))
        elif char in RU_LAYOUT:
            res.append(char.translate(TRANS_RU_TO_EN))
        else:
            res.append(char)
    return "".join(res)


DEFAULT_OPS: dict[str, dict[str, Any]] = {
    "SE": {
        "name": "Software Engineering",
        "school": "School of Software Engineering",
        "admin": "@Alsh444",
        "aliases": ["SE", "СЕ", "Software Engineering", "сешник", "сешники", "сешница", "софтвер", "софтверщик", "софтваре", "софтвер инжиниринг"],
    },
    "IT": {
        "name": "Computer Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@TypicallyRain",
        "aliases": ["IT", "ИТ", "Computer Science", "айти", "айтишник", "айтишники", "компьютер сайнс", "комп сайнс", "кс"],
    },
    "BDA": {
        "name": "Big Data Analysis",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@therealarujxx",
        "aliases": ["BDA", "БДА", "Big Data Analysis", "Big Data", "бдашник", "бдашники", "бигдата", "биг дата", "биг дата анализис"],
    },
    "MCS": {
        "name": "Mathematical and Computational Science",
        "school": "School of Artificial Intelligence and Data Science",
        "admin": "@howflowersbloom",
        "aliases": ["MCS", "МКС", "Mathematical and Computational Science", "мксник", "мксники", "маткомп", "математикал"],
    },
    "CS": {
        "name": "Cybersecurity",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": ["CS", "КС", "Cybersecurity", "кибербез", "кибербезопасность", "сайберсекьюрити", "сайбер", "кибер"],
    },
    "SST": {
        "name": "Smart Security Technologies",
        "school": "School of Cybersecurity",
        "admin": "@alishaisyapping",
        "aliases": ["SST", "ССТ", "Smart Security Technologies", "ссшник", "смарт секьюрити", "смарт секьюрити технолоджис"],
    },
    "IIOT": {
        "name": "Industrial Internet of Things",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["IIOT", "ИИОТ", "Industrial Internet of Things", "ииотник", "индастриал иот", "иот"],
    },
    "EE": {
        "name": "Electronic Engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["EE", "ЕЕ", "ЭЭ", "Electronic Engineering", "электронщик", "электроник инжиниринг", "электроника"],
    },
    "ST": {
        "name": "Smart Technologies",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["ST", "СТ", "Smart Technologies", "стшник", "смарт тех", "смарт технолоджис"],
    },
    "DNE": {
        "name": "Digital technologies in nuclear power engineering",
        "school": "School of Intelligent Systems",
        "admin": "@dhshrbrhr",
        "aliases": ["DNE", "ДНЕ", "Digital technologies in nuclear power engineering", "днешник", "нуклеар", "ядерка", "ядерная инженерия"],
    },
    "ITM": {
        "name": "IT Management",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["ITM", "ИТМ", "IT Management", "итмщик", "айти менеджмент", "ит менеджмент"],
    },
    "ITE": {
        "name": "IT Entrepreneurship",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["ITE", "ИТЕ", "IT Entrepreneurship", "итешник", "айти предпринимательство", "ит предпринимательство", "айти энтрепренершип"],
    },
    "AIB": {
        "name": "AI Business",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["AIB", "АИБ", "AI Business", "аибник", "аи бизнес", "ай бизнес", "ии бизнес"],
    },
    "MT": {
        "name": "Media Technologies",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["MT", "МТ", "Media Technologies", "мтшник", "медиа тех", "медиа технологии", "медиатехнологии"],
    },
    "DJ": {
        "name": "Digital Journalism",
        "school": "School of Creative Industries",
        "admin": "@assiixq",
        "aliases": ["DJ", "ДЖ", "Digital Journalism", "джник", "диджитал журналистика", "цифровая журналистика", "диджей"],
    },
    "DPA": {
        "name": "Digital Public Administration",
        "school": "School of Digital Public Administration",
        "admin": "@assiixq",
        "aliases": ["DPA", "ДПА", "Digital Public Administration", "дпашник", "диджитал паблик", "госуправление"],
    },
    "DL": {
        "name": "Digital Jurisprudence",
        "school": "School of Digital Public Administration",
        "admin": "@finrandiri",
        "aliases": ["DL", "ДЛ", "Digital Jurisprudence", "длшник", "диджитал юриспруденция", "цифровая юриспруденция", "цифровые юристы"],
    },
}


@dataclass(frozen=True)
class OPProgram:
    code: str
    name: str
    school: str
    admin: str
    aliases: tuple[str, ...] = ()


class WelcomeTracker:
    """Tracks welcome messages and recently joined members so OP reactions only trigger for the actual new member."""

    def __init__(self, max_messages: int = 10) -> None:
        self.max_messages = max_messages
        self._active_new_members: dict[tuple[int, int], int] = {}
        self._welcome_messages: dict[tuple[int, int], int] = {}

    def add_welcome_message(
        self, chat_id: int, welcome_message_id: int, target_user_id: int
    ) -> None:
        self._welcome_messages[(chat_id, welcome_message_id)] = target_user_id
        self._active_new_members[(chat_id, target_user_id)] = self.max_messages

    def add_user(self, chat_id: int, user_id: int) -> None:
        self._active_new_members[(chat_id, user_id)] = self.max_messages

    def is_target_member(
        self,
        chat_id: int,
        user_id: int,
        reply_to_message_id: int | None = None,
        is_reply_to_bot: bool = False,
    ) -> bool:
        if reply_to_message_id is not None:
            welcome_target = self._welcome_messages.get(
                (chat_id, reply_to_message_id)
            )
            if welcome_target is not None:
                return user_id == welcome_target
            if is_reply_to_bot:
                return True

        return (
            (chat_id, user_id) in self._active_new_members
            and self._active_new_members[(chat_id, user_id)] > 0
        )

    def record_message(self, chat_id: int, user_id: int) -> None:
        key = (chat_id, user_id)
        if key in self._active_new_members:
            self._active_new_members[key] -= 1
            if self._active_new_members[key] <= 0:
                del self._active_new_members[key]

    def remove_user(self, chat_id: int, user_id: int) -> None:
        self._active_new_members.pop((chat_id, user_id), None)


class OPRegistry:
    """Registry for Educational Programs (OPs) and their assigned administrators."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ops: dict[str, OPProgram] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for code, info in data.items():
                    code_upper = code.upper()
                    raw_aliases = info.get("aliases", [])
                    self._ops[code_upper] = OPProgram(
                        code=code_upper,
                        name=str(info.get("name", code_upper)),
                        school=str(info.get("school", "")),
                        admin=str(info.get("admin", "@admin")),
                        aliases=tuple(str(a) for a in raw_aliases),
                    )
                return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                LOGGER.warning("Не удалось прочитать %s: %s. Используем значения по умолчанию.", self.path, error)

        for code, info in DEFAULT_OPS.items():
            self._ops[code] = OPProgram(
                code=code,
                name=info["name"],
                school=info["school"],
                admin=info["admin"],
                aliases=tuple(info.get("aliases", [])),
            )
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            code: {
                "name": prog.name,
                "school": prog.school,
                "admin": prog.admin,
                "aliases": list(prog.aliases),
            }
            for code, prog in self._ops.items()
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get_all(self) -> dict[str, OPProgram]:
        return dict(self._ops)

    def get(self, code: str) -> OPProgram | None:
        return self._ops.get(code.upper())

    def set_admin(self, code: str, admin: str) -> bool:
        code_upper = code.upper()
        if code_upper not in self._ops:
            return False
        current = self._ops[code_upper]
        self._ops[code_upper] = OPProgram(
            code=current.code,
            name=current.name,
            school=current.school,
            admin=admin,
            aliases=current.aliases,
        )
        self.save()
        return True

    def find_matching_ops(self, text: str) -> list[OPProgram]:
        if not text:
            return []

        candidates = {text}
        swapped = swap_keyboard_layout(text)
        if swapped != text:
            candidates.add(swapped)

        matched: list[OPProgram] = []
        for code, prog in self._ops.items():
            terms = [code, prog.name] + list(prog.aliases)
            found = False
            for term in terms:
                if not term:
                    continue
                pattern = rf"(?i)(?<![a-zA-Z0-9_а-яА-ЯёЁ]){re.escape(term)}(?![a-zA-Z0-9_а-яА-ЯёЁ])"
                if any(re.search(pattern, cand) for cand in candidates):
                    found = True
                    break
            if found:
                matched.append(prog)

        return matched



def format_admin_tag(admin: str) -> str:
    admin_str = admin.strip()
    if not admin_str:
        return "@admin"
    if admin_str.isdigit():
        return f'<a href="tg://user?id={admin_str}">Администратор</a>'
    if not admin_str.startswith("@"):
        admin_str = f"@{admin_str}"
    return html.escape(admin_str)



def build_welcome_text(chain: MarkovChain, mention: str, max_words: int) -> str:
    generated = html.escape(chain.generate(max_words=max_words))
    return (
        f"{generated}\n\n"
        f"Рады видеть тебя, {mention}! 👋\n\n"
        "Пожалуйста, ознакомься с правилами в описании группы, а также с гайдом.\n\n"
        "💡 <b>Ответь на это сообщение (Reply)</b>, указав свою ОП (например: <code>SE</code>, <code>CS</code>, <code>IT</code>), чтобы узнать своего ответственного админа!"
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
            return await message.reply_text(text, **kwargs)  # type: ignore[attr-defined,no-any-return]
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


QUESTION_PATTERN = re.compile(
    r"(?i)\b("
    r"кто\s+(с|на|из|тут|в|поступил|поступает|учится|выбрал|идет|шел)|"
    r"есть\s+(ли\s+)?кто|"
    r"кто[- ]нибудь|"
    r"а\s+кто|"
    r"ищу\s+(кто|кого|с|на)|"
    r"много\s+(ли\s+)?(тут\s+)?(кто|с|на|из)|"
    r"кого\s+больше"
    r")\b"
)


async def handle_op_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.text or not message.chat:
        return

    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]

    reply_to = message.reply_to_message
    reply_to_id = reply_to.message_id if reply_to else None
    is_reply_to_bot = bool(
        reply_to and reply_to.from_user and reply_to.from_user.id == context.bot.id
    )

    if not tracker.is_target_member(
        chat_id, user_id, reply_to_id, is_reply_to_bot=is_reply_to_bot
    ):
        return

    if QUESTION_PATTERN.search(message.text):
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    matched_ops = op_registry.find_matching_ops(message.text)

    if not matched_ops:
        tracker.record_message(chat_id, user_id)
        if is_reply_to_bot:
            user_mention = (
                message.from_user.mention_html() if message.from_user else "Студент"
            )
            help_text = (
                f"Не удалось распознать ОП в вашем сообщении, {user_mention}. 🤔\n\n"
                "Пожалуйста, укажите код или название вашей ОП (например: <b>SE</b>, <b>CS</b>, <b>IT</b>, <b>BDA</b>, <b>MCS</b>).\n"
                "Полный список доступных ОП можно посмотреть с помощью команды /ops."
            )
            await reply_with_connect_retry(
                message,
                help_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.message_id,
            )
        return

    tracker.remove_user(chat_id, user_id)

    user_mention = (
        message.from_user.mention_html() if message.from_user else "Студент"
    )

    if len(matched_ops) == 1:
        op = matched_ops[0]
        admin_tag = format_admin_tag(op.admin)
        reply_text = (
            f"Привет, {user_mention}! 👋\n\n"
            f"📍 <b>ОП: {html.escape(op.code)} ({html.escape(op.name)})</b>\n"
            f"🏫 <i>{html.escape(op.school)}</i>\n"
            f"👤 Ответственный администратор: {admin_tag}"
        )
    else:
        items = []
        for op in matched_ops:
            admin_tag = format_admin_tag(op.admin)
            items.append(
                f"• <b>{html.escape(op.code)}</b> ({html.escape(op.name)})\n"
                f"  🏫 <i>{html.escape(op.school)}</i> — {admin_tag}"
            )
        formatted_items = "\n\n".join(items)
        reply_text = (
            f"Привет, {user_mention}! 👋\n\n"
            f"📍 <b>Найдены направления (ОП):</b>\n\n{formatted_items}"
        )

    await reply_with_connect_retry(
        message,
        reply_text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id,
    )


async def show_ops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    all_ops = op_registry.get_all()

    by_school: dict[str, list[OPProgram]] = {}
    for op in all_ops.values():
        by_school.setdefault(op.school, []).append(op)

    sections = []
    for school, ops in by_school.items():
        op_lines = []
        for op in ops:
            admin_tag = format_admin_tag(op.admin)
            op_lines.append(
                f"• <b>{html.escape(op.code)}</b> — {html.escape(op.name)} ({admin_tag})"
            )
        lines_str = "\n".join(op_lines)
        sections.append(f"🏫 <b>{html.escape(school)}</b>\n{lines_str}")

    response = "📚 <b>Список образовательных программ (ОП):</b>\n\n" + "\n\n".join(
        sections
    )
    await reply_with_connect_retry(message, response, parse_mode=ParseMode.HTML)


async def set_op_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    registry: AdminRegistry = context.application.bot_data["admins"]
    is_authorized = registry.contains(user.id)

    if not is_authorized and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            membership = await context.bot.get_chat_member(chat.id, user.id)
            if membership.status in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                is_authorized = True
        except TelegramError:
            pass

    if not is_authorized:
        await reply_with_connect_retry(
            message, "Команда доступна только администраторам."
        )
        return

    if not context.args or len(context.args) < 2:
        await reply_with_connect_retry(
            message,
            "Использование: /setopadmin <КОД_ОП> <@username_или_id>\n"
            "Пример: /setopadmin SE @alex_admin",
        )
        return

    code = context.args[0].upper()
    admin = context.args[1]

    op_registry: OPRegistry = context.application.bot_data["op_registry"]
    if op_registry.set_admin(code, admin):
        await reply_with_connect_retry(
            message,
            f"✅ Для ОП <b>{html.escape(code)}</b> успешно назначен администратор {format_admin_tag(admin)}.",
            parse_mode=ParseMode.HTML,
        )
    else:
        all_codes = ", ".join(op_registry.get_all().keys())
        await reply_with_connect_retry(
            message,
            f"❌ ОП '{html.escape(code)}' не найдена. Доступные ОП: {all_codes}",
        )


async def welcome_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.new_chat_members or not message.chat:
        return

    chain: MarkovChain = context.application.bot_data["greeting_chain"]
    settings: Settings = context.application.bot_data["settings"]
    tracker: WelcomeTracker = context.application.bot_data["welcome_tracker"]
    bot_id = context.bot.id

    for member in message.new_chat_members:
        if member.id == bot_id:
            await reply_with_connect_retry(
                message,
                "✅ Бот подключён. Я буду приветствовать новых участников. "
                "Администратор может выполнить /allowpm, чтобы открыть личные "
                "команды /start и /preview.",
            )
            continue
        text = build_welcome_text(chain, member.mention_html(), settings.max_words)
        sent_msg = await reply_with_connect_retry(message, text, parse_mode=ParseMode.HTML)
        if sent_msg and hasattr(sent_msg, "message_id"):
            tracker.add_welcome_message(message.chat.id, sent_msg.message_id, member.id)
        else:
            tracker.add_user(message.chat.id, member.id)


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
    op_registry = OPRegistry(settings.op_admins_path)
    welcome_tracker = WelcomeTracker(max_messages=5)
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
            "op_registry": op_registry,
            "welcome_tracker": welcome_tracker,
            "settings": settings,
        }
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("allowpm", allow_private_messages))
    application.add_handler(CommandHandler("id", show_ids))
    application.add_handler(CommandHandler("ops", show_ops))
    application.add_handler(CommandHandler("setopadmin", set_op_admin))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_op_message)
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

