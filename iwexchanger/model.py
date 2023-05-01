import datetime
from enum import IntEnum
from typing import Type

from peewee import *
from playhouse.postgres_ext import *

db = SqliteDatabase(None)


class BannerLocation(IntEnum):
    INSTANT = 10
    DAILY = 20
    TOP = 30
    BOTTOM = 40


class TradeStatus(IntEnum):
    PENDING = 10
    CHECKING = 20
    LAUNCHED = 30
    SOLD = 50
    TIMEDOUT = 60
    DISPUTED = 70
    VIOLATION = 80


class DisputeType(IntEnum):
    TRADE_NOT_AS_DESCRIPTION = 10
    TRADE_NO_GOOD = 20
    EXCHANGE_NOT_AS_DESCRIPTION = 50
    EXCHANGE_NO_GOOD = 60
    VIOLATION = 100


class ExchangeStatus(IntEnum):
    LAUNCHED = 10
    ACCEPTED = 20
    DECLINED = 30
    DISPUTED = 50


class EnumField(IntegerField):
    def __init__(self, choices: Type[IntEnum], *args, **kw):
        super(IntegerField, self).__init__(*args, **kw)
        self.choices = choices

    def db_value(self, value: IntEnum):
        return value.value

    def python_value(self, value: int):
        return self.choices(value)


class BaseModel(Model):
    class Meta:
        database = db


class Field(BaseModel):
    id = AutoField()
    name = CharField(unique=True)


class UserLevel(BaseModel):
    id = AutoField()
    name = CharField(unique=True)
    fields = ManyToManyField(Field, backref="levels")


class User(BaseModel):
    id = AutoField()
    uid = CharField(unique=True)
    name = CharField()
    levels = ManyToManyField(UserLevel, backref="users")
    coins = IntegerField(default=0)
    sanity = IntegerField(default=100)
    created = DateTimeField(default=datetime.datetime.now)
    activity = DateTimeField(default=datetime.datetime.now)
    chat = BooleanField(default=True)
    anonymous = BooleanField(default=False)

class BlackList(BaseModel):
    id = AutoField()
    by = ForeignKeyField(User)
    of = ForeignKeyField(User)


class Restriction(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, backref="restrictions")
    by = ForeignKeyField(User)
    created = DateTimeField(default=datetime.datetime.now)
    to = DateTimeField()
    fields = ManyToManyField(Field)


class Banner(BaseModel):
    id = AutoField()
    text = TextField()
    enabled = BooleanField(default=True)
    location = EnumField(BannerLocation, default=BannerLocation.TOP)


class Trade(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, backref="trades")
    name = CharField()
    exchange = CharField(null=True)
    coins = IntegerField()
    status = EnumField(TradeStatus, default=TradeStatus.PENDING)
    description = TextField(null=True)
    photo = CharField(null=True)
    good = TextField()
    available = DateTimeField(default=datetime.datetime.now)
    created = DateTimeField(default=datetime.datetime.now)
    modified = DateTimeField(null=True)
    revision = BooleanField(default=False)
    deleted = BooleanField(default=False)


class Log(BaseModel):
    id = AutoField()
    created = DateTimeField(default=datetime.datetime.now)
    initiator = ForeignKeyField(User)
    participants = ManyToManyField(User)
    activity = CharField()
    details = TextField(null=True)


class Dispute(BaseModel):
    id = AutoField()
    trade = ForeignKeyField(Trade, backref="disputes")
    user = ForeignKeyField(User, backref="disputes")
    type = EnumField(DisputeType, default=DisputeType.VIOLATION)
    created = DateTimeField(default=datetime.datetime.now)
    description = TextField(null=True)
    photo = CharField(null=True)
    influence = IntegerField(default=0)


class Exchange(BaseModel):
    id = AutoField()
    status = EnumField(ExchangeStatus, default=ExchangeStatus.LAUNCHED)
    user = ForeignKeyField(User, backref="exchanges")
    trade = ForeignKeyField(Trade, backref="exchanges")
    exchange = TextField(null=True)
    coins = IntegerField(default=0)
    description = TextField(null=True)
