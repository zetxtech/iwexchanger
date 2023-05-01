import asyncio
import functools
from typing import Awaitable, Callable, Iterable, ParamSpec, Sized, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class AsyncCountPool(dict):
    def __init__(self, *args, base=1000, **kw):
        super().__init__(*args, **kw)
        self.lock = asyncio.Lock()
        self.next = base + 1

    async def append(self, value):
        async with self.lock:
            key = self.next
            self[key] = value
            self.next += 1
            return key


def batch(l: Sized, n=1):
    """Make list of list of certain size from a list."""
    size = len(l)
    for ndx in range(0, size, n):
        yield l[ndx : min(ndx + n, size)]


def remove_prefix(text: str, prefix: str):
    """Remove prefix from the begining of test."""
    return text[text.startswith(prefix) and len(prefix) :]


def walk(l: Iterable[Iterable]):
    """Iterate over a irregular n-dimensional list."""
    for el in l:
        if isinstance(el, Iterable) and not isinstance(el, (str, bytes)):
            yield from flatten(el)
        else:
            yield el


def flatten(l: Iterable[Iterable]):
    """Flatten a irregular n-dimensional list to a 1-dimensional list."""
    return type(l)(walk(l))


def flatten2(l: Iterable[Iterable]):
    """Flatten a 2-dimensional list to a 1-dimensional list."""
    return [i for j in l for i in j]


def truncate_str(text: str, length: int):
    """Truncate a str to a certain length, and the omitted part is represented by "..."."""
    return f"{text[:length + 3]}..." if len(text) > length else text


def truncate_str_reverse(text: str, length: int):
    """Truncate a str to a certain length from the end, and the omitted part is represented by "..."."""
    return f"...{text[- length - 3:]}" if len(text) > length else text


def async_partial(f, *args1, **kw1):
    async def func(*args2, **kw2):
        return await f(*args1, *args2, **kw1, **kw2)

    return func


def force_async(fn: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    """Turns a sync function to async function using threads."""
    from concurrent.futures import ThreadPoolExecutor
    import asyncio

    pool = ThreadPoolExecutor()

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        future = pool.submit(fn, *args, **kwargs)
        return asyncio.wrap_future(future)  # make it awaitable

    return wrapper


def force_sync(fn: Callable[P, Awaitable[T]]) -> Callable[P, T]:
    """Turn an async function to sync function."""
    import asyncio

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        res = fn(*args, **kwargs)
        if asyncio.iscoroutine(res):
            return asyncio.get_event_loop().run_until_complete(res)
        return res

    return wrapper
