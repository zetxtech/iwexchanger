import asyncio
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import cached_property, partial
import hashlib
from math import sqrt
import re
from textwrap import dedent, indent
from typing import Any, Dict, Union
from dataclasses import dataclass
from importlib import resources

import names
from thefuzz import process, fuzz
from dateutil import parser
from appdirs import user_data_dir
from loguru import logger
from pyrogram import Client, ContinuePropagation
from pyrogram.handlers import MessageHandler, InlineQueryHandler
from pyrogram.types import (
    BotCommand,
    InputMediaPhoto,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message as TM,
    CallbackQuery as TC,
    User as TU,
    InlineQuery as TI,
)
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import BadRequest
from pyrubrum import (
    DictDatabase,
    Element,
    Menu,
    LinkMenu,
    PageMenu,
    ContentPageMenu,
    MenuStyle,
    PageStyle,
    ParameterizedHandler,
    transform,
)

from . import __name__, image
from .utils import Singleton, flatten2, remove_prefix, truncate_str
from .model import (
    Dispute,
    DisputeType,
    Exchange,
    ExchangeStatus,
    Log,
    Restriction,
    BlackList,
    Trade,
    TradeStatus,
    User,
    UserLevel,
    Field,
    db,
    fn,
    SQL,
    JOIN,
)


class ConversationStatus(Enum):
    WAITING_EXCHANGE = auto()
    WAITING_EXCHANGE_DESC = auto()
    WAITING_TRADE_NAME = auto()
    WAITING_TRADE_DESC = auto()
    WAITING_TRADE_PHOTO = auto()
    WAITING_TRADE_GOOD = auto()
    WAITING_EXCHANGE_FOR = auto()
    WAITING_TRADE_START_TIME = auto()
    WAITING_COINS = auto()
    WAITING_SEARCH_TRADE = auto()
    WAITING_USER = auto()
    WAITING_MESSAGE = auto()
    WAITING_FIELD = auto()
    WAITING_REPORT = auto()
    CHATING = auto()


@dataclass
class Conversation:
    context: Union[TM, TC]
    status: ConversationStatus
    params: Dict[str, Any]


@dataclass
class MessageInfo:
    from_user: User
    trade: Trade


def name(self: TU):
    return " ".join([n for n in (self.first_name, self.last_name) if n])


setattr(TU, "name", property(name))

fake = {}


def user_has_field(user: User, field: str):
    for ur in user.restrictions.where(Restriction.to > datetime.now()):
        for f in ur.fields:
            if f.name == "all":
                return False
            if f.name == field:
                return False
    for ul in user.levels:
        for f in ul.fields:
            if f.name == "all":
                return True
            if f.name == field:
                return True
    else:
        return False


def user_spec(user: User):
    if user.anonymous:
        if user.uid not in fake:
            fake[user.uid] = names.get_first_name()
        return fake[user.uid]
    else:
        return user.name


def useroper(field: str = None, conversation=False, group=False):
    def deco(func):
        async def wrapper(*args, **kw):
            if len(args) == 5:
                self, handler, client, context, parameters = args
            elif len(args) == 4:
                self, client, context, parameters = args
            elif len(args) == 3:
                self, client, context = args
            else:
                raise ValueError("wrong number of arguments")

            async def error(m):
                if isinstance(context, TM):
                    await client.send_message(context.from_user.id, m)
                elif isinstance(context, TC):
                    try:
                        await context.answer(m, show_alert=True)
                    except BadRequest:
                        await client.send_message(context.from_user.id, m)

            if (
                isinstance(context, TM)
                and context.chat.type not in (ChatType.BOT, ChatType.PRIVATE)
                and not group
            ):
                return

            sender = context.from_user
            user, _ = await self.fetch_user(sender)

            if field:
                try:
                    if not user_has_field(user, field):
                        return await error(f"⛔ 您没有权限执行此命令 (需要 {field.upper()}).")
                except Exception as e:
                    logger.opt(exception=e).warning("鉴权时出现错误.")
                    return await error("⚠️ 发生错误.")
            try:
                if not conversation:
                    self.set_conversation(user, context, status=None)
                return await func(*args, user=user, **kw)
            except Exception as e:
                if isinstance(e, ContinuePropagation):
                    raise
                logger.opt(exception=e).warning("回调函数出现错误.")
                return await error("⚠️ 发生错误.")

        return wrapper

    return deco


class Bot(metaclass=Singleton):
    username = "iwexchanger_bot"
    groupname = "iwexchanger_bot_files"

    def __init__(self, token, id, hash, proxy=None):
        self.bot = Client(
            self.username,
            bot_token=token,
            api_id=id,
            api_hash=hash,
            proxy=proxy,
            workdir=user_data_dir(__name__),
        )
        self.started = asyncio.Event()
        self.proxy = proxy

        self._user_conversion: Dict[int, Conversation] = {}
        self._user_messages: Dict[int, MessageInfo] = {}
        self._logo = None

    async def listen(self):
        try:
            await self.bot.start()
            self.started.set()
            await self.setup()
            await asyncio.Event().wait()
        finally:
            try:
                await self.bot.stop()
            except ConnectionError:
                pass

    async def setup(self):
        self.bot.add_handler(MessageHandler(self.text_handler))
        self.bot.add_handler(InlineQueryHandler(self.inline_handler))
        self.menu = ParameterizedHandler(self.tree, DictDatabase())
        self.menu.setup(self.bot)
        with resources.path(image, "logo.png") as f:
            message = await self.bot.send_photo(self.groupname, str(f))
        self._logo = message.photo.file_id
        await self.bot.set_bot_commands([BotCommand("start", "开始使用"), BotCommand("admin", "管理工具")])
        logger.info(f"已启动监听: {self.bot.me.username}.")

    def set_conversation(
        self,
        user: User,
        context: Union[TM, TC] = None,
        status: ConversationStatus = None,
        params: Dict[str, Any] = None,
        **kw,
    ):
        message = context.message if isinstance(context, TC) else context
        current_conv = self._user_conversion.get((message.chat.id, user.uid), None)
        current_params = current_conv.params if current_conv else {}
        params = params or current_params
        params.update(kw)
        self._user_conversion[(message.chat.id, user.uid)] = (
            Conversation(context, status, params) if status else None
        )

    async def to_menu(self, client: Client, context: Union[TC, TM] = None, menu_id="start", uid=None, **kw):
        if not context:
            if not uid:
                raise ValueError("uid must be provided for context constructing")
            message = await client.send_message(uid, "🔄 正在加载")
            user = await client.get_users(uid)
            hash = hashlib.sha1(f"{uid}_{datetime.now().timestamp()}".encode())
            cid = str(int(hash.hexdigest(), 16) % (10**8))
            context = TC(client=client, id=cid, from_user=user, message=message, chat_instance=None)
        if isinstance(context, TC):
            params = getattr(context, "parameters", {})
            params.update(kw)
        else:
            params = kw
        await self.menu[menu_id].on_update(self.menu, client, context, params)

    async def fetch_user(self, u: Union[TU, str, int]):
        if isinstance(u, TU):
            user = u
            uid = u.id
        else:
            user = await self.bot.get_users(u)
            uid = u
        system = User.get(uid="0")
        with db.atomic():
            ur, created = User.get_or_create(uid=uid, defaults={"name": user.name})
            if created:
                user_info = f"uid = {uid}"
                if user.username:
                    user_info = f"{user.username}, {user_info}"
                log = Log.create(initiator=system, activity="create user", details=str(uid))
                log.participants.add(ur)
                logger.info(f"新用户: {user.name} [gray50]({user_info})[/].")
                lr, _ = UserLevel.get_or_create(name="user")
                ur.levels.add(lr)
                log = Log.create(initiator=system, activity="add level to user", details=str(lr.id))
                log.participants.add(ur)
        UserUserLevel = User.levels.get_through_model()
        system_users = (
            UserLevel.select().where(UserLevel.name == "system").join(UserUserLevel).join(User).group_by(User)
        )
        if system_users.count() < 2:
            with db.atomic():
                lr, _ = UserLevel.get_or_create(name="system")
                ur.levels.add(lr)
                log = Log.create(initiator=system, activity="add level to user", details=str(lr.id))
                log.participants.add(ur)
                logger.info(f"[red]用户 {user.name} 已被设为 SYSTEM[/].")
        return ur, created

    @cached_property
    def tree(self):
        ms = lambda **kw: {"parse_mode": ParseMode.MARKDOWN, "style": MenuStyle(back_text="◀️ 返回", **kw)}
        ps = lambda **kw: {
            "parse_mode": ParseMode.MARKDOWN,
            "style": PageStyle(back_text="◀️ 返回", previous_page_text="⬅️", next_page_text="➡️", **kw),
        }
        DMenu = partial(Menu, **ms())
        DDMenu = partial(Menu, **ms(back_enable=False))
        return transform(
            {
                DMenu("Start", "start", self.on_start, default=True): {
                    ContentPageMenu(
                        "🛍️ 交易大厅",
                        "trade_list",
                        self.content_trade_list,
                        header=self.header_trade_list,
                        footer="👇 您可以直接输入以进行搜索",
                        **ps(
                            limit=5,
                            limit_items=10,
                            back_enable=False,
                            extras=["__new_trade_guide", "__trade_list_switch"],
                        ),
                    ): {DMenu("💲 交易详情", "trade_details", self.on_trade_details)},
                    DMenu("👤 我的信息", "user_me", self.on_user_me): {
                        DMenu("💬 开关私聊", "switch_contact", self.on_switch_contact),
                        DMenu("🕵️‍♂️ 开关匿名", "switch_anonymous", self.on_switch_anonymous),
                    },
                },
                DMenu("Admin", "admin", self.on_admin): {
                    DMenu("👤 用户管理", "user_admin", self.on_user_admin): {
                        ContentPageMenu(
                            "👥 列出用户",
                            "users_list",
                            self.content_users_list,
                            header="👇 请按序号选择您需要查询的用户信息:\n",
                            **ps(limit=5, limit_items=10, extras=["__users_message"]),
                        ): {
                            DMenu("用户详情", "user", self.on_user_details, disable_web_page_preview=True): {
                                ContentPageMenu(
                                    "👑 调整用户组",
                                    "user_level",
                                    self.content_user_level,
                                    header="👇🏼 请选择用户隶属的用户组以删除:",
                                    **ps(limit=3, limit_items=6, extras=["__user_level_add"]),
                                ): {DMenu("删除用户组", "user_level_delete", self.on_user_level_delete)},
                                DMenu("⚠️ 永久封禁", "user_delete", self.on_user_delete): {
                                    DMenu("✅ 确认", "user_delete_confirm", self.on_user_delete_confirm)
                                },
                                ContentPageMenu(
                                    "🔨 设置限制",
                                    "user_restriction_set",
                                    self.content_restriction_fields,
                                    header="👇🏼 请选择限制用户权限:",
                                    footer=self.footer_restriction_fields,
                                    **ps(limit=3, limit_items=6, extras=["__user_restriction"]),
                                ): {DMenu("接收限制", "user_restriction_get", self.on_user_restriction_get)},
                                DMenu(
                                    "✅ 移除所有封禁", "user_restriction_delete", self.on_user_restriction_delete
                                ): None,
                                DMenu("✉️ 发送消息", "user_message", self.on_user_message): None,
                            },
                        },
                        ContentPageMenu(
                            "👥 用户组管理",
                            "level_admin",
                            self.content_level_admin,
                            header="👇🏼 请选择用户组:",
                            **ps(limit=10, limit_items=5, back_to="user_admin"),
                        ): {
                            ContentPageMenu(
                                "接收用户组",
                                "level",
                                self.content_level_field,
                                header="👇🏼 请选择权限以删除:",
                                **ps(limit=5, limit_items=10, extras=["__level_field_add"]),
                            ): {DMenu("删除权限", "user_level_field", self.on_level_field_delete)}
                        },
                    },
                    DMenu("ℹ️ 系统信息", "sys_admin", self.on_sys_admin): None,
                },
                Menu("✉️ 向所有人发信", "__users_message", self.on_user_message, **ms(back_to="users_list")): None,
                Menu("🆕️ 新建交易", "__new_trade_guide", self.on_new_trade_guide, **ms(back_to="trade_list")): {
                    Menu("✅ 确认并同意", "new_trade", self.on_new_trade, **ms(back_to="trade_list"))
                },
                DMenu("💰 我的交易", "__trade_list_switch", self.on_trade_list_switch): None,
                DMenu("交易提醒", "__trade_notify", self.on_trade_notify): {
                    DMenu("交易确认", "trade_accept", self.on_trade_accept),
                    DMenu("交易拒绝", "trade_decline", self.on_trade_decline),
                    DMenu("拉黑此人", "trade_blacklist", self.on_trade_blacklist),
                },
                DMenu("交易完成", "__trade_finished", self.on_trade_finish): {
                    PageMenu(
                        "⚠️ 举报交易",
                        "report_after_trade",
                        "🚔 您认为对方的物品存在以下哪种问题?",
                        [Element("未收到货", "no_good"), Element("货不对板", "not_as_description")],
                        **ps(limit=2, limit_items=2),
                    ): {DMenu("接收问题", "report_after_trade_problem", self.on_trade_report)}
                },
                DMenu("增加描述", "__exchange_add_desc", self.on_exchange_add_desc): {
                    DDMenu("不添加", "exchange_no_desc", self.on_exchange_no_desc)
                },
                DMenu("交换提交成功", "__exchange_submitted", self.on_exchange_submitted): None,
                DMenu("增加描述", "__trade_add_desc", self.on_trade_add_desc): {
                    DDMenu("不添加", "trade_no_desc", self.on_trade_no_desc)
                },
                DMenu("增加图片", "__trade_add_photo", self.on_trade_add_photo): {
                    DDMenu("不添加", "trade_no_photo", self.on_trade_no_photo)
                },
                DMenu("设定开始时间", "__trade_set_start_time", self.on_set_trade_start_time): {
                    DDMenu("不设定", "trade_no_start_time", self.on_trade_no_start_time)
                },
                PageMenu(
                    "设定二次确认",
                    "__trade_set_revision",
                    "🚔 对方提供交换物后, 您是否需要检查对方用户和物品描述?\n💡 **无需** 时才能支持硬币购买",
                    [Element("需要", "yes"), Element("无需", "no")],
                    **ps(limit=2, limit_items=2),
                ): {DDMenu("接收二次确认", "trade_revision", self.on_trade_revision)},
                Menu("交易详情公共", "__trade_public", self.on_trade_details_public, **ms(back_to="trade_list")): {
                    DMenu("💲 进行交易", "exchange_public", "💲 请选择您的交易方式:"): {
                        DMenu("💲 以物易物", "exchange_public_item", self.on_exchange),
                        DMenu("💲 使用硬币", "exchange_public_coin", self.on_exchange_coin),
                    },
                    DMenu("⚠️ 举报交易", "report_public", self.on_report): None,
                    DMenu("💬 在线咨询", "contact_public", self.on_contact): None,
                },
                Menu("交易详情管理", "__trade_admin", self.on_trade_details_public, **ms(back_to="trade_list")): {
                    DMenu("💲 进行交易", "exchange_admin", "💲 请选择您的交易方式:"): {
                        DMenu("💲 以物易物", "exchange_admin_item", self.on_exchange),
                        DMenu("💲 使用硬币", "exchange_admin_coin", self.on_exchange_coin),
                    },
                    DMenu("💬 在线咨询", "contact_admin", self.on_contact): None,
                    DMenu("✅ 审核通过", "checked_admin", self.on_checked): None,
                    ContentPageMenu(
                        "⚠️ 举报管理",
                        "report_admin",
                        self.content_report_admin,
                        header="👇🏼 请选择举报信息以查看:",
                        **ps(limit=4, limit_items=4),
                    ): {
                        DMenu("举报详情", "report_details", self.on_report_details): {
                            DMenu("✅ 同意", "report_accept", self.on_report_accept),
                            DMenu("⚠️ 拒绝", "report_decline", self.on_report_decline),
                        }
                    },
                    DMenu("🚫 立刻删除", "violation", self.on_violation): None,
                },
                Menu("交易详情我的", "__trade_mine", self.on_trade_details_mine, **ms(back_to="trade_list")): {
                    DMenu("▶️ 上架下架", "launch", self.on_launch): None,
                    DMenu("🚮 删除交易", "delete", self.on_delete): None,
                    DMenu("🔄 编辑交易", "modify", self.on_modify): None,
                    DMenu("🔗 分享交易", "share", self.on_share): None,
                    ContentPageMenu(
                        "📩 交换请求",
                        "trade_exchange_list",
                        self.content_trade_exchange_list,
                        header="👇 请按序号选择您需要查询的交换请求:\n",
                        **ps(limit=3, limit_items=6),
                    ): {DMenu("接收交换请求", "trade_exchange", self.on_trade_exchange)},
                },
                ContentPageMenu(
                    "➕ 增加用户组",
                    "__user_level_add",
                    self.content_user_level_add,
                    header="👇🏼 请选择用户组以添加:",
                    **ps(limit=5, limit_items=10, back_to="user"),
                ): {DMenu("添加用户组", "user_level_add", self.on_user_level_add)},
                PageMenu(
                    "✅ 确认",
                    "__user_restriction",
                    self.on_user_restriction_ok,
                    [Element(str(h), str(h)) for h in [1, 3, 7, 30, 360]],
                    **ps(limit=5, limit_items=5, back_to="user"),
                ): {DMenu("接收时长", "user_restriction_time", self.on_user_restriction)},
                ContentPageMenu(
                    "➕ 增加权限",
                    "__level_field_add",
                    self.content_level_field_add,
                    header="👇🏼 请选择权限以添加, 或输入以手动添加:",
                    **ps(limit=5, limit_items=10),
                ): {DMenu("添加权限", "level_field_add", self.on_level_field_add)},
            }
        )

    @useroper(None, conversation=True)
    async def text_handler(self, client: Client, message: TM, user: User):
        if message.reply_to_message:
            minfo: MessageInfo = self._user_messages[message.reply_to_message.id]
            if message.text == "/ban":
                BlackList.create(by=user, of=minfo.from_user)
                await message.reply("🈲 已经将对方加入黑名单.")
            elif message.text:
                m = await client.send_message(
                    minfo.from_user.uid,
                    f"💬 __{user_spec(user)}__ 向您发送了 **{minfo.trade.name}** 相关会话:\n\n{message.text}\n\n(回复该信息以开始与对方聊天)",
                )
                self._user_messages[m.id] = MessageInfo(from_user=user, trade=minfo.trade)
                self.set_conversation(
                    user,
                    message,
                    ConversationStatus.CHATING,
                    trade_id=minfo.trade.id,
                    reply_to_user=minfo.from_user.uid,
                )
                m = await message.reply("✅ 已发送.")
                await asyncio.sleep(0.5)
                await m.delete()
                return
            else:
                await message.reply("⚠️ 不受支持的信息类型.")
                await asyncio.sleep(0.5)
                await m.delete()
                return
        conv = self._user_conversion.get((message.chat.id, user.uid), None)
        if not conv:
            message.continue_propagation()
        if conv.status == ConversationStatus.WAITING_REPORT:
            if message.text:
                if message.text.startswith("/"):
                    message.continue_propagation()
            t = Trade.get_by_id(int(conv.params["trade_id"]))
            e = Exchange.get_by_id(int(conv.params["exchange_id"]))
            to_trade = conv.params["to_trade"]
            problem = conv.params["report_after_trade_problem_id"]
            if to_trade:
                if problem == "no_good":
                    type = DisputeType.EXCHANGE_NO_GOOD
                elif problem == "not_as_description":
                    type = DisputeType.EXCHANGE_NOT_AS_DESCRIPTION
            else:
                if problem == "no_good":
                    type = DisputeType.TRADE_NO_GOOD
                elif problem == "not_as_description":
                    type = DisputeType.TRADE_NOT_AS_DESCRIPTION
            with db.atomic():
                d = Dispute.create(
                    trade=t,
                    user=user,
                    type=type,
                    description=message.caption or message.text,
                    photo=message.photo.file_id if message.photo else None,
                    influence=sqrt(max(t.coins, 10)),
                )
                target = e.user if to_trade else t.user
                target.sanity = max(target.sanity - sqrt(max(t.coins, 10)), 0)
                target.save()
                log = Log.create(initiator=user, activity="raise dispute after trade", details=str(d.id))
                log.participants.add(target)
                logger.debug(f"{user.name} 认为与 {(e.user if to_trade else t.user).name} 的交易存在 {type.name} 问题.")
                await message.reply("✅ 成功提交举报, 将等待管理员确认后, 给予对方一定惩罚.")
                return
        if message.text:
            if message.text.startswith("/"):
                message.continue_propagation()
            elif conv.status == ConversationStatus.WAITING_EXCHANGE:
                t = Trade.get_by_id(int(conv.params["trade_id"]))
                if t.revision:
                    target = "__exchange_add_desc"
                else:
                    target = "__exchange_submitted"
                await self.to_menu(client, message, target, exchange=message.text, **conv.params)
            elif conv.status == ConversationStatus.WAITING_EXCHANGE_DESC:
                if len(message.text) > 100:
                    self.set_conversation(user, conv.context, ConversationStatus.WAITING_EXCHANGE_DESC)
                    await message.reply("⚠️ 过长, 最大长度为100.")
                else:
                    await self.to_menu(
                        client, message, "__exchange_submitted", exchange_desc=message.text, **conv.params
                    )
            elif conv.status == ConversationStatus.WAITING_TRADE_NAME:
                if len(message.text) > 20:
                    self.set_conversation(user, conv.context, ConversationStatus.WAITING_TRADE_NAME)
                    await message.reply("⚠️ 过长, 最大长度为20.")
                else:
                    await self.to_menu(
                        client, message, "__trade_add_desc", trade_name=message.text, **conv.params
                    )
            elif conv.status == ConversationStatus.WAITING_TRADE_DESC:
                if len(message.text) > 100:
                    self.set_conversation(user, conv.context, ConversationStatus.WAITING_TRADE_DESC)
                    await message.reply("⚠️ 过长, 最大长度为100.")
                else:
                    await self.to_menu(
                        client, message, "__trade_add_photo", trade_desc=message.text, **conv.params
                    )
            elif conv.status == ConversationStatus.WAITING_TRADE_GOOD:
                self.set_conversation(
                    user, conv.context, ConversationStatus.WAITING_EXCHANGE_FOR, trade_good=message.text
                )
                msg = "👉🏼 请输入你需要的物品名称 (尽可能简短):"
                if conv.params.get("trade_modify", False):
                    t = Trade.get_by_id(int(conv.params["trade_id"]))
                    msg += f"\n🔄 (当前: `{t.exchange}`)"
                await message.reply(msg)

            elif conv.status == ConversationStatus.WAITING_EXCHANGE_FOR:
                if len(message.text) > 100:
                    self.set_conversation(user, conv.context, ConversationStatus.WAITING_EXCHANGE_FOR)
                    return await message.reply("⚠️ 过长, 最大长度为100.")
                self.set_conversation(
                    user, conv.context, ConversationStatus.WAITING_COINS, trade_exchange_for=message.text
                )
                msg = dedent(
                    """
                👉🏼 请输入您的物品的等值价值
                
                用户可以用硬币购买您的物品, 并扣除 10% 手续费.
                输入 0 以禁用硬币购买.
                
                价值参考:
                
                1     - 群组推荐
                10    - 网易云会员七天兑换码
                100   - Emby 邀请码
                1000  - Telegram 账号
                10000 - 奥德赛 Emby 邀请码
                {conv}
                **请注意**: 请勿设置过高, 若对方认定您的商品虚假, 将导致大量扣信用分.
                """
                ).strip()
                if conv.params.get("trade_modify", False):
                    t = Trade.get_by_id(int(conv.params["trade_id"]))
                    msg = msg.format(conv=f"\n🔄 (当前: `{t.coins}`)\n")
                else:
                    msg = msg.format(conv="")
                await message.reply(msg)
            elif conv.status == ConversationStatus.WAITING_COINS:
                retry = False
                try:
                    coins = int(message.text)
                except ValueError:
                    retry = True
                else:
                    if coins < 0:
                        retry = True
                history_sold = (
                    Trade.select()
                    .where(Trade.status == TradeStatus.SOLD)
                    .join(User)
                    .where(User.id == user.id)
                    .count()
                )
                if (history_sold + 1) * 1000 * pow(user.sanity / 100, 10) < coins:
                    retry = "⚠️ 金额过大, 请进行更多交易或提升信用."
                if retry:
                    self.set_conversation(user, conv.context, ConversationStatus.WAITING_COINS)
                    await message.reply(retry if isinstance(retry, str) else "⚠️ 输入错误, 请重新输入.")
                else:
                    await self.to_menu(
                        client, message, "__trade_set_start_time", trade_coins=coins, **conv.params
                    )
            elif conv.status == ConversationStatus.WAITING_TRADE_START_TIME:
                params = {k: v for k, v in conv.context.parameters.items() if k.startswith("trade_")}
                try:
                    trade_start_time = parser.parse()
                except parser.ParserError:
                    await message.reply("⚠️ 输入错误, 请重新输入.")
                    await self.to_menu(client, message, "__trade_set_start_time", **params)
                else:
                    await self.to_menu(
                        client, message, "__trade_set_revision", trade_start_time=trade_start_time, **params
                    )
            elif conv.status == ConversationStatus.WAITING_USER:
                user_id = message.text
                try:
                    u = await client.get_users(user_id)
                    if User.get_or_none(uid=u.id):
                        return await self.to_menu(client, message, "user", user_id=u.id)
                except BadRequest:
                    pass
                uns = {u.uid: u.name for u in User.select().iterator()}
                results = process.extract(user_id, uns, limit=5)
                uids = [uid for _, score, uid in results if score > 75]
                if len(uids) > 1:
                    return await self.to_menu(client, message, "list_users", user_ids=uids)
                elif len(uids) == 1:
                    return await self.to_menu(client, message, "user", user_id=uids[0])
                else:
                    await message.reply("⚠️ 未找到该用户.")
            elif conv.status == ConversationStatus.WAITING_MESSAGE:
                uid = conv.context.parameters.get("user_id", None)
                uids = conv.context.parameters.get("user_ids", [])
                cond = conv.context.parameters.get("cond", None)
                if uid:
                    urs = User.select().where(User.uid == uid)
                elif uids:
                    urs = User.select().where(User.uid.in_(uids))
                elif cond:
                    urs = User.select().where(cond)
                else:
                    urs = User.select()
                fails = 0
                count = urs.count()
                m = await message.reply(f"🔄 正在发送.")
                for i, ur in enumerate(urs.iterator()):
                    try:
                        await client.send_message(
                            ur.uid, f"📢 管理员提醒:\n\n{message.text}", parse_mode=ParseMode.MARKDOWN
                        )
                    except BadRequest:
                        fails += 1
                    await m.edit_text(f"🔄 正在发送: {i+1}/{count} 个用户.")
                if i == 0:
                    await m.edit_text(f"✅ 已发送.")
                else:
                    await m.edit_text(f"✅ 已发送给 {i+1} 个用户, 其中 {fails} 个发送错误.")
            elif conv.status == ConversationStatus.WAITING_FIELD:
                fr = Field.get_or_create(name=message.text)
                await self.to_menu(
                    client,
                    message,
                    "level_field_add",
                    level_id=conv.params["level"].id,
                    level_field_add_id=fr.id,
                )
            elif conv.status == ConversationStatus.WAITING_SEARCH_TRADE:
                tns = {t.id: t.name for t in Trade.select().iterator()}
                tns.update({t.id: t.exchange for t in Trade.select().iterator()})
                results = process.extract(message.text, tns, limit=30, scorer=fuzz.partial_ratio)
                tids = [tid for _, score, tid in results if score > 50]
                if len(tids) > 1:
                    return await self.to_menu(client, message, "trade_list", trade_ids=uids)
                elif len(tids) == 1:
                    return await self.to_menu(client, message, "trade_details", trade_id=tids[0])
                else:
                    await message.reply("⚠️ 未找到该交易.")
            elif conv.status == ConversationStatus.CHATING:
                t = Trade.get_by_id(int(conv.params["trade_id"]))
                u = conv.params.get("reply_to_user", t.user.uid)
                m = await client.send_message(
                    u,
                    f"💬 __{user_spec(user)}__ 向您发送了 **{t.name}** 相关会话:\n\n{message.text}\n\n(回复该信息以开始与对方聊天)",
                )
                self._user_messages[m.id] = MessageInfo(from_user=user, trade=t)
                m = await message.reply("✅ 已发送")
                await asyncio.sleep(0.5)
                await m.delete()
            else:
                message.continue_propagation()
        elif message.photo:
            if conv.status == ConversationStatus.WAITING_TRADE_PHOTO:
                self.set_conversation(
                    user,
                    conv.context,
                    ConversationStatus.WAITING_TRADE_GOOD,
                    trade_photo=message.photo.file_id,
                )
                msg = "👉🏼 请输入你的物品内容 (例如密钥等, 暂不支持图片):"
                if conv.params.get("trade_modify", False):
                    t = Trade.get_by_id(int(conv.params["trade_id"]))
                    msg += f"\n🔄 当前密文内容请点击查看:\n\n||{t.good}||"
                await message.reply(msg)

    async def inline_handler(self, client: Client, inline_query: TI):
        try:
            query = int(inline_query.query)
            t = Trade.get_or_none(id=query)
            if not t:
                raise ValueError
            if not int(t.user.uid) == inline_query.from_user.id:
                raise ValueError
        except ValueError:
            await inline_query.answer(
                results=[],
                cache_time=10,
                is_personal=True,
                switch_pm_text='从易物交易大厅分享交易',
                switch_pm_parameter='inline'
            )
            return
        tu = user_spec(t.user)
        td = f"🛍️ __{tu}__ 正在请求以物易物:\n\n"
        tl = f"t.me/{client.me.username}?start=__t_{t.id}"
        tlu = f"t.me/{client.me.username}"
        if len(t.name) < 10:
            td += f"他拥有: **{t.name}**\n"
        else:
            td += f"他拥有:\n**{t.name}**\n\n"
        td += f"他希望换取: **{t.exchange}**\n\n👇 点击下方按钮以进行交换"
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    title=f"{tu} 发起的交易",
                    input_message_content=InputTextMessageContent(td),
                    description=f"{truncate_str(t.name, 10)} => {truncate_str(t.exchange, 10)}",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("查看详情", url=tl),
                                InlineKeyboardButton("交易大厅", url=tlu),
                            ]
                        ]
                    ),
                ),
            ],
            cache_time=10,
            is_personal=True,
        )

    @useroper()
    async def on_start(self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User):
        if isinstance(context, TM):
            if not context.text:
                return None
            cmds = context.text.split()
            if len(cmds) == 2:
                if cmds[1].startswith("__t_"):
                    return await self.to_menu(
                        client, context, "trade_details", trade_details_id=remove_prefix(cmds[1], "__t_")
                    )
                elif cmds[1].startswith("__u_"):
                    return await self.to_menu(client, context, "user", user_id=remove_prefix(cmds[1], "__u_"))
        name = context.from_user.name
        if user.name != name:
            with db.atomic():
                user.name = name
                user.save()
                Log.create(initiator=user, activity="updated username")
        msg = f"🌈 您好 {name}, 欢迎使用 **易物 Exchanger**!"
        return InputMediaPhoto(media=self._logo, caption=msg, parse_mode=ParseMode.MARKDOWN)

    @useroper("view_trades")
    async def content_trade_list(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        self.set_conversation(user, context, ConversationStatus.WAITING_SEARCH_TRADE)
        if parameters.pop("media_changed", False):
            await context.edit_message_media(InputMediaPhoto(self._logo))
        is_admin = user_has_field(user, "admin_trade")
        mine = parameters.get("mine", False)
        if mine:
            ts = (
                Trade.select()
                .where(Trade.deleted == False)
                .join(User)
                .where(User.id == user.id)
                .order_by(Trade.status, Trade.modified.desc())
            ).iterator()
        elif is_admin:

            def gen():
                tids = []
                for t in (
                    Trade.select()
                    .where(Trade.status < TradeStatus.DISPUTED, Trade.deleted == False)
                    .join(Dispute)
                    .order_by(Dispute.type.desc())
                    .group_by(Trade)
                    .order_by(Trade.modified.desc())
                    .iterator()
                ):
                    tids.append(t.id)
                    yield t
                for t in (
                    Trade.select()
                    .where(Trade.deleted == False)
                    .where((Trade.status == TradeStatus.LAUNCHED) | (Trade.status == TradeStatus.CHECKING))
                    .order_by(Trade.status, Trade.modified.desc())
                    .iterator()
                ):
                    if not t.id in tids:
                        yield t

            ts = gen()
        else:
            ts = (
                Trade.select()
                .where(Trade.status == TradeStatus.LAUNCHED, Trade.deleted == False)
                .join(User)
                .where(User.sanity >= 70)
                .group_by(Trade)
                .iterator()
            )
        items = []
        icons = {
            TradeStatus.PENDING: "📝",
            TradeStatus.CHECKING: "🛡️",
            TradeStatus.LAUNCHED: "🛒",
            TradeStatus.SOLD: "🤝",
            TradeStatus.TIMEDOUT: "⌛️",
            TradeStatus.DISPUTED: "🤔",
            TradeStatus.VIOLATION: "🚫",
        }
        need_admin = True
        only_list = parameters.get('trade_ids', None)
        for i, t in enumerate(ts):
            if only_list and t.id not in only_list:
                continue
            if mine or is_admin:
                spec = f"{icons[t.status]} `{i+1}`"
            else:
                if not user_has_field(user, "add_trade"):
                    continue
                spec = f"`{i+1: >3}`"
            annotation = ""
            if is_admin and need_admin:
                disputes = Dispute.select().join(Trade).where(Trade.id == t.id).count()
                checking = t.status == TradeStatus.CHECKING and not t.deleted
                if not disputes and not checking:
                    need_admin = False
                elif disputes:
                    annotation = " (--需要审查争议--)"
                elif checking:
                    annotation = " (--需要检查--)"
            spec += (
                f" | __{truncate_str(t.exchange, 12)}__{annotation}\n"
                + " " * 5
                + f"=> **{truncate_str(t.name, 10)}**\n"
            )
            items.append((spec, str(i + 1), str(t.id)))
        return items

    @useroper("view_trades")
    async def on_trade_details_public(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        is_admin = user_has_field(user, "admin_trade")

        if t.status == TradeStatus.CHECKING:
            msg = "🛍️ **需要检查**"
        elif t.status == TradeStatus.LAUNCHED:
            if t.available and t.available > datetime.now():
                msg = f"🛍️ 交易将在 {t.available.strftime('%Y-%m-%d %H:%M:%S')} 可用."
            else:
                msg = f"🛍️ 交易详情"
        elif t.status == TradeStatus.SOLD:
            msg = "🛍️ 交易成功"
        elif t.status == TradeStatus.TIMEDOUT:
            msg = "🛍️ 因**过久未更新**被下架"
        elif t.status == TradeStatus.DISPUTED:
            msg = "🛍️ 正处于**纠纷锁定**状态"
        elif t.status == TradeStatus.VIOLATION:
            msg = "🛍️ 因**违反用户协议**被移除"

        if t.deleted:
            msg += " (已删除)"

        if is_admin:
            msg += f"\n\n[{t.user.name}](tg://user?id={user.uid}) ([管理](t.me/{client.me.username}?start=__u_{user.uid})) 正在出售:\n"
        else:
            msg += f"\n\n__{user_spec(t.user)}__ 正在出售:\n"
        msg += f"**{t.name}**"
        if t.description:
            msg += f"\n{t.description}"
        msg += f"\n\n他希望通过以下物品进行交换:\n**{t.exchange}**\n\n"
        msg += f"交易发起日期: {t.created.strftime('%Y-%m-%d')}\n"
        disputes = Dispute.select().join(Trade).where(Trade.id == t.id).count()
        if disputes:
            msg += f"当前该交易有 {disputes} 个举报, "
        msg += f"对方的信用分为 {t.user.sanity}"
        if t.user.sanity < 75:
            msg += " **(极低)**, "
        if t.user.sanity < 90:
            msg += " **(较低)**, "
        else:
            msg += ", "
        msg += f"售出过 {t.user.trades.where(Trade.status == TradeStatus.SOLD).count()} 件商品.\n"
        if t.revision:
            msg += f"**非即时**:\n您提供该物品后, 交易将需要对方检查其描述才能完成. 若对方拒绝交易, 您的物品密文将不会展现."
        else:
            msg += f"**即时**: 您提供对方所需物品后, 交易将立即完成."

        if is_admin:
            disputes = Dispute.select().join(Trade).where(Trade.id == t.id).count()
            if disputes:
                msg += f"\n\n**👑 管理员事务: 该交易有 {disputes} 个争议**\n"
            if t.status == TradeStatus.CHECKING and not t.deleted:
                msg += f"\n\n**👑 管理员事务: 该交易需要检查**\n"

        if t.photo:
            parameters["media_changed"] = True
            return InputMediaPhoto(media=t.photo, caption=msg, parse_mode=ParseMode.MARKDOWN)
        else:
            if parameters.get("from_link", False):
                return InputMediaPhoto(media=self._logo, caption=msg)
            else:
                return msg

    async def header_trade_list(self, handler, client: Client, context: TM, parameters):
        menu = handler["trade_list"]
        items = len(flatten2(menu.entries))
        mine = parameters.get("mine", False)
        if mine:
            return f"🛍️ 我的交易 - 共 {items} 交易\n"
        else:
            return f"🛍️ 交易大厅 - 共 {items} 交易\n"

    def check_trade(self, t: Trade, user: User):
        if t.status != TradeStatus.LAUNCHED:
            return f"⚠️ 交易当前未上架."
        if t.status != TradeStatus.LAUNCHED:
            if t.available and t.available > datetime.now():
                return f"⚠️ 交易仅在 {t.available.strftime('%Y-%m-%d %H:%M:%S')} 后可用!"
        if t.deleted:
            return f"⚠️ 交易已被删除."
        if BlackList.select().where(BlackList.by == t.user, BlackList.of == user).get_or_none():
            return f"⚠️ 对方已将您拉黑, 无法交易."

    @useroper("exchange")
    async def on_exchange(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        check_msg = self.check_trade(t, user)
        if check_msg:
            return check_msg
        self.set_conversation(
            user, context, ConversationStatus.WAITING_EXCHANGE, params={"trade_id": parameters["trade_id"]}
        )
        return f"👉🏼 请输入对方所需的 **{t.exchange}** (例如密钥等, 暂不支持图片):"

    @useroper("exchange")
    async def on_exchange_coin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.coins == 0 or t.revision:
            await context.answer("⚠️ 不支持硬币购买.")
            return
        check_msg = self.check_trade(t, user)
        if check_msg:
            return check_msg
        if user.coins < t.coins:
            await context.answer("⚠️ 硬币不足.")
            return
        with db.atomic():
            user.coins -= t.coins
            t.user.coins += t.coins
            await self.to_menu(
                client,
                context,
                "__exchange_submitted",
                trade_id=t.id,
                coins=t.coins,
                exchange=f"{t.coins} 硬币",
            )

    @useroper()
    async def on_exchange_add_desc(self, handler, client: Client, context: TM, parameters: dict, user: User):
        params = {k: parameters[k] for k in ("trade_id", "exchange")}
        self.set_conversation(user, context, status=ConversationStatus.WAITING_EXCHANGE_DESC, params=params)
        return "📝 添加一个描述\n使售卖者更加了解您的物品并接受该交易, 您可以直接输入您的描述, 或点击下方的**不添加**按钮以跳过.\n(请勿输入任何密文)"

    @useroper()
    async def on_exchange_no_desc(self, handler, client: Client, context: TC, parameters: dict, user: User):
        return await self.to_menu(client, context, "__exchange_submitted", exchange_desc=None)

    @useroper()
    async def on_trade_add_desc(self, handler, client: Client, context: TM, parameters: dict, user: User):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        self.set_conversation(user, context, ConversationStatus.WAITING_TRADE_DESC, params=params)
        msg = "📝 添加一个描述\n使交换者更加了解您的物品, 您可以直接输入您的描述, 或点击下方的**不添加**按钮以跳过.\n(请勿输入任何密文)"
        if parameters.get("trade_modify", False):
            t = Trade.get_by_id(int(parameters["trade_id"]))
            if t.description:
                msg += f"\n🔄 (当前: `{t.description}`)"
            else:
                msg += f"\n🔄 (当前: 不添加)"
        return msg

    @useroper()
    async def on_trade_no_desc(self, handler, client: Client, context: TC, parameters: dict, user: User):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        params["trade_desc"] = None
        await self.to_menu(client, context, "__trade_add_photo", **params)

    @useroper()
    async def on_trade_add_photo(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        self.set_conversation(user, context, status=ConversationStatus.WAITING_TRADE_PHOTO, params=params)
        return "📝 添加一个描述图片\n使交换者更加了解您的物品, 您可以直接发送您的图片, 或点击下方的**不添加**按钮以跳过.\n(请勿涉及任何密文)"

    @useroper()
    async def on_trade_no_photo(self, handler, client: Client, context: TC, parameters: dict, user: User):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        params["trade_photo"] = None
        self.set_conversation(user, context, status=ConversationStatus.WAITING_TRADE_GOOD, params=params)
        msg = "👉🏼 请输入你的物品内容 (例如密钥等, 暂不支持图片):"
        if parameters.get("trade_modify", False):
            t = Trade.get_by_id(int(parameters["trade_id"]))
            msg += f"\n🔄 当前密文内容请点击查看:\n\n||{t.good}||"
        return msg

    @useroper()
    async def on_set_trade_start_time(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        self.set_conversation(
            user, context, status=ConversationStatus.WAITING_TRADE_START_TIME, params=params
        )
        msg = "📝 指定时间才允许交易\n请输入 `YYYY-mm-dd hh:mm:ss` 格式的时间, 或点击下方的**不添加**按钮以跳过."
        if parameters.get("trade_modify", False):
            t = Trade.get_by_id(int(parameters["trade_id"]))
            if t.available > datetime.now():
                msg += f'\n🔄 (当前: `{t.available.strftime("%Y-%m-%d %H:%M:%S")}`)'
            else:
                msg += f"\n🔄 (当前: 不设定)"
        return msg

    @useroper()
    async def on_trade_no_start_time(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        await self.to_menu(client, context, "__trade_set_revision", trade_start_time=None)

    @useroper("exchange")
    async def on_exchange_submitted(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        exchange = parameters["exchange"]
        description = parameters.get("exchange_desc", None)
        coins = parameters.get("coins", 0)
        check_msg = self.check_trade(t, user)
        if check_msg:
            return check_msg
        with db.atomic():
            e = Exchange.create(user=user, trade=t, exchange=exchange, description=description, coins=coins)
            log = Log.create(initiator=user, activity="join exchange on trade", details=str(t.id))
            log.participants.add(t.user)
            logger.debug(f"{user.name} 参与了 {t.user.name} 发起的交易.")
        if not t.revision:
            await self.to_menu(
                self.bot,
                menu_id="__trade_finished",
                uid=t.user.uid,
                trade_id=t.id,
                exchange_id=e.id,
                to_trade=True,
            )
            with db.atomic():
                e.status = ExchangeStatus.ACCEPTED
                e.save()
                t.status = TradeStatus.SOLD
                t.save()
                e.user.sanity = min(e.user.sanity + max(sqrt(max(t.coins, 10)) / 20, 3), 100)
                e.user.save()
                t.user.sanity = min(t.user.sanity + max(sqrt(max(t.coins, 10)) / 5, 5), 100)
                t.user.save()
                log = Log.create(initiator=user, activity="buy trade", details=str(e.id))
                log.participants.add(t.user)
            await self.to_menu(
                self.bot, context, "__trade_finished", trade_id=t.id, exchange_id=e.id, to_trade=False
            )
        else:
            await self.to_menu(
                self.bot,
                menu_id="__trade_notify",
                uid=t.user.uid,
                trade_id=t.id,
                exchange_id=e.id,
                to_trade=True,
            )
            return f"🔄 等待交易.\n\n您需要等待 {user_spec(t.user)} 审核您的物品描述后即可通过推送获得对方物品."

    @useroper("community")
    async def on_report(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if user_has_field(t.user, "admin_trade"):
            await context.answer("⚠️ 无法举报管理员.")
            return
        if user.sanity < max(t.user.sanity - 10, 70):
            await context.answer("⚠️ 您的信誉值低于70或低于对方, 无法举报.")
            return
        with db.atomic():
            d = Dispute.get_or_none(user=user, trade=t, type=DisputeType.VIOLATION)
            if not d:
                Dispute.create(user=user, trade=t, type=DisputeType.VIOLATION, influence=10)
                t.user.sanity = max(t.user.sanity - 10, 0)
                t.user.save()
                log = Log.create(initiator=user, activity="report on a trade", details=str(t.id))
                log.participants.add(t.user)
                logger.debug(f"{user.name} 举报了 {t.user.name} 发起的交易.")
                await context.answer("✅ 成功举报.")
            else:
                d.trade.user.sanity = min(d.trade.user.sanity + d.influence, 100)
                d.trade.user.save()
                d.delete_instance()
                log = Log.create(initiator=user, activity="cancel report on a trade", details=str(t.id))
                log.participants.add(t.user)
                logger.debug(f"{user.name} 取消举报了 {t.user.name} 发起的交易.")
                await context.answer("✅ 成功取消举报.")
        await self.to_menu(client, context, "trade_details")

    @useroper("community")
    async def on_contact(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if not t.user.chat:
            await context.answer("⚠️ 对方禁用了在线联系.")
            return
        if BlackList.get_or_none(by=t.user, of=user):
            await context.answer("⚠️ 对方已将您拉黑.")
            return
        self.set_conversation(user, context, ConversationStatus.CHATING, trade_id=parameters["trade_id"])
        return f"💬 接下来, 您可以向 __{user_spec(t.user)}__ 发送消息, 使用任意命令以结束."

    @useroper("add_trade")
    async def on_new_trade_guide(self, handler, client: Client, context: TC, parameters: dict, user: User):
        if user.sanity < 60:
            await context.answer("⚠️ 信誉过低, 已被封禁.")
            return
        if (
            Trade.select()
            .where(Trade.status == TradeStatus.LAUNCHED, Trade.deleted == False)
            .join(User)
            .where(User.id == user.id)
            .count()
            > 5
        ):
            await context.answer("⚠️ 不能上架超过 5 个交易.")
            return
        return dedent(
            """
        🌈 欢迎在 **易物** 置换平台发起交易, 规则如下:
        1. 禁止发送政治/暴力/虐待/儿童色情/赌博/毒品/非法交易等置换. (永封)
        2. 禁止将色情图片作为物品描述图. (7天)
        3. 当您上架的物品出现虚假/货不对板, 并被交换者举报, 我们将扣除您的信用分 (当前为 {sanity}), 低于 90 的信用分将导致您被检查, 低于 70 的信用分将导致您被封禁.
        4. 在商品描述中加入图片/链接可能需要等待管理员检查才可上架.
        """.format(
                sanity=user.sanity
            )
        ).strip()

    @useroper()
    async def on_new_trade(self, handler, client: Client, context: TC, parameters: dict, user: User):
        params = {k: v for k, v in parameters.items() if k.startswith("trade_")}
        self.set_conversation(user, context, ConversationStatus.WAITING_TRADE_NAME, params=params)
        msg = "👉🏼 请输入您可供交换的物品名称 (尽可能简短):"
        if parameters.get("trade_modify", False):
            t = Trade.get_by_id(int(parameters["trade_id"]))
            msg += f"\n🔄 (当前: `{t.name}`)"
        await client.send_message(user.uid, msg)

    @useroper("add_trade")
    async def on_trade_revision(self, handler, client: Client, context: TC, parameters: dict, user: User):
        trade_revision = parameters["trade_revision_id"]
        if trade_revision == "yes":
            trade_revision = True
        else:
            trade_revision = False
        if parameters.get("trade_modify", False):
            with db.atomic():
                t = Trade.get_by_id(int(parameters["trade_id"]))
                t.name = parameters["trade_name"]
                t.exchange = parameters["trade_exchange_for"]
                t.coins = parameters["trade_coins"]
                t.description = parameters["trade_desc"]
                t.photo = parameters["trade_photo"]
                t.good = parameters["trade_good"]
                t.available = parameters["trade_start_time"] or datetime.now()
                t.revision = trade_revision
                t.modified = datetime.now()
                t.save()
                Log.create(initiator=user, activity="modify a trade", details=str(t.id))
        else:
            with db.atomic():
                t = Trade.create(
                    user=user,
                    name=parameters["trade_name"],
                    exchange=parameters["trade_exchange_for"],
                    coins=parameters["trade_coins"],
                    description=parameters["trade_desc"],
                    photo=parameters["trade_photo"],
                    good=parameters["trade_good"],
                    available=parameters["trade_start_time"] or datetime.now(),
                    revision=trade_revision,
                )
                Log.create(initiator=user, activity="add a trade", details=str(t.id))
        with db.atomic():
            if (not user_has_field(user, "admin_trade")) and self.trade_requires_check(t):
                t.status = TradeStatus.CHECKING
                t.save()
                Log.create(initiator=user, activity="launch a trade", details="requires checking")
                logger.debug(f'{user.name} 提交了一个出售 "{t.name}" 的交易待检查.')
                msg = "🛡️ 等待管理员检查后上架."
            else:
                t.status = TradeStatus.LAUNCHED
                t.save()
                Log.create(initiator=user, activity="launch a trade", details="launched")
                logger.debug(f'{user.name} 上架了一个出售 "{t.name}" 的交易.')
                msg = "⭐ 成功上架."
        await client.send_message(user.uid, msg)

    @useroper()
    async def on_trade_details(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        tid = int(parameters["trade_details_id"])
        if parameters.pop("media_changed", False):
            await context.edit_message_media(InputMediaPhoto(self._logo))
        if not tid:
            return "⚠️ 无效交易!"
        t = Trade.get_or_none(id=tid)
        if not t:
            return "⚠️ 没有找到该交易!"

        if t.user.id == user.id:
            await self.to_menu(
                client, context, "__trade_mine", trade_id=t.id, from_link=isinstance(context, TM)
            )
        elif user_has_field(user, "admin_trade"):
            await self.to_menu(
                client, context, "__trade_admin", trade_id=t.id, from_link=isinstance(context, TM)
            )
        else:
            await self.to_menu(
                client, context, "__trade_public", trade_id=t.id, from_link=isinstance(context, TM)
            )

    @useroper()
    async def on_trade_list_switch(self, handler, client: Client, context: TC, parameters: dict, user: User):
        if parameters.get("mine", False):
            parameters["mine"] = False
            await context.answer("🏛️ 当前显示交易大厅.")
            handler["__trade_list_switch"].name = "💰 我的交易"
        else:
            parameters["mine"] = True
            await context.answer("💰 当前显示我的交易.")
            handler["__trade_list_switch"].name = "🏛️ 交易大厅"
        await self.to_menu(client, context, "trade_list")

    @useroper()
    async def on_trade_details_mine(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.status == TradeStatus.PENDING:
            status = "**未发布**"
        elif t.status == TradeStatus.CHECKING:
            status = "正在检查**待上架**"
        elif t.status == TradeStatus.LAUNCHED:
            if t.available and t.available > datetime.now():
                status = f"已上架并在**未来**开放购买"
            else:
                status = f"**已上架**"
        elif t.status == TradeStatus.SOLD:
            status = "交易成功"
        elif t.status == TradeStatus.TIMEDOUT:
            status = "因**过久未更新**被下架"
        elif t.status == TradeStatus.DISPUTED:
            status = "正处于**纠纷锁定**状态"
        elif t.status == TradeStatus.VIOLATION:
            status = "因**违反用户协议**被移除"
        if t.deleted:
            status += " (已删除)"
        msgs = [f"物品名称: **{t.name}**"]
        if t.description:
            if len(t.description) > 14:
                msgs += [f"物品描述:\n{t.description}"]
            else:
                msgs += [f"物品描述: {t.description}"]
        if t.coins:
            msgs += [f"等值硬币: {t.coins}"]
        msgs += [
            f"需要审核: {'是' if t.revision else '否'}",
            f"物品密文: <已隐藏>",
        ]
        if len(t.exchange) > 14:
            msgs += [f"\n意向交换:\n{t.exchange}"]
        else:
            msgs += [f"意向交换: {t.exchange}"]
        if t.available > datetime.now():
            msgs.append(f"可用时间: {t.available.strftime('%Y-%m-%d %H:%M:%S')}")
        msg = f"ℹ️ 您的交易 ({status})\n\n" + indent("\n".join(msgs), " " * 3)
        exchanges = (
            Exchange.select()
            .where(Exchange.status == ExchangeStatus.LAUNCHED)
            .join(Trade)
            .where(Trade.id == t.id)
        )
        if exchanges.count():
            msg += "\n\n📩 交换请求:\n\n"
            for e in exchanges.iterator():
                msg += f"   - {user_spec(e.user)}"
                if e.description:
                    msg += f": {truncate_str(e.description, 15)}"
                msg += "\n"
        if t.photo:
            parameters["media_changed"] = True
            return InputMediaPhoto(media=t.photo, caption=msg)
        else:
            if parameters.get("from_link", False):
                return InputMediaPhoto(media=self._logo, caption=msg)
            else:
                return msg

    def trade_requires_check(self, trade):
        if trade.photo:
            return True
        url_pattern = re.compile(
            r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        )
        if any(url_pattern.search(f) for f in (trade.name, trade.description, trade.exchange) if f):
            return True
        if trade.user.sanity < 90:
            return True

    @useroper()
    async def on_share(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        tu = user_spec(t.user)
        td = f"🌈以下是将被分享的商品海报:\n\n🛍️ __{tu}__ 正在请求以物易物:\n\n"
        tl = f"t.me/{client.me.username}?start=__t_{t.id}"
        tlu = f"t.me/{client.me.username}"
        if len(t.name) < 10:
            td += f"他拥有: **{t.name}**\n"
        else:
            td += f"他拥有:\n**{t.name}**\n\n"
        td += f"他希望换取: **{t.exchange}**\n\n👇 点击下方按钮以进行交换"
        await client.send_message(
            user.uid,
            td,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("查看详情", url=tl),
                        InlineKeyboardButton("交易大厅", url=tlu),
                    ],
                    [InlineKeyboardButton("确认并分享到聊天", switch_inline_query=str(t.id))],
                ]
            ),
        )
        await context.answer()

    @useroper()
    async def on_launch(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.status >= TradeStatus.SOLD or t.deleted:
            await context.answer("⚠️ 交易关闭无法上架.")
            return
        if t.status == TradeStatus.PENDING:
            if not user_has_field(user, "add_trade"):
                await context.answer("⚠️ 没有权限.")
                return
            if (
                Trade.select()
                .where(Trade.status == TradeStatus.LAUNCHED, Trade.deleted == False)
                .join(User)
                .where(User.id == user.id)
                .count()
                > 5
            ):
                await context.answer("⚠️ 不能上架超过 5 个交易.")
                return
            with db.atomic():
                if (not user_has_field(user, "admin_trade")) and self.trade_requires_check(t):
                    t.status = TradeStatus.CHECKING
                    Log.create(initiator=user, activity="launch a trade", details="requires checking")
                    logger.debug(f'{user.name} 提交了一个出售 "{t.name}" 的交易待检查.')
                    await context.answer("🛡️ 等待管理员检查后上架.")
                else:
                    t.status = TradeStatus.LAUNCHED
                    Log.create(initiator=user, activity="launch a trade", details="launched")
                    logger.debug(f'{user.name} 上架了一个出售 "{t.name}" 的交易.')
                    await context.answer("✅ 成功上架.")
                t.save()
            await self.to_menu(client, context, "__trade_mine")
        else:
            with db.atomic():
                t.status = TradeStatus.PENDING
                t.save()
                Log.create(initiator=user, activity="unlaunch a trade", details=str(t.id))
                await context.answer("✅ 成功下架.")
                await self.to_menu(client, context, "__trade_mine")

    @useroper()
    async def on_delete(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.deleted:
            await context.answer("⚠️ 交易已经被删除.")
            return
        if t.status < TradeStatus.DISPUTED:
            await context.answer("⚠️ 无法删除争议交易.")
            return
        if Dispute.select().join(Trade).where(Trade.id == t.id).count():
            await context.answer("⚠️ 无法删除争议交易.")
            return
        with db.atomic():
            t.deleted = True
            t.save()
            Log.create(initiator=user, activity="delete a trade", details=str(t.id))
            await context.answer("✅ 成功删除.")
            await self.to_menu(client, context, "trade_list")

    @useroper()
    async def on_modify(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.status >= TradeStatus.SOLD or t.deleted:
            await context.answer("⚠️ 交易关闭无法修改.")
        elif Dispute.select().join(Trade).where(Trade.id == t.id).count():
            await context.answer("⚠️ 无法修改争议交易.")
        else:
            await self.to_menu(client, context, "new_trade", trade_modify=True)

    @useroper("admin")
    async def on_admin(self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User):
        return f"👇 您好管理员 {context.from_user.name}, 请选择管理指令"

    @useroper("admin")
    async def on_sys_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        latest_user_r: User = User.select().where(User.uid != "0").order_by(User.created.desc()).get()
        trades = Trade.select().where(Trade.status > TradeStatus.PENDING, Trade.deleted == False)
        latest_activity_r = User.select().where(User.uid != "0").order_by(User.activity.desc()).get()
        latest_log_r: Log = Log.select().order_by(Log.created.desc()).get()
        latest_log_user = latest_log_r.initiator
        if latest_log_user.uid == "0":
            latest_log_spec = f"**{latest_log_user.name}**: {latest_log_r.activity}"
        else:
            latest_log_spec = f"[{latest_log_r.initiator.name}](tg://user?id={latest_log_r.initiator.uid}): {latest_log_r.activity}"
        msg = f"⭐ 当前系统信息:\n\n" + indent(
            "\n".join(
                [
                    f"有效用户: {User.select().count()}",
                    f"最新用户: [{latest_user_r.name}](tg://user?id={latest_user_r.uid})",
                    f"最近访问: [{latest_activity_r.name}](tg://user?id={latest_activity_r.uid})",
                    f"总交易数: {trades.count()}",
                    f"最新日志: {latest_log_spec}",
                ]
            ),
            " " * 3,
        )
        return msg

    @useroper("admin_user")
    async def on_user_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_USER)
        return "👇🏼 请选择指令\n\n👉🏼 或输入用户以搜索"

    @useroper("admin_user")
    async def content_users_list(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        parameters.pop("user_id", None)
        user_ids = parameters.get("user_ids", None)
        cond = parameters.get("cond", None)
        urs = (
            User.select(User, fn.Count(Field.id).alias("fields_count"))
            .join(User.levels.get_through_model(), JOIN.LEFT_OUTER)
            .join(UserLevel, JOIN.LEFT_OUTER)
            .join(UserLevel.fields.get_through_model(), JOIN.LEFT_OUTER)
            .join(Field, JOIN.LEFT_OUTER)
        )
        if user_ids:
            urs = urs.where(User.uid.in_(user_ids))
        elif cond:
            urs = urs.where(cond)
        admins = (
            urs.where(User.uid != "0").where((Field.name == "admin") | (Field.name == "all")).group_by(User)
        )
        urs = urs.where(User.uid != "0").order_by(SQL("fields_count")).group_by(User)

        items = []
        admin_ids = []
        count = 0
        for ur in admins.iterator():
            count += 1
            admin_ids.append(ur.id)
            items.append(
                (f"`{count: >3}` | [{ur.name}](tg://user?id={ur.uid}) (**Admin**)", str(count), ur.uid)
            )
        for ur in urs.iterator():
            if ur.id not in admin_ids:
                count += 1
                items.append((f"`{count: >3}` | [{ur.name}](tg://user?id={ur.uid})", str(count), ur.uid))
        if not items:
            await context.answer("⚠️ 当前没有用户!")
        return items

    @useroper("admin_user")
    async def on_user_details(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        parameters.pop("fields", None)
        uid = int(parameters["user_id"])
        ur = User.get_or_none(uid=uid)
        if not ur:
            return "⚠️ 用户未注册"

        trades = ur.trades.where(Trade.status > TradeStatus.PENDING, Trade.deleted == False).count()
        trade_disputes = ur.trades.where(Trade.status >= TradeStatus.DISPUTED).count()
        trade_sold = ur.trades.where(Trade.status == TradeStatus.SOLD).count()
        exchanges = ur.exchanges.count()
        exchanges_disputes = ur.exchanges.where(Exchange.status >= ExchangeStatus.DISPUTED).count()
        exchanges_accepted = ur.exchanges.where(Exchange.status == ExchangeStatus.ACCEPTED).count()
        last_active_time = ur.activity.strftime("%Y-%m-%d")
        msg = "ℹ️ 用户信息如下\n\n" + indent(
            "\n".join(
                [
                    f"用户 ID: `{uid}`",
                    f"用户昵称: [{ur.name}](tg://user?id={ur.uid})",
                    f"角色信用: {ur.sanity}",
                    f"角色硬币: {ur.coins}",
                    f"交易总数: {trades}",
                    f"交易成功: {trade_sold}",
                    f"交易争议: {trade_disputes}",
                    f"交换总数: {exchanges}",
                    f"交换成功: {exchanges_accepted}",
                    f"交换争议: {exchanges_disputes}",
                    f"最近活跃: {last_active_time}",
                    f"用户组: {', '.join([l.name for l in ur.levels])}",
                ]
            ),
            " " * 3,
        )

        if ur.restrictions.count():
            msg += "\n\n🚨 封禁历史记录\n\n"
            for r in ur.restrictions.order_by(Restriction.to.desc()).iterator():
                if r.to > datetime.now():
                    msg += f"   - By {r.by.name} ({r.created.strftime('%Y-%m-%d')}) to **{r.to.strftime('%Y-%m-%d')}**)\n"
                    for f in r.fields:
                        msg += f"     封禁: {f.name}\n"
                else:
                    msg += f"   - By {r.by.name} ~~({r.created.strftime('%Y-%m-%d')}, {(r.to - r.created).days} days)~~\n"
        return msg

    @useroper("admin_user")
    async def content_user_level(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ur = User.get(uid=int(parameters["user_id"]))
        items = []
        for i, ulr in enumerate(ur.levels):
            items.append((f"`{i+1: >3}` | {ulr.name} ({ulr.fields.count()} 个权限)", str(i + 1), ulr.id))
        return items

    @useroper("admin_user")
    async def on_user_level_delete(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ur = User.get(uid=int(parameters["user_id"]))
        lr = UserLevel.get_by_id(int(parameters["user_level_delete_id"]))
        if ur.id == user.id:
            await context.answer("⚠️ 无法设置自己的用户组.")
            return
        if user_has_field(ur, "all"):
            await context.answer("⚠️ 无法设置超级管理员的用户组.")
            return
        if user_has_field(ur, "admin"):
            if not user_has_field(user, "admin_admin"):
                await context.answer("⚠️ 无权限设置管理员相关设置.")
                return
        with db.atomic():
            ur.levels.remove(lr)
            log = Log.create(initiator=user, activity="remove level from user", details=str(lr.id))
            log.participants.add(ur)
            logger.debug(f"{user.name} 设置 {ur.name} 减少了 {lr.name} 等级.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "user")

    @useroper("admin_user")
    async def content_user_level_add(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        ur = User.get(uid=int(parameters["user_id"]))
        items = []
        for i, ulr in enumerate(UserLevel.select().iterator()):
            if Field.get(name="all") in ulr.fields:
                continue
            if ulr not in ur.levels:
                items.append((f"`{i+1: >3}` | {ulr.name} ({ulr.fields.count()} 个权限)", str(i + 1), ulr.id))
        if not items:
            await context.answer("⚠️ 用户已经隶属于目前所有可用用户组.")
            return
        return items

    @useroper("admin_user")
    async def on_user_level_add(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ur = User.get(uid=int(parameters["user_id"]))
        lr = UserLevel.get_by_id(int(parameters["user_level_add_id"]))
        if ur.id == user.id:
            await context.answer("⚠️ 无法设置自己的用户组.")
            return
        if user_has_field(ur, "all"):
            await context.answer("⚠️ 无法设置超级管理员的用户组.")
            return
        if user_has_field(ur, "admin"):
            require_admin_admin = False
        else:
            for f in lr.fields:
                if f.name == "admin":
                    require_admin_admin = True
                    break
            else:
                require_admin_admin = False
        if require_admin_admin:
            if not user_has_field(user, "admin_admin"):
                await context.answer("⚠️ 无权限设置管理员相关设置.")
                return
        with db.atomic():
            if not lr in ur.levels:
                ur.levels.add(lr)
            else:
                await context.answer("⚠️ 用户组已存在.")
                return
            log = Log.create(initiator=user, activity="add level to user", details=str(lr.id))
            log.participants.add(ur)
            logger.debug(f"{user.name} 设置 {ur.name} 增加了 {lr.name} 等级.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "user")

    @useroper("admin_user")
    async def on_user_delete(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ur = User.get(uid=int(parameters["user_id"]))
        if ur.id == user.id:
            await context.answer("⚠️ 无法封禁自己.")
            return
        if user_has_field(ur, "all"):
            await context.answer("⚠️ 无法封禁超级管理员.")
            return
        if user_has_field(ur, "admin"):
            await context.answer("⚠️ 请先去除其管理员权限.")
            return
        return f"⚠️ 你确定要永久封禁 {ur.name} 吗?"

    @useroper("admin_user")
    async def on_user_delete_confirm(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        ur = User.get(uid=int(parameters["user_id"]))
        with db.atomic():
            r = Restriction.create(user=ur, by=user, to=datetime(9999, 12, 31))
            r.fields.add(Field.get(name="all"))
            log = Log.create(initiator=user, activity="ban user")
            log.participants.add(ur)
            logger.debug(f"{user.name} 封禁了 {ur.name}.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "user")

    @useroper("admin_user")
    async def content_restriction_fields(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        ur = User.get(uid=int(parameters["user_id"]))
        if ur.id == user.id:
            await context.answer("⚠️ 无法限制自己.")
            return
        if user_has_field(ur, "all"):
            await context.answer("⚠️ 无法限制超级管理员.")
            return
        if user_has_field(ur, "admin"):
            await context.answer("⚠️ 请先去除其管理员权限.")
            return
        items = []
        for i, fr in enumerate(Field.select().join(UserLevel.fields.get_through_model()).iterator()):
            items.append((f"`{i+1: >3}` | {fr.name}", str(i + 1), fr.id))
        return items

    @useroper("admin_user")
    async def footer_restriction_fields(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        if "fields" in parameters:
            return f"**当前选择: {','.join([Field.get_by_id(fid).name for fid in parameters['fields']])}**"
        else:
            return ""

    @useroper("admin_user")
    async def on_user_restriction(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ur = User.get(uid=int(parameters["user_id"]))
        frs = [Field.get_by_id(fid) for fid in parameters.pop("fields")]
        time = int(parameters["user_restriction_time_id"])
        with db.atomic():
            r = Restriction.create(user=ur, by=user, to=datetime.now() + timedelta(days=time))
            for fr in frs:
                r.fields.add(fr)
            log = Log.create(initiator=user, activity="restrict user", details=str(r.id))
            log.participants.add(ur)
            logger.debug(f"{user.name} 对 {ur.name} 执行了 {time} 天的限制.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "user")

    @useroper("admin_user")
    async def on_user_restriction_get(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        fr = Field.get_by_id(int(parameters["user_restriction_get_id"]))
        if not "fields" in parameters:
            parameters["fields"] = [fr.id]
        else:
            parameters["fields"].append(fr.id)
        await context.answer("✅ 已选择")
        await self.to_menu(client, context, "user_restriction_set")

    @useroper("admin_user")
    async def on_user_restriction_ok(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        if "fields" not in parameters:
            await context.answer("⚠️ 尚未选择")
        else:
            return "🕒 选择惩罚时长"

    @useroper("admin_user")
    async def on_user_restriction_delete(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        ur = User.get(uid=int(parameters["user_id"]))
        rs = Restriction.select().where(Restriction.to > datetime.now()).join(User).where(User.id == ur.id)
        if not rs.count():
            await context.answer("⚠️ 用户未被限制.")
            return
        with db.atomic():
            for r in rs:
                r.to = datetime.now()
                r.save()
                log = Log.create(initiator=user, activity="remove restriction from user", details=str(r.id))
                log.participants.add(ur)
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "user")

    @useroper("admin_message")
    async def on_user_message(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_MESSAGE)
        return "✉️ 请输入信息:"

    @useroper("admin_admin")
    async def content_level_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        items = []
        for i, ulr in enumerate(UserLevel.select().iterator()):
            items.append((f"`{i+1: >3}` | {ulr.name} ({ulr.fields.count()} 个权限)", str(i + 1), ulr.id))
        return items

    @useroper("admin_admin")
    async def content_level_field(self, handler, client: Client, context: TC, parameters: dict, user: User):
        lr = UserLevel.get_by_id(int(parameters["level_id"]))
        items = []
        for i, fr in enumerate(lr.fields):
            items.append((f"`{i+1: >3}` | {fr.name}", str(i + 1), fr.id))
        return items

    @useroper("admin_admin")
    async def on_level_field_delete(self, handler, client: Client, context: TC, parameters: dict, user: User):
        lr = UserLevel.get_by_id(int(parameters["level_id"]))
        fr = Field.get_by_id(int(parameters["user_level_field_id"]))
        if fr.name == "all":
            await context.answer("⚠️ 无法删除超级管理员权限.")
            return
        with db.atomic():
            lr.fields.remove(fr)
            Log.create(initiator=user, activity="delete field from level", details=f"{lr.id}, {fr.id}")
            logger.debug(f"{user.name} 从 {lr.name} 等级删除了 {fr.name} 权限.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "level")

    @useroper("admin_admin")
    async def content_level_field_add(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        lr = UserLevel.get_by_id(int(parameters["level_id"]))
        self.set_conversation(user, context, ConversationStatus.WAITING_FIELD, level=lr)
        items = []
        for i, fr in enumerate(Field.select().join(UserLevel.fields.get_through_model()).iterator()):
            items.append((f"`{i+1: >3}` | {fr.name}", str(i + 1), fr.id))
        return items

    @useroper("admin_admin")
    async def on_level_field_add(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        lr = UserLevel.get_by_id(int(parameters["level_id"]))
        fr = Field.get_by_id(int(parameters["level_field_add_id"]))
        if fr.name == "all":
            if not user_has_field(user, "all"):
                context.answer("⚠️ 超级管理员才能增加超级管理员权限.")
                return
        with db.atomic():
            lr.fields.add(fr)
            Log.create(initiator=user, activity="add field to level", details=f"{lr.id}, {fr.id}")
            logger.debug(f"{user.name} 向 {lr.name} 等级增加了 {fr.name} 权限.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "level")

    @useroper("admin_trade")
    async def on_checked(self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.deleted:
            await context.answer("⚠️ 无法检查已经删除的物品.")
            return
        if t.status > TradeStatus.CHECKING:
            await context.answer("⚠️ 无法检查已经上架的物品.")
            return
        with db.atomic():
            t.status = TradeStatus.LAUNCHED
            t.save()
            log = Log.create(initiator=user, activity="check trade", details=str(t.id))
            log.participants.add(t.user)
            logger.debug(f'{user.name} 检查了交易 "{truncate_str(t.name, 20)}"')
            await client.send_message(
                t.user.uid, f"📢 管理员通知: 您的交易 **{t.name}** 已被管理员审核上架.", parse_mode=ParseMode.MARKDOWN
            )
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "trade_details")

    @useroper("admin_trade")
    async def on_violation(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        with db.atomic():
            t.status = TradeStatus.VIOLATION
            t.save()
            t.user.sanity -= 30
            t.user.save()
            log = Log.create(initiator=user, activity="set trade as violation", details=str(t.id))
            log.participants.add(t.user)
            logger.debug(f"{user.name} 认定了一个交易为违规.")
            await client.send_message(
                t.user.uid,
                f"📢 管理员提醒: 您出售的 **{t.name}** 的因违规被管理员锁定, 您将被扣除一定信誉.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "trade_details")

    @useroper("admin_trade")
    async def content_report_admin(
        self, handler, client: Client, context: Union[TC, TM], parameters: dict, user: User
    ):
        await context.edit_message_media(InputMediaPhoto(self._logo))
        t = Trade.get_by_id(int(parameters["trade_id"]))
        if t.status >= TradeStatus.DISPUTED:
            try:
                await context.answer("⚠️ 该交易已经处于举报解决状态.")
            except BadRequest:
                pass
            await self.to_menu(client, context, "__trade_admin")
            return
        items = []
        icons = {
            DisputeType.TRADE_NO_GOOD: "🔍",
            DisputeType.TRADE_NOT_AS_DESCRIPTION: "😞",
            DisputeType.EXCHANGE_NO_GOOD: "🔍",
            DisputeType.EXCHANGE_NOT_AS_DESCRIPTION: "😞",
            DisputeType.VIOLATION: "🚫",
        }
        typespec = {
            DisputeType.TRADE_NO_GOOD: "出售者发送虚假物品",
            DisputeType.TRADE_NOT_AS_DESCRIPTION: "出售者发送物品与描述不符",
            DisputeType.EXCHANGE_NO_GOOD: "交换者发送虚假物品",
            DisputeType.EXCHANGE_NOT_AS_DESCRIPTION: "交换者发送物品与描述不符",
            DisputeType.VIOLATION: "违规内容",
        }
        for i, dr in enumerate(t.disputes.order_by(Dispute.created).iterator()):
            if dr.description:
                spec = f"{icons[dr.type]} `{i+1}` | 举报{typespec[dr.type]}: {truncate_str(dr.description, 20)}"
            else:
                spec = f"{icons[dr.type]} `{i+1}` | <来自 __{dr.user.name}__ 的举报: {typespec[dr.type]}>"
            items.append((spec, str(i + 1), dr.id))
        if not items:
            try:
                await context.answer("⚠️ 当前没有举报")
            except BadRequest:
                pass
            await self.to_menu(client, context, "__trade_admin")
            return
        return items

    @useroper()
    async def on_report_details(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        dr = Dispute.get_by_id(int(parameters["report_details_id"]))
        msg = f"🚨 举报 {dr.type.name}\n\n"
        msg += f"交易: {truncate_str(t.name, 10)}\n   => {truncate_str(t.exchange, 10)}\n"
        if t.status in (TradeStatus.SOLD, TradeStatus.DISPUTED):
            e = (
                Exchange.select()
                .where(Exchange.status == ExchangeStatus.ACCEPTED)
                .join(Trade)
                .where(Trade.id == t.id)
                .get()
            )
            if t.user == dr.user:
                reportee = e.user
                reportee_provides = t.exchange
            else:
                reportee = t.user
                reportee_provides = t.name
            msg += f"举报人: {dr.user.name} (应收到 **{truncate_str(reportee_provides, 10)}**)\n"
            msg += f"交易等值价值: {t.coins}\n"
            msg += f"举报人信用: {dr.user.sanity} 被举报人信用: {reportee.sanity}\n"
        elif t.status in (TradeStatus.LAUNCHED, TradeStatus.VIOLATION):
            msg = f"举报人: {dr.user.name}"
        else:
            msg = "⚠️ 交易已关闭."
        if dr.photo:
            parameters["media_changed"] = True
            return InputMediaPhoto(media=dr.photo, caption=msg)
        else:
            return msg

    @useroper()
    async def on_report_accept(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        dr = Dispute.get_by_id(int(parameters["report_details_id"]))
        with db.atomic():
            if dr.type == DisputeType.VIOLATION:
                t.status = TradeStatus.VIOLATION
                dr.user.coins += dr.influence / 2 * 100
                dr.user.sanity = min(dr.user.sanity + dr.influence / 2, 100)
                t.user.sanity = max(t.user.sanity - dr.influence * 2, 0)
                await client.send_message(
                    dr.user.uid,
                    f"📢 管理员提醒: 您对 __{user_spec(t.user)}__ 出售 **{t.name}** 的违规举报被管理员审核通过, 您将被奖励一定的硬币和信誉.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await client.send_message(
                    t.user.uid,
                    f"📢 管理员提醒: 您出售的 **{t.name}** 的因违规被管理员锁定, 您将被扣除一定信誉.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                t.status = TradeStatus.DISPUTED
                e = (
                    Exchange.select()
                    .where(Exchange.status == ExchangeStatus.ACCEPTED)
                    .join(Trade)
                    .where(Trade.id == t.id)
                    .group_by(Exchange)
                    .get()
                )
                if dr.type in (DisputeType.EXCHANGE_NO_GOOD, DisputeType.EXCHANGE_NOT_AS_DESCRIPTION):
                    reporter = t.user
                    reportee = e.user
                elif dr.type in (DisputeType.TRADE_NO_GOOD, DisputeType.TRADE_NOT_AS_DESCRIPTION):
                    reporter = e.user
                    reportee = t.user
                reporter.coins += t.coins / 2
                reportee.sanity = max(reportee.sanity - dr.influence - 10, 0)
                reportee.coins -= t.coins / 2
                await client.send_message(
                    reporter.uid, f"📢 管理员提醒: 您对交易的违规举报被管理员审核通过, 您将被补偿一定的硬币.", parse_mode=ParseMode.MARKDOWN
                )
                await client.send_message(
                    reportee.uid, f"📢 管理员提醒: 您的交易存在违规被举报, 您将被扣除一定的信誉.", parse_mode=ParseMode.MARKDOWN
                )
            reporter.save()
            reportee.save()
            t.save()
            log = Log.create(initiator=user, activity="accept report", details=str(dr.id))
            log.participants.add(dr.user)
            logger.debug(f"{user.name} 确认了一个交易为违规.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "trade_details")

    @useroper()
    async def on_report_decline(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        dr = Dispute.get_by_id(int(parameters["report_details_id"]))
        with db.atomic():
            if dr.type == DisputeType.VIOLATION:
                dr_sanity_old = int(dr.user.sanity)
                dr.user.sanity = max(dr.user.sanity - dr.influence / 2, 0)
                dr.user.save()
                reportee = t.user
                msg = f"📢 管理员警告: 您对 __{user_spec(t.user)}__ 出售 **{t.name}** 的违规举报被管理员拒绝. 您已被扣除 {int(dr_sanity_old-dr.user.sanity)} 信誉. 请勿恶意举报."
                await client.send_message(dr.user.uid, msg, parse_mode=ParseMode.MARKDOWN)
            else:
                e = (
                    Exchange.select()
                    .where(Exchange.status == ExchangeStatus.ACCEPTED)
                    .join(Trade)
                    .where(Trade.id == t.id)
                    .group_by(Exchange)
                    .get()
                )
                if dr.type in (DisputeType.EXCHANGE_NO_GOOD, DisputeType.EXCHANGE_NOT_AS_DESCRIPTION):
                    reportee = e.user
                elif dr.type in (DisputeType.TRADE_NO_GOOD, DisputeType.TRADE_NOT_AS_DESCRIPTION):
                    reportee = t.user
                msg = f"📢 管理员警告: 您对交易的违规举报被管理员拒绝. 如您对此有疑问, 请再次发起举报."
                await client.send_message(dr.user.uid, msg, parse_mode=ParseMode.MARKDOWN)
            reportee.sanity = min(reportee.sanity + dr.influence, 100)
            reportee.save()
            dr.delete_instance()
            log = Log.create(initiator=user, activity="accept report", details=str(dr.id))
            log.participants.add(dr.user)
            logger.debug(f"{user.name} 否认了一个交易为违规.")
            await context.answer("✅ 成功")
            await self.to_menu(client, context, "report_admin")

    @useroper()
    async def on_trade_notify(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        e = Exchange.get_by_id(int(parameters["exchange_id"]))
        msg = "🙋‍♂️ 新的交易请求\n\n"
        msg += f"对方 ({user_spec(e.user)}) 提供了您需要的:\n**{t.exchange}**\n"
        if e.description:
            msg += f"{e.description}\n"
        else:
            msg += f"对方没有提供物品描述.\n"
        msg += f"\n您需要确认交易以查看内容, 若您点击确认交易, 您的 **{truncate_str(t.name, 10)}** 将被提供给对方.\n"
        return msg

    @useroper()
    async def on_trade_accept(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        e = Exchange.get_by_id(int(parameters["exchange_id"]))
        if t.status != TradeStatus.LAUNCHED or e.status != ExchangeStatus.LAUNCHED:
            await context.answer("⚠️ 该交易不再可用.")
            await context.message.delete()
            return
        with db.atomic():
            e.status = ExchangeStatus.ACCEPTED
            e.save()
            t.status = TradeStatus.SOLD
            t.save()
            e.user.sanity = min(e.user.sanity + max(sqrt(max(t.coins, 10)) / 20, 3), 100)
            t.user.sanity = min(t.user.sanity + max(sqrt(max(t.coins, 10)) / 5, 5), 100)
            t.user.save()
            e.user.save()
            log = Log.create(initiator=user, activity="accept exchange", details=str(e.id))
            log.participants.add(e.user)
        await self.to_menu(
            self.bot,
            menu_id="__trade_finished",
            uid=e.user.uid,
            trade_id=t.id,
            exchange_id=e.id,
            to_trade=False,
        )
        await self.to_menu(
            self.bot, context, "__trade_finished", trade_id=t.id, exchange_id=e.id, to_trade=True
        )

    @useroper()
    async def on_trade_decline(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        e = Exchange.get_by_id(int(parameters["exchange_id"]))
        if t.status != TradeStatus.LAUNCHED or e.status != ExchangeStatus.LAUNCHED:
            await context.answer("⚠️ 该交易不再可用.")
            await context.message.delete()
            return
        with db.atomic():
            e.status = ExchangeStatus.DECLINED
            e.save()
            log = Log.create(initiator=user, activity="decline exchange", details=str(e.id))
            log.participants.add(e.user)
            msg = f"😥 很遗憾, 交易 **{t.exchange}** => **{t.name}** 被对方拒绝.\n\n您的**{t.exchange}**:\n||{e.exchange}||\n依然可用."
            await self.bot.send_message(e.user.uid, msg, parse_mode=ParseMode.MARKDOWN)
            await context.answer("✅ 已拒绝.")
        await asyncio.sleep(0.5)
        await context.message.delete()

    @useroper()
    async def on_trade_blacklist(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        e = Exchange.get_by_id(int(parameters["exchange_id"]))
        if t.status != TradeStatus.LAUNCHED or e.status != ExchangeStatus.LAUNCHED:
            await self.answer("⚠️ 该交易不再可用.")
            await context.message.delete()
            return
        with db.atomic():
            e.status = ExchangeStatus.DECLINED
            e.save()
            log = Log.create(initiator=user, activity="decline exchange", details=str(e.id))
            log.participants.add(e.user)
            msg = f"😥 很遗憾, 交换 {t.exchange} => {t.name} 被对方拒绝, 同时您已被拉黑.\n\n您的物品:\n||{e.exchange}||\n依然可用."
            BlackList.create(by=t.user, of=e.user)
            await self.bot.send_message(e.user.uid, msg, parse_mode=ParseMode.MARKDOWN)
            await context.answer("✅ 已拒绝并拉黑.")
        await asyncio.sleep(0.5)
        await context.message.delete()

    @useroper()
    async def on_trade_finish(self, handler, client: Client, context: TC, parameters: dict, user: User):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        e = Exchange.get_by_id(int(parameters["exchange_id"]))
        to_trade = parameters.get("to_trade", True)
        msg = "🌈 交易完成\n\n"
        if to_trade:
            msg += f"您的交易已完成, 您已向对方提供了:\n**{t.name}**\n||{t.good}||\n\n"
            if e.coins:
                msg += f"对方向您支付了 {e.coins} 硬币.\n"
            else:
                msg += f"对方 ({user_spec(e.user)}) 提供了您需要的:\n**{t.exchange}**\n"
                if e.description:
                    msg += f"{e.description}\n"
                msg += f"||{e.exchange}||\n\n"
        else:
            if e.coins:
                msg += f"您的交易已完成, 您已向对方支付了 {e.coins} 硬币.\n"
            else:
                msg += f"您的交易已完成, 您已向对方提供了:\n**{t.exchange}**\n"
                if e.description:
                    msg += f"{e.description}\n"
                msg += f"||{e.exchange}||\n\n"
            msg += f"对方 ({user_spec(t.user)}) 提供了您需要的:\n**{t.name}**\n"
            if t.description:
                msg += f"{t.description}\n"
            msg += f"||{t.good}||\n\n"
        msg += f"请注意: 该信息将**只显示一次**, 请及时保存所需信息.\n"
        msg += f"若您对该交易有疑虑, 可以在 7 天内举报.\n"
        msg += f"欢迎您再次使用 **易物 Exchanger**!"
        return msg

    @useroper()
    async def on_trade_report(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_REPORT, **parameters)
        return "⚠️ 请输入证据描述: (图片或文字描述, 若为图片, 请将文字填写在图片的说明中)"

    @useroper()
    async def on_user_me(self, handler, client: Client, context: TC, parameters: dict, user: User):
        trades = user.trades.where(Trade.status > TradeStatus.PENDING, Trade.deleted == False).count()
        trade_sold = user.trades.where(Trade.status == TradeStatus.SOLD).count()
        exchanges = user.exchanges.count()
        exchanges_accepted = user.exchanges.where(Exchange.status == ExchangeStatus.ACCEPTED).count()
        msg = "ℹ️ 当前用户信息\n\n" + indent(
            "\n".join(
                [
                    f"ID: `{user.uid}`",
                    f"昵称: [{user.name}](tg://user?id={user.uid})",
                    f"信用: {user.sanity}",
                    f"硬币: {user.coins}",
                    f"交易成功: {trade_sold} / {trades}",
                    f"交换成功: {exchanges_accepted} / {exchanges}",
                    f"用户组: {', '.join([l.name for l in user.levels])}",
                ]
            ),
            " " * 3,
        )

        if user.restrictions.count():
            msg += "\n\n🚨 封禁\n\n"
            for r in user.restrictions.order_by(Restriction.to.desc()).iterator():
                if r.to > datetime.now():
                    msg += f"   - By {r.by.name} ({r.created.strftime('%Y-%m-%d')} to **{r.to.strftime('%Y-%m-%d')}**)\n"
                    for f in r.fields:
                        msg += f"     封禁: {f.name}\n"
                else:
                    msg += f"   - By {r.by.name} ~~({r.created.strftime('%Y-%m-%d')}, {(r.to - r.created).days} days)~~\n"
        return msg

    @useroper()
    async def on_switch_contact(self, handler, client: Client, context: TC, parameters: dict, user: User):
        if user.chat:
            user.chat = False
            await context.answer("✅ 将拒绝所有私聊.")
        else:
            user.chat = True
            await context.answer("✅ 允许与您私聊.")
        user.save()
        await self.to_menu(client, context, "user_me")

    @useroper()
    async def on_switch_anonymous(self, handler, client: Client, context: TC, parameters: dict, user: User):
        if user.anonymous:
            user.anonymous = False
            await context.answer("✅ 关闭匿名模式.")
        else:
            user.anonymous = True
            await context.answer("✅ 开启匿名模式.")
        user.save()
        await self.to_menu(client, context, "user_me")

    @useroper()
    async def content_trade_exchange_list(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        t = Trade.get_by_id(int(parameters["trade_id"]))
        items = []
        for i, er in enumerate(
            Exchange.select().join(Trade).where(Trade.id == t.id).order_by(Exchange.status).iterator()
        ):
            if er.description:
                spec = f"`{i+1: >3}` | {truncate_str(er.description, 20)}"
            else:
                spec = f"`{i+1: >3}` | <来自 __{user_spec(er.user)}__ 的请求>"
            if er.status == ExchangeStatus.DECLINED:
                spec = f"~~{spec}~~"
            items.append((spec, str(i + 1), er.id))
        if not items:
            await context.answer("⚠️ 当前没有该商品的交换请求.")
            return
        return items

    @useroper()
    async def on_trade_exchange(self, handler, client: Client, context: TC, parameters: dict, user: User):
        e = Exchange.get_by_id(int(parameters["trade_exchange_id"]))
        if e.status == ExchangeStatus.DECLINED:
            await context.answer(f"⚠️ 来自 {user_spec(e.user)} 的请求已关闭.")
            return
        await self.to_menu(
            client,
            menu_id="__trade_notify",
            uid=user.uid,
            trade_id=int(parameters["trade_id"]),
            exchange_id=e.id,
        )
        await context.answer()
