import asyncio
import logging
from pathlib import Path

import uvloop
import toml
import typer
from appdirs import user_data_dir
from loguru import logger
from rich import traceback
from rich.logging import Console, RichHandler
from rich.theme import Theme
from peewee import ManyToManyField

traceback.install()
uvloop.install()

from . import __author__, __name__, __url__, __version__

logger.remove()
logging.addLevelName(5, "TRACE")
logger.add(
    RichHandler(
        console=Console(stderr=True, theme=Theme({"logging.level.trace": "gray50"})),
        markup=True,
        rich_tracebacks=True,
    ),
    format=lambda _: "{message}",
    level=0,
)

app = typer.Typer(
    pretty_exceptions_show_locals=False,
    rich_markup_mode="rich",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

from .bot import Bot
from .model import BaseModel, User, UserLevel, Field, Log, db


@app.command(help=f"Bot server for [orange3]IW Exchanger[/] {__version__}.")
def main(
    config: Path = typer.Argument(
        ...,
        envvar=f"{__name__.upper()}_CONFIG",
        dir_okay=False,
        allow_dash=True,
        help="Config toml file",
    )
):
    with open(config) as f:
        config = toml.load(f)
    proxy = config.get("proxy", None)
    if proxy:
        proxy.setdefault("scheme", "socks5")
        proxy.setdefault("hostname", "127.0.0.1")
        proxy.setdefault("port", "1080")

    db_path = Path(config.get("db", "{data}/iwexchanger.db").format(data=user_data_dir(__name__)))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        models = []
        for m in BaseModel.__subclasses__():
            models.append(m)
        for m in BaseModel.__subclasses__():
            for n, f in m._meta.manytomany.items():
                if isinstance(f, ManyToManyField):
                    models.append(f.get_through_model())
        db.init(db_path)
        db.create_tables(models)
        system = User.create(uid="0", name="System")
        system_l = UserLevel.create(name="system")
        all_f = Field.create(name="all")
        system_l.fields.add(all_f)
        system.levels.add(system_l)
        for names in (
            "admin",
            "admin_user",
            "admin_message",
            "admin_admin",
            "admin_field",
            "admin_restriction",
            "admin_banner",
            "admin_trade",
            "admin_log",
            "admin_check",
            "admin_dispute",
            "view_trades",
            "add_trade",
            "exchange",
            "community",
        ):
            field = Field.create(name=names)
            Log.create(initiator=system, activity="add field", details=str(field.id))
        user_l = UserLevel.create(name="user")
        for names in ("view_trades", "add_trade", "exchange", "community"):
            fr = Field.get(name=names)
            user_l.fields.add(fr)
            Log.create(initiator=system, activity="add field to level", details=f"{system_l.id}, {fr.id}")
    else:
        db.init(db_path)

    async def doit():
        await Bot(**config["bot"], proxy=proxy).listen()

    asyncio.run(doit())
