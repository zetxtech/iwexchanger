from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum, auto
from functools import cached_property, partial
import hashlib
from itertools import chain
import random
import re
import string
from textwrap import indent
from threading import Thread
from typing import Dict, Union
import asyncio
import uuid

from loguru import logger
from pyrogram import filters, Client, ContinuePropagation
from pyrogram.handlers import MessageHandler
from pyrogram.types import BotCommand, Message as TM, CallbackQuery as TC, User as TU
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import BadRequest
from thefuzz import process
from dateutil import parser

from .. import __name__
from ..services.github import create_invite_repo, remove_repo
from ..model import (
    Instance,
    db,
    User,
    UserRole,
    InviteCode,
    Message,
    InviteCodeStatus,
    MessageSettings,
    Auth,
    Invite,
    EmbyCode,
    MessageLevel,
    Repo,
)
from ..utils import async_partial, remove_prefix, flatten2, truncate_str_reverse
from ..bot import Bot

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

logger = logger.bind(scheme="control")

EC = "‎"


class ConversationStatus(Enum):
    WAITING_INVITE_CODE = auto()
    WAITING_USER = auto()
    WAITING_EMBYCODE_SITE = auto()
    WAITING_EMBYCODE = auto()
    WAITING_MESSAGE = auto()
    WAITING_GITHUB_USERNAME = auto()
    WAITING_GITHUB_USERNAME_CREATE = auto()
    WAITING_MESSAGE_TIME = auto()


@dataclass
class Conversation:
    context: Union[TM, TC]
    status: ConversationStatus


class ControlBot(Bot):
    username = "embykeeper-bot"

    def __init__(self, *args, github=None, **kw):
        super().__init__(*args, **kw)
        self.github = github

        self._user_conversion: Dict[int, Conversation] = {}
        self._reload = asyncio.Event()

    async def setup(self):
        self.bot.add_handler(MessageHandler(self.text_handler, filters.text), group=100)
        self.menu = ParameterizedHandler(self.tree, DictDatabase())
        self.menu.setup(self.bot, group=100)
        await self.bot.set_bot_commands([BotCommand("start", "开始使用"), BotCommand("admin", "管理工具")])
        logger.info(f"已启动监听: {self.bot.me.username}.")

    def next_message_settings(self):
        MS = MessageSettings
        now = datetime.now().time()
        s1 = MS.select().where(MS.time >= now).order_by(MS.time)
        s2 = MS.select().where(MS.time < now).order_by(MS.time)
        for ms in chain(s1.iterator(), s2.iterator()):
            yield ms

    @staticmethod
    def parse_message(message: Message, prefix=" ", length=15):
        if message.level == MessageLevel.ADMIN:
            icon = "📣"
        elif message.level > MessageLevel.WARNING:
            icon = "⚠️"
        else:
            icon = "ℹ️"
        content = truncate_str_reverse(message.content, length)
        return f"{icon}{prefix}`{content}`"

    async def watch(self):
        while True:
            await asyncio.sleep(10)
            for ms in self.next_message_settings():
                now = datetime.now()
                next_time = datetime.combine(now.date(), ms.time)
                delta = next_time - now
                if delta.days < 0:
                    delta = timedelta(days=0, seconds=delta.seconds)
                delta_sec = int(delta.total_seconds())
                if delta_sec > 180:
                    logger.debug(f"等待 {delta_sec} 秒以进行下一次推送.")
                for _ in range(delta_sec):
                    if self._reload.is_set():
                        self._reload.clear()
                        break
                    else:
                        await asyncio.sleep(1)
                else:
                    if not ms.enabled:
                        continue
                    if not ms.user.role >= UserRole.MEMBER:
                        continue
                    with db.atomic():
                        messages = (
                            Message.select()
                            .where(Message.read == False)
                            .join(User)
                            .where(User.id == ms.user.id)
                            .order_by(Message.time)
                        )
                        mids = []
                        for m in messages.iterator():
                            mids.append(m.id)
                            m.read = True
                            m.save()
                        if mids:
                            await self.to_menu(
                                self.bot,
                                menu_id="__daily_message_list",
                                uid=ms.user.uid,
                                messages_id=mids,
                            )
                    continue
                break

    @cached_property
    def tree(self):
        ms = lambda **kw: {"parse_mode": ParseMode.MARKDOWN, "style": MenuStyle(back_text="◀️ 返回", **kw)}
        ps = lambda **kw: {
            "parse_mode": ParseMode.MARKDOWN,
            "style": PageStyle(back_text="◀️ 返回", previous_page_text="⬅️", next_page_text="➡️", **kw),
        }
        DMenu = partial(Menu, **ms())
        return transform(
            {
                DMenu("Start", "start", self.on_start, default=True): {
                    DMenu("👤 用户信息", "user_info", self.on_user_info): {
                        DMenu(
                            "✉️ 新建邀请码",
                            "new_invite_code",
                            self.on_new_invite_code,
                            disable_web_page_preview=True,
                        ),
                        DMenu("🔑 输入邀请码", "enter_invite_code", self.on_enter_invite_code),
                    },
                    DMenu("💬 消息推送", "message_info", self.on_message_info): {
                        DMenu("🔇 开关推送", "toggle_message", self.on_toggle_message): None,
                        PageMenu(
                            "🕒 更改时间",
                            "change_message_time",
                            "将在每日的几点推送?",
                            self.items_message_time,
                            **ps(limit=4, limit_items=12),
                        ): {
                            Menu(
                                "接受小时", "cmt_hour", self.on_change_message_time, **ms(back_to="message_info")
                            )
                        },
                        ContentPageMenu(
                            "🕰️ 历史消息",
                            "message_list",
                            self.content_message_list,
                            header="👇 请点击序号查看详情",
                            **ps(limit=5, limit_items=5),
                        ): {DMenu("消息详情", "message", self.on_message_details)},
                    },
                    DMenu("👑 高级用户", "prime_info", self.on_prime_info, disable_web_page_preview=True): {
                        DMenu("💌 分享邀请码", "pi_embycode", self.on_share_embycode_site),
                        DMenu("⌨️ Github", "pi_github", self.on_request_github_prime),
                        LinkMenu("💡 爱发电赞助", "pi_afd", "https://afdian.net/a/jackzzs"),
                        DMenu("✉️ 输入邀请码", "pi_invite", self.on_prime_invite),
                    },
                },
                DMenu("Admin", "admin", self.on_admin): {
                    DMenu("👤 用户管理", "user_admin", self.on_user_admin): {
                        PageMenu(
                            "👑 管理员邀请",
                            "new_admin_invite_code",
                            "👇🏼 请选择权限:",
                            self.items_naic_role,
                            **ps(limit=2, limit_items=5, back_to="user_admin"),
                        ): {
                            PageMenu(
                                "接收权限",
                                "naic_role",
                                "👇🏼 请选择数量:",
                                [Element(str(h), str(h)) for h in [1, 5, 10, 20, 50]],
                                **ps(limit=5, limit_items=5, back_to="user_admin"),
                            ): {
                                PageMenu(
                                    "接收数量",
                                    "naic_pcs",
                                    "👇🏼 请选择可用次数:",
                                    [Element(str(h), str(h)) for h in [1, 5, 10, 50]] + [Element("无限", "-1")],
                                    **ps(limit=4, limit_items=5, back_to="user_admin"),
                                ): {
                                    PageMenu(
                                        "接收可用次数",
                                        "naic_slots",
                                        "👇🏼 请选择有效天数:",
                                        [Element(str(h), str(h)) for h in [1, 3, 7, 14, 30, 180]]
                                        + [Element("无限", "-1")],
                                        **ps(limit=3, limit_items=7, back_to="user_admin"),
                                    ): {
                                        Menu(
                                            "接收天数",
                                            "naic_days",
                                            self.on_new_admin_invite_code,
                                            **ms(back_to="user_admin"),
                                        )
                                    }
                                }
                            }
                        },
                        ContentPageMenu(
                            "✉️ 邀请码管理",
                            "invite_code_admin",
                            self.content_invite_code_admin,
                            preliminary=self.before_invite_code_admin,
                            **ps(limit=5, limit_items=10, extras=["__invite_code_all_delete"]),
                        ): {
                            DMenu("邀请码详情", "invite_code", self.on_invite_code_details): {
                                DMenu(
                                    "🗑️ 删除邀请码",
                                    "invite_code_delete",
                                    self.on_invite_code_delete,
                                    **ms(back_to="invite_code_admin"),
                                ),
                            }
                        },
                        ContentPageMenu(
                            "👥 列出用户",
                            "list_users",
                            self.content_users_list,
                            header="👇 请按序号选择您需要查询的用户信息:\n",
                            **ps(limit=5, limit_items=10, extras=["__users_message"]),
                        ): {
                            DMenu("用户详情", "user", self.on_user_details, disable_web_page_preview=True): {
                                PageMenu(
                                    "👑 调整权限",
                                    "user_role_config",
                                    "👇🏼 请选择权限:",
                                    self.items_urc_role,
                                    **ps(limit=2, limit_items=5),
                                ): {Menu("接收权限", "urc_role", self.on_user_role_config, **ms(back_to="user"))},
                                DMenu("✉️ 发送消息", "user_message", self.on_user_message): None,
                                DMenu("🈲 踢出用户", "user_kick", self.on_user_kick): None,
                            },
                        },
                    },
                    DMenu("👑 高级用户", "prime_admin", self.on_prime_admin): {
                        ContentPageMenu(
                            "💌 查看收到的验证码",
                            "list_embycodes",
                            self.content_list_embycodes,
                            header="👇 请按序号选择您需要查询的邀请码:\n",
                            **ps(limit=5, limit_items=5, extras=["__embycode_show_all"]),
                        ): {
                            DMenu(
                                "💌 验证码详情:",
                                "embycode",
                                self.on_embycode_details,
                                disable_web_page_preview=True,
                            ): {
                                DMenu("🈲 踢出用户", "embycode_user_kick", self.on_embycode_user_kick),
                                DMenu("✅ 标为已用", "embycode_use", self.on_embycode_use),
                            }
                        },
                        DMenu("⌨️ 新建密钥仓库", "new_invite_repo", self.on_new_invite_repo): None,
                    },
                    DMenu("ℹ️ 系统信息", "sys_admin", self.on_sys_admin): None,
                },
                Menu("🗑️ 清空全部", "__invite_code_all_delete", "❓ 您确定要清空全部邀请码?", **ms(back_to="user_admin")): {
                    Menu("❗ 确定", "icad_confirm", self.on_invite_code_all_delete, **ms(back_to="user_admin"))
                },
                Menu("👁️‍🗨️ 显示全部", "__embycode_show_all", self.on_embycode_show_all): None,
                Menu("✉️ 向所有人发信", "__users_message", self.on_user_message): None,
                ContentPageMenu(
                    "🌥️ 今日消息",
                    "__daily_message_list",
                    self.content_message_list,
                    header="🌥️ 今日日志",
                    **ps(limit=5, limit_items=5),
                ): {DMenu("日志详情", "__message", self.on_message_details)},
            }
        )

    def set_conversation(self, context: Union[TM, TC], user: User, status: ConversationStatus = None):
        message = context.message if isinstance(context, TC) else context
        self._user_conversion[(message.chat.id, user.uid)] = Conversation(context, status) if status else None

    async def to_menu(self, client, context=None, menu_id="start", uid=None, **kw):
        if not context:
            if not uid:
                raise ValueError("uid must be provided for context constructing")
            message = await self.bot.send_message(uid, "🔄 正在加载")
            user = await self.bot.get_users(uid)
            hash = hashlib.sha1(f"{uid}_{datetime.now().timestamp()}".encode())
            cid = str(int(hash.hexdigest(), 16) % (10**8))
            context = TC(client=self.bot, id=cid, from_user=user, message=message, chat_instance=None)
        if isinstance(context, TC):
            params = getattr(context, "parameters", {})
            params.update(kw)
        else:
            params = kw
        await self.menu[menu_id].on_update(self.menu, client, context, params)

    async def use_code(self, code: str, user: User):
        code: InviteCode = (
            InviteCode.select()
            .where(
                InviteCode.code == code.upper(),
                InviteCode.status == InviteCodeStatus.OK,
                InviteCode.timeout > datetime.now(),
            )
            .get_or_none()
        )
        if code:
            invited = user.invited_by.where(Invite.to == user.role).count()
            if code.role <= user.role and invited:
                return "🚫 邀请码用户组不高于当前用户组."
            if user.role <= UserRole.BANNED:
                return "🚫 被封禁用户无法使用邀请码."
            if code.created_by.id == user.id:
                return "🚫 不能邀请自己."
            with db.atomic() as txn:
                try:
                    Invite.create(by=code.created_by, of=user, to=code.role)
                    user.role = code.role
                    if code.slots > 0:
                        code.slots -= 1
                        if code.slots == 0:
                            code.status = InviteCodeStatus.USED
                    code.save()
                    user.save()
                except:
                    txn.rollback()
                    logger.exception("使用邀请码时出现错误, 已回滚.")
                    return "⚠️ 发生错误."
                else:
                    Invitee = User.alias()
                    invitor = code.created_by
                    invite_count = invitor.invites.join(Invitee, on="of").group_by(Invitee).count()
                    if invite_count >= 3:
                        if invitor.role < UserRole.PRIME:
                            invitor.role == UserRole.PRIME
                    if invite_count >= 6:
                        if invitor.role < UserRole.SUPER:
                            invitor.role == UserRole.SUPER
                    repo = code.repos.get_or_none()
                    if repo:
                        t = Thread(target=remove_repo, args=(self.github, repo.name))
                        t.daemon = True
                        t.start()
                        repo.delete_instance()
                        logger.info(f"邀请码被使用, 后台启动私密 Repo 删除: {repo.name}.")
                        return "👌 欢迎您开发者, 已成功使用邀请码."
                    else:
                        return f"👌 成功使用该邀请码, 您已成为 {user.role.name}."
        else:
            return "🚫 邀请码无效."

    async def fetch_user(self, u, code=None):
        if isinstance(u, TU):
            user = u
            uid = u.id
        else:
            user = await self.bot.get_users(u)
            uid = u
        ur: User
        with db.atomic():
            ur, created = User.get_or_create(uid=uid)
            if created:
                user_info = f"uid = {uid}"
                if user.username:
                    user_info = f"{user.username}, {user_info}"
                MessageSettings.create(user=ur)
                logger.info(f"新用户: {user.name} [gray50]({user_info})[/].")
                self._reload.set()
        if ur.role != UserRole.CREATOR:
            if User.select().where(User.role == UserRole.CREATOR).count() == 0:
                with db.atomic():
                    Invite.create(by=ur, of=ur, to=UserRole.CREATOR)
                    ur.role = UserRole.CREATOR
                    ur.save()
                logger.info(f"[red]用户 {user.name} 已被设为 {ur.role.name}[/].")
        elif created and code:
            await self.use_code(code, ur, force=True)
        return ur, created

    def useroper(perm: UserRole = UserRole.MEMBER, conversation=False, group=False):
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
                        await context.answer(m, show_alert=True)

                if (
                    isinstance(context, TM)
                    and context.chat.type not in (ChatType.BOT, ChatType.PRIVATE)
                    and not group
                ):
                    return

                try:
                    sender = context.from_user
                    user, _ = await self.fetch_user(sender)
                    if perm and perm > user.role:
                        return await error("⛔ 您没有权限执行此命令.")
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

    @useroper(None, conversation=True)
    async def text_handler(self, client: Client, message: TM, user: User):
        if message.text.startswith("/"):
            message.continue_propagation()
        conv = self._user_conversion.get((message.chat.id, user.uid), None)
        if not conv:
            message.continue_propagation()
        if conv.status == ConversationStatus.WAITING_INVITE_CODE:
            ret = await self.use_code(message.text, user)
            await message.reply(ret)
        elif conv.status == ConversationStatus.WAITING_USER:
            user_id = message.text
            try:
                u = await client.get_users(user_id)
                if User.get_or_none(uid=u.id):
                    return await self.to_menu(client, message, "user", user_id=u.id)
            except BadRequest:
                pass
            if User.select().count() > 100:
                await message.reply("⌛ 正在搜索用户昵称, 可能需要一定时间.")
            uids = [u.uid for u in User.select().iterator()]
            uns = {u.id: f"{u.name}" for u in await client.get_users(uids)}
            results = process.extract(user_id, uns, limit=5)
            uids = [uid for _, score, uid in results if score > 75]
            if len(uids) > 1:
                return await self.to_menu(client, message, "list_users", user_ids=uids)
            elif len(uids) == 1:
                return await self.to_menu(client, message, "user", user_id=uids[0])
            else:
                await message.reply("⚠️ 未找到该用户.")
        elif conv.status == ConversationStatus.WAITING_EMBYCODE_SITE:
            conv.context.parameters["embycode_site"] = message.text
            self.self.set_conversation(user, conv.context, ConversationStatus.WAITING_EMBYCODE)
            await message.reply("💌 请输入邀请码:")
        elif conv.status == ConversationStatus.WAITING_EMBYCODE:
            site = str(conv.context.parameters["embycode_site"])
            code = message.text
            if user.role < UserRole.SUPER:
                with db.atomic():
                    EmbyCode.create(site=site, code=code, user=user)
                    if user.role == UserRole.PRIME:
                        user.role = UserRole.SUPER
                    else:
                        user.role = UserRole.PRIME
                    user.save()
                await message.reply(f"👑 成功! 您已成为 {user.role.name} 用户.")
            else:
                await message.reply(f"⚠️ 您已经是超级用户.")
        elif conv.status == ConversationStatus.WAITING_MESSAGE:
            uid = conv.context.parameters.get("user_id", None)
            uids = conv.context.parameters.get("user_ids", [])
            cond = conv.context.parameters.get("cond", None)
            if uid:
                urs = [User.get(uid=uid)]
            elif uids:
                urs = User.select().where(User.uid.in_(uids)).iterator()
            elif cond:
                urs = User.select().where(cond).iterator()
            else:
                urs = User.select().iterator()
            for i, ur in enumerate(urs):
                if user:
                    instance, _ = Instance.get_or_create(uuid=uuid.UUID(int=0))
                    Message.create(content=message.text, level=MessageLevel.ADMIN, instance=instance, user=ur)
            if i == 0:
                await message.reply(f"✅ 已发送.")
            else:
                await message.reply(f"✅ 已发送给 {i+1} 个用户.")
        elif conv.status == ConversationStatus.WAITING_GITHUB_USERNAME:
            instance, _ = Instance.get_or_create(uuid=uuid.UUID(int=0))
            creator = User.select().where(User.role == UserRole.CREATOR).get()
            spec = (
                f"[{user.name}](tg://user?id={user.id}) ([管理](t.me/{client.me.username}?start=__u_{user.id}))"
            )
            Message.create(
                content=f"{spec} 申请了开发者高级用户权限: [{message.text}](https://github.com/{message.text})",
                level=MessageLevel.ADMIN,
                instance=instance,
                user=creator,
            )
            await message.reply(f"✅ 申请成功, 稍后我们将核实并给予高级用户, 并通过系统消息通知, 感谢您的贡献!")
        elif conv.status == ConversationStatus.WAITING_GITHUB_USERNAME_CREATE:
            if user.role < UserRole.CREATOR:
                return await message.reply(f"⚠️ 只有 {UserRole.CREATOR.name} 可以新建开发者账户.")
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
            coder = InviteCode.create(code=code, role=UserRole.PRIME, created_by=user)
            spec = re.sub("[^A-Za-z0-9]+", "-", message.text).lower()
            name = f"welcome-{spec}"
            Repo.create(name=name, code=coder)
            t = Thread(target=create_invite_repo, args=(self.github, name, message.text, code))
            t.daemon = True
            t.start()
            logger.info(f"后台启动私密 Repo 建立: {name}.")
            await message.reply(f"✅ 已在后台开始进行私密 Repo 建立.")
        elif conv.status == ConversationStatus.WAITING_MESSAGE_TIME:
            try:
                time = parser.parse(message.text).time()
                self.change_message_time(user, time)
                self._reload.set()
                await message.reply(f'ℹ️ 消息将在每日 {time.strftime("%I:%M %p")} 推送.')
            except parser.ParserError:
                await message.reply(f"⚠️ 无效的时间.")
        else:
            message.continue_propagation()

    @useroper(None)
    async def on_start(self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User):
        if isinstance(context, TM):
            if not context.text:
                return None
            cmds = context.text.split()
            if len(cmds) == 2:
                if cmds[1] == "__prime":
                    return await self.to_menu(client, context, "prime_info")
                elif cmds[1].startswith("__u_"):
                    return await self.to_menu(client, context, "user", user_id=remove_prefix(cmds[1], "__u_"))
                elif cmds[1].startswith("__i_"):
                    code = remove_prefix(cmds[1], "__i_")
                    await context.reply(await self.use_code(code, user))
        return (
            f"🌈 您好 {context.from_user.name}, 欢迎使用 **[Embykeeper](https://github.com/embykeeper/embykeeper)**!"
        )

    @useroper(None)
    async def on_user_info(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        Inviter = User.alias()
        invites_q = (
            User.select()
            .where(User.role >= UserRole.MEMBER)
            .join(Invite, on=Invite.of)
            .join(Inviter, on=Invite.by)
            .where(Inviter.id == user.id)
            .group_by(User)
        )
        invite_codes_q = user.invite_codes.where(
            InviteCode.status == InviteCodeStatus.OK, InviteCode.timeout > datetime.now()
        )
        msg = f"ℹ️ 您好 {context.from_user.name}, 当前用户信息为:\n\n" + indent(
            "\n".join(
                [
                    f"用户 ID: `{user.uid}`",
                    f"等级状态: {user.role.name}",
                    f"邀请码数: {invite_codes_q.count()}",
                    f"邀请人数: {invites_q.count()}",
                    f"注册时间: {user.created.strftime('%Y-%m-%d')}",
                ]
            ),
            " " * 3,
        )

        return msg

    @useroper()
    async def on_new_invite_code(self, handler, client: Client, context: TC, parameters: dict, user: User):
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        InviteCode.create(code=code, created_by=user, slots=-1)
        return f"🔑 您的邀请码为: `{code}`\n(`t.me/{client.me.username}?start=__i_{code}`)."

    @useroper()
    async def on_enter_invite_code(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_INVITE_CODE)
        return "👉🏼 请输入邀请码:"

    @useroper()
    async def on_message_info(self, handler, client: Client, context: TC, parameters: dict, user: User):
        settings, _ = MessageSettings.get_or_create(user=user)
        if settings.enabled:
            return f' 🔌 启用消息: 是\n\n 🕒 每日提醒时间: {settings.time.strftime("%I:%M %p")}'
        else:
            return f" 🔌 启用消息: 否"

    def change_message_time(self, user, time):
        settings, _ = MessageSettings.get_or_create(user=user)
        settings.time = time
        settings.save()

    @useroper()
    async def on_change_message_time(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        mhour = int(parameters["cmt_hour_id"])
        mtime = time(mhour, 0)
        self.change_message_time(user, mtime)
        self._reload.set()
        return f'ℹ️ 消息将在每日 {mtime.strftime("%I:%M %p")} 推送.'

    @useroper()
    async def on_toggle_message(self, handler, client: Client, context: TC, parameters: dict, user: User):
        settings: MessageSettings = user.message_settings.get()
        settings.enabled = not settings.enabled
        settings.save()
        await context.answer(f'ℹ️ 消息推送已{"开启" if settings.enabled else "关闭"}.')
        await self.to_menu(client, context, "message_info")

    @useroper()
    async def content_message_list(self, handler, client: Client, context: TC, parameters: dict, user: User):
        mids = parameters.get("messages_id", [])
        if mids:
            ms = Message.select().where(Message.id.in_(mids))
        else:
            ms = Message.select().join(User).where(User.id == user.id).order_by(Message.time.desc())
        items = [
            (f"{self.parse_message(m, prefix=f' {i+1}|', length=30)}", str(i + 1), m.id)
            for i, m in enumerate(ms)
        ]
        if not items:
            await context.answer(f"⚠️ 没有查询到消息!")
        return items

    @useroper()
    async def on_message_details(self, handler, client: Client, context: TC, parameters: dict, user: User):
        mid = int(parameters.get("message_id", None) or parameters.get("__message_id", None))
        m = Message.get_by_id(mid)
        return f"📢 {m.level.name} 消息 (`{m.time.strftime('%Y-%m-%d %H:%M:%S')}`)\n\n{m.content}"

    @useroper(UserRole.ADMIN)
    async def on_admin(self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User):
        return f"👇 您好管理员 {context.from_user.name}, 请选择管理指令"

    @useroper(UserRole.ADMIN)
    async def on_sys_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        latest_user_r = User.select().order_by(User.created.desc()).get()
        try:
            latest_user = await client.get_users(latest_user_r.uid)
        except BadRequest:
            spec = "<未知>"
        else:
            spec = f"[{latest_user.name}](tg://user?id={latest_user.id})"
        valid_invite_q = InviteCode.select().where(
            InviteCode.status == InviteCodeStatus.OK, InviteCode.timeout > datetime.now()
        )
        latest_auth_c = Auth.time > datetime.now() - timedelta(days=30)
        msg = f"⭐ 当前系统信息:\n\n" + indent(
            "\n".join(
                [
                    f"有效用户: {User.select().where(User.role>=UserRole.MEMBER).count()}",
                    f"最新用户: {spec}",
                    f"有效邀请码: {valid_invite_q.count()}",
                    f"30日认证: {Auth.select().where(latest_auth_c).count()}",
                    f"30日认证用户: {User.select().join(Auth).where(latest_auth_c).group_by(User).count()}",
                    f"缓存消息: {Message.select().count()}",
                ]
            ),
            " " * 3,
        )
        return msg

    @useroper(UserRole.ADMIN)
    async def on_user_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_USER)
        return "👇🏼 请选择指令\n\n👉🏼 或输入用户以搜索"

    @useroper(UserRole.ADMIN)
    async def content_users_list(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        async def lazy_item(i, ur, *args):
            try:
                u = await client.get_users(ur.uid)
            except (KeyError, BadRequest):
                uname = "<未知>"
            else:
                uname = f"[{u.name}](tg://user?id={u.id})"
            return f"`{i+1: >3}` | {uname} ({ur.role.name})"

        parameters.pop("user_id", None)
        user_ids = parameters.get("user_ids", None)
        cond = parameters.get("cond", None)
        if user_ids:
            urs = User.select().where(User.uid.in_(user_ids))
        elif cond:
            urs = User.select().where(cond)
        else:
            urs = User.select()
        urs = urs.order_by(User.role.desc())
        items = []
        for i, ur in enumerate(urs.iterator()):
            items.append((async_partial(lazy_item, i, ur), str(i + 1), ur.uid))
        if not items:
            await context.answer("⚠️ 当前没有用户!")
        return items

    @useroper(UserRole.ADMIN)
    async def on_user_details(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        uid = int(parameters["user_id"])
        try:
            u = await client.get_users(uid)
        except BadRequest:
            spec = "<未知>"
        else:
            spec = f"[{u.name}](tg://user?id={u.id})"
        ur = User.get_or_none(uid=uid)
        msg = "ℹ️ 用户信息如下\n\n" + indent(
            "\n".join(
                [
                    f"用户 ID: `{uid}`",
                    f"用户昵称: {spec}",
                    f"角色状态: {ur.role.name if ur else '未注册'}",
                ]
            ),
            " " * 3,
        )

        if ur:
            Inviter = User.alias()
            invites_q = (
                User.select()
                .where(User.role >= UserRole.MEMBER)
                .join(Invite, on=Invite.of)
                .join(Inviter, on=Invite.by)
                .where(Inviter.id == user.id)
                .group_by(User)
            )
            invited_by_items = []
            for ir in Invite.select().where(Invite.of == ur).iterator():
                try:
                    i = await client.get_users(ir.by.uid)
                except BadRequest:
                    spec = "<未知>"
                else:
                    spec = (
                        f"[{i.name}](tg://user?id={i.id}) ([管理](t.me/{client.me.username}?start=__u_{i.id}))"
                    )
                invited_by_items.append(f"{spec} => {ir.to.name}")
            invite_codes_q = ur.invite_codes.where(
                InviteCode.status == InviteCodeStatus.OK, InviteCode.timeout > datetime.now()
            )
            last_auth = ur.auths.order_by(Auth.time.desc()).get_or_none()
            last_auth_time = last_auth.time.strftime("%Y-%m-%d") if last_auth else "无"
            msg += "\n" + indent(
                "\n".join(
                    [
                        f"邀请码数: {invite_codes_q.count()}",
                        f"邀请人数: {invites_q.count()}",
                        f"注册时间: {ur.created.strftime('%Y-%m-%d')}",
                        f"认证次数: {ur.auths.count()}",
                        f"上次认证: {last_auth_time}",
                        "邀请人:" if invited_by_items else "邀请人: 无",
                        indent("\n".join(invited_by_items), " " * 4),
                    ]
                ),
                " " * 3,
            )
        return msg

    @useroper(UserRole.ADMIN)
    async def on_user_kick(self, handler, client: Client, context: TC, parameters: dict, user: User):
        uid = int(parameters["user_id"])
        ur: User = User.get_or_none(uid=uid)
        if not ur:
            return "⚠️ 用户未注册."
        elif ur.role == UserRole.BANNED:
            ur.role = UserRole.MEMBER
            ur.save()
            logger.info(f"{user.uid} 已将 {ur.uid} 恢复.")
            return f"✅ 用户已被恢复为 {UserRole.MEMBER.name}."
        elif ur.role >= UserRole.ADMIN:
            return "⚠️ 不能踢出管理员, 请先调整权限."
        else:
            ur.role = UserRole.BANNED
            ur.save()
            logger.info(f"{user.uid} 已将 {ur.uid} 踢出.")
            return f"✅ 用户已被踢出."

    @useroper(UserRole.ADMIN)
    async def items_urc_role(self, handler, client: Client, context: TC, parameters: dict, user: User):
        return [Element(r.name, str(r.value)) for r in UserRole if r < UserRole.CREATOR and r < user.role]

    @useroper(UserRole.ADMIN)
    async def on_user_role_config(self, handler, client: Client, context: TC, parameters: dict, user: User):
        uid = int(parameters["user_id"])
        role = UserRole(int(parameters["urc_role_id"]))
        if role > user.role:
            return f"⚠️ 权限不足, 无法调整至 {role.name}."
        ur: User = User.get_or_none(uid=uid)
        if not ur:
            return "⚠️ 用户未注册."
        elif ur.id == user.id:
            return "⚠️ 无法调整自己的权限."
        elif ur.role == UserRole.DELETED and user.role < UserRole.SUPERADMIN:
            return f"⚠️ 修改 {UserRole.DELETED.name} 用户的权限需要您具有 {UserRole.SUPERADMIN.name} 权限."
        else:
            with db.atomic():
                Invite.create(by=user, of=ur, to=role)
                ur.role = role
                ur.save()
            logger.info(f"{user.uid} 已将 {ur.uid} 调整为 {role.name}.")
            return f"✅ 用户已被调整为 {role.name}."

    @useroper(UserRole.ADMIN)
    async def items_naic_role(self, handler, client: Client, context: TC, parameters, user: User):
        return [
            Element(r.name, str(r.value))
            for r in UserRole
            if UserRole.MEMBER <= r < UserRole.CREATOR and r < user.role
        ]

    @useroper(UserRole.ADMIN)
    async def on_new_admin_invite_code(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        role = UserRole(int(parameters["naic_role_id"]))
        if role > user.role:
            return f"⚠️ 权限不足, 无法创建 {role.name} 的邀请码."
        pcs = int(parameters["naic_pcs_id"])
        slots = int(parameters["naic_slots_id"])
        days = int(parameters["naic_days_id"])
        if days < 0:
            timeout = datetime(9999, 12, 31)
        else:
            timeout = datetime.now() + timedelta(days=days)
        codes = []
        with db.atomic():
            for _ in range(pcs):
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=12))
                InviteCode.create(code=code, role=role, slots=slots, timeout=timeout, created_by=user)
                codes.append(f"`{code}`")
        logger.info(f"{user.uid} 已生成 {role.name} 的邀请码 {pcs} 个.")
        codes = "\n".join(codes)
        return f"🔑 已创建邀请码:\n{codes}"

    @useroper(UserRole.ADMIN)
    async def content_invite_code_admin(self, handler, client: Client, context: TC, parameters, user: User):
        irs = (
            InviteCode.select()
            .where(InviteCode.status == InviteCodeStatus.OK, InviteCode.timeout > datetime.now())
            .order_by(InviteCode.role.desc())
        )
        items = []
        for i, ir in enumerate(irs.iterator()):
            spec = f"`{ir.code}`"
            if ir.role > UserRole.MEMBER:
                spec += f" ({ir.role.name})"
            items.append((f"`{i+1: >3}` | " + spec, str(i + 1), str(ir.id)))
        if not items:
            await context.answer("⚠️ 当前没有有效的验证码!")
        return items

    @useroper(UserRole.SUPERADMIN)
    async def on_invite_code_all_delete(self, handler, client: Client, context: TC, parameters, user: User):
        with db.atomic():
            for i in InviteCode.select().iterator():
                i.status = InviteCodeStatus.DELETED
                i.save()
        logger.info(f"{user.uid} 已清空邀请码.")
        return "✅ 已清空邀请码."

    async def before_invite_code_admin(self, menu: Menu, handler, client: Client, context: TM, parameters):
        menu.header = f"📜 当前共有{len(flatten2(menu.entries))}个有效邀请码:\n"

    @useroper(UserRole.ADMIN)
    async def on_invite_code_details(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        iid = int(parameters["invite_code_id"])
        ir = InviteCode.get_by_id(iid)
        try:
            created_by = await client.get_users(ir.created_by.uid)
        except BadRequest:
            spec = "<未知>"
        else:
            spec = f"[{created_by.name}](tg://user?id={created_by.id})"
        return f'ℹ️ 邀请码 "`{ir.code}`" 信息如下:\n\n' + indent(
            "\n".join(
                [
                    f"创建者: {spec}",
                    f"目标角色: {ir.role.name}",
                    f"剩余额度: {ir.slots}",
                    f"剩余时间: {(ir.timeout - datetime.now()).days} 天",
                ]
            ),
            " " * 3,
        )

    @useroper(UserRole.ADMIN)
    async def on_invite_code_delete(self, handler, client: Client, context: TC, parameters: dict, user: User):
        iid = int(parameters["invite_code_id"])
        ir = InviteCode.get_by_id(iid)
        ir.status = InviteCodeStatus.DELETED
        ir.save()
        return "✅ 成功删除邀请码."

    @useroper()
    async def on_prime_info(
        self, handler, client: Client, context: Union[TM, TC], parameters: dict, user: User
    ):
        lines = []
        if user.role == UserRole.MEMBER:
            lines += [
                "👑 您可以通过以下方式成为高级用户:\n",
            ]
        if user.role == UserRole.PRIME:
            lines += ["👑 欢迎您高级用户, 您还可以继续升级为超级用户:\n"]
        if user.role < UserRole.SUPER:
            lines += [
                "  1. 通过爱发电赞助 (5元给开发者买个小包子).",
                "  2. 分享任意一个邀请制 Emby 站点的邀请码.",
                "  3. 在 [Github](https://github.com/embykeeper/embykeeper) 提交Bug修复或新功能.",
                "  4. 分享邀请链接给三位用户.",
            ]
        else:
            lines += ["👑 欢迎您超级用户, 您已经是最高等级!"]
        return "\n".join(lines)

    @useroper()
    async def on_share_embycode_site(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        self.set_conversation(user, context, ConversationStatus.WAITING_EMBYCODE_SITE)
        return "💌 请输入 Emby 邀请码的站点名称:"

    @useroper(UserRole.ADMIN)
    async def on_prime_admin(self, handler, client: Client, context: TC, parameters: dict, user: User):
        return "ℹ️ 高级用户信息如下\n\n" + indent(
            "\n".join(
                [
                    f"高级用户数: `{User.select().where(User.role>=UserRole.PRIME).count()}`",
                    f"可用邀请码: `{EmbyCode.select().where(EmbyCode.used==False).count()}`",
                    f"邀请仓库数: `{Repo.select().join(InviteCode).where(InviteCode.status<InviteCodeStatus.OK).group_by(InviteCode).count()}`",
                ]
            ),
            " " * 3,
        )

    @useroper(UserRole.SUPERADMIN)
    async def content_list_embycodes(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        show_all = show = context.parameters.get("embycode_show_all", False)
        while True:
            if show:
                ecs = EmbyCode.select()
            else:
                ecs = EmbyCode.select().where(EmbyCode.used == False)
            items = [
                (f"{i+1}. `{c.code}`\n({c.site})\n", str(i + 1), str(c.id))
                for i, c in enumerate(ecs.iterator())
            ]
            if not items:
                if not show_all:
                    if not show:
                        show = True
                        continue
                await context.answer("⚠️ 当前没有 Emby 邀请码.")
                return None
            else:
                if show and not show_all:
                    await context.answer("⚠️ 当前没有未使用 Emby 邀请码, 将显示全部邀请码")
                break
        return items

    @useroper(UserRole.SUPERADMIN)
    async def on_embycode_details(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ecid = int(parameters["embycode_id"])
        ec = EmbyCode.get_by_id(ecid)
        try:
            u = await client.get_users(ec.user.uid)
        except BadRequest:
            spec = "<未知>"
        else:
            spec = f"[{u.name}](tg://user?id={u.id}) ([管理](t.me/{client.me.username}?start=__u_{u.id}))"
        return "💌 邀请码信息如下:\n\n" + indent(
            "\n".join(
                [
                    f"站点: `{ec.site}`",
                    f"代码: `{ec.code}`",
                    f"发送人: {spec}",
                    f"已使用: {'是' if ec.used else '否'}",
                ]
            ),
            " " * 3,
        )

    @useroper(UserRole.SUPERADMIN)
    async def on_embycode_user_kick(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ecid = int(parameters["embycode_id"])
        ec = EmbyCode.get_by_id(ecid)
        parameters["user_id"] = ec.user.uid
        return await self.on_user_kick(handler, client, context, parameters)

    @useroper(UserRole.SUPERADMIN)
    async def on_embycode_use(self, handler, client: Client, context: TC, parameters: dict, user: User):
        ecid = int(parameters["embycode_id"])
        ec = EmbyCode.get_by_id(ecid)
        ec.used = True
        ec.save()
        await context.answer("✅ 成功.")
        await self.to_menu(client, context, "embycode")

    @useroper(UserRole.SUPERADMIN)
    async def on_embycode_show_all(self, handler, client: Client, context: TC, parameters: dict, user: User):
        current = context.parameters.get("embycode_show_all", False)
        context.parameters["embycode_show_all"] = not current
        await context.answer(f'✅ 当前显示: {"未使用" if current else "已使用"}.')
        await self.to_menu(client, context, "list_embycodes")

    @useroper()
    async def on_request_github_prime(
        self, handler, client: Client, context: TC, parameters: dict, user: User
    ):
        self.set_conversation(user, context, ConversationStatus.WAITING_GITHUB_USERNAME)
        return "✉️ 如果您是本项目的贡献者, 请输入您的用户名:"

    @useroper(UserRole.ADMIN)
    async def on_user_message(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_MESSAGE)
        return "✉️ 请输入信息:"

    @useroper(UserRole.CREATOR)
    async def on_new_invite_repo(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_GITHUB_USERNAME_CREATE)
        return "💆‍♀️ 请输入 Github 用户名:"

    @useroper()
    async def on_prime_invite(self, handler, client: Client, context: TC, parameters: dict, user: User):
        return await self.on_enter_invite_code(handler, client, context, parameters)

    @useroper()
    async def items_message_time(self, handler, client: Client, context: TC, parameters: dict, user: User):
        self.set_conversation(user, context, ConversationStatus.WAITING_MESSAGE_TIME)
        return [Element(str(h), str(h)) for h in range(24)]
