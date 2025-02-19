import asyncio
import re
import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout
import pytest
import pytest_asyncio
import redis
import redis.asyncio

from fakeredis import FakeServer, aioredis, FakeAsyncRedis, FakeStrictRedis
from test import testtools

pytestmark = []
fake_only = pytest.mark.parametrize("async_redis", [pytest.param("fake", marks=pytest.mark.fake)], indirect=True)
pytestmark.extend(
    [
        pytest.mark.asyncio,
    ]
)


@pytest_asyncio.fixture
async def conn(async_redis: redis.asyncio.Redis):
    """A single connection, rather than a pool."""
    async with async_redis.client() as conn:
        yield conn


async def test_ping(async_redis: redis.asyncio.Redis):
    pong = await async_redis.ping()
    assert pong is True


async def test_types(async_redis: redis.asyncio.Redis):
    await async_redis.hset("hash", mapping={"key1": "value1", "key2": "value2", "key3": 123})
    result = await async_redis.hgetall("hash")
    assert result == {b"key1": b"value1", b"key2": b"value2", b"key3": b"123"}


async def test_transaction(async_redis: redis.asyncio.Redis):
    async with async_redis.pipeline(transaction=True) as tr:
        tr.set("key1", "value1")
        tr.set("key2", "value2")
        ok1, ok2 = await tr.execute()
    assert ok1
    assert ok2
    result = await async_redis.get("key1")
    assert result == b"value1"


async def test_transaction_fail(async_redis: redis.asyncio.Redis):
    await async_redis.set("foo", "1")
    async with async_redis.pipeline(transaction=True) as tr:
        await tr.watch("foo")
        await async_redis.set("foo", "2")  # Different connection
        tr.multi()
        tr.get("foo")
        with pytest.raises(redis.asyncio.WatchError):
            await tr.execute()


async def test_pubsub(async_redis, event_loop):
    queue = asyncio.Queue()

    async def reader(ps):
        while True:
            message = await ps.get_message(ignore_subscribe_messages=True, timeout=5)
            if message is not None:
                if message.get("data") == b"stop":
                    break
                queue.put_nowait(message)

    async with async_timeout(5), async_redis.pubsub() as ps:
        await ps.subscribe("channel")
        task = event_loop.create_task(reader(ps))
        await async_redis.publish("channel", "message1")
        await async_redis.publish("channel", "message2")
        result1 = await queue.get()
        result2 = await queue.get()
        assert result1 == {"channel": b"channel", "pattern": None, "type": "message", "data": b"message1"}
        assert result2 == {"channel": b"channel", "pattern": None, "type": "message", "data": b"message2"}
        await async_redis.publish("channel", "stop")
        await task


@pytest.mark.slow
async def test_pubsub_timeout(async_redis: redis.asyncio.Redis):
    async with async_redis.pubsub() as ps:
        await ps.subscribe("channel")
        await ps.get_message(timeout=0.5)  # Subscription message
        message = await ps.get_message(timeout=0.5)
        assert message is None


@pytest.mark.slow
async def test_pubsub_disconnect(async_redis: redis.asyncio.Redis):
    async with async_redis.pubsub() as ps:
        await ps.subscribe("channel")
        await ps.connection.disconnect()
        message = await ps.get_message(timeout=0.5)  # Subscription message
        assert message is not None
        message = await ps.get_message(timeout=0.5)
        assert message is None


async def test_blocking_ready(async_redis, conn):
    """Blocking command which does not need to block."""
    await async_redis.rpush("list", "x")
    result = await conn.blpop("list", timeout=1)
    assert result == (b"list", b"x")


@pytest.mark.slow
async def test_blocking_timeout(conn):
    """Blocking command that times out without completing."""
    result = await conn.blpop("missing", timeout=1)
    assert result is None


@pytest.mark.slow
async def test_blocking_unblock(async_redis, conn, event_loop):
    """Blocking command that gets unblocked after some time."""

    async def unblock():
        await asyncio.sleep(0.1)
        await async_redis.rpush("list", "y")

    task = event_loop.create_task(unblock())
    result = await conn.blpop("list", timeout=1)
    assert result == (b"list", b"y")
    await task


async def test_wrongtype_error(async_redis: redis.asyncio.Redis):
    await async_redis.set("foo", "bar")
    with pytest.raises(redis.asyncio.ResponseError, match="^WRONGTYPE"):
        await async_redis.rpush("foo", "baz")


async def test_syntax_error(async_redis: redis.asyncio.Redis):
    with pytest.raises(redis.asyncio.ResponseError, match="^wrong number of arguments for 'get' command$"):
        await async_redis.execute_command("get")


@testtools.run_test_if_lupa
class TestScripts:
    async def test_no_script_error(self, async_redis: redis.asyncio.Redis):
        with pytest.raises(redis.exceptions.NoScriptError):
            await async_redis.evalsha("0123456789abcdef0123456789abcdef", 0)

    @pytest.mark.max_server("6.2.7")
    async def test_failed_script_error6(self, async_redis):
        await async_redis.set("foo", "bar")
        with pytest.raises(redis.asyncio.ResponseError, match="^Error running script"):
            await async_redis.eval('return redis.call("ZCOUNT", KEYS[1])', 1, "foo")

    @pytest.mark.min_server("7")
    async def test_failed_script_error7(self, async_redis):
        await async_redis.set("foo", "bar")
        with pytest.raises(redis.asyncio.ResponseError):
            await async_redis.eval('return redis.call("ZCOUNT", KEYS[1])', 1, "foo")


@testtools.run_test_if_redispy_ver("gte", "5.1")
async def test_repr_redis_51(async_redis: redis.asyncio.Redis):
    assert re.fullmatch(
        r"<redis.asyncio.connection.ConnectionPool("
        r"<fakeredis.aioredis.FakeConnection(server=<fakeredis._server.FakeServer object at .*>,db=0)>)>",
        repr(async_redis.connection_pool),
    )


@fake_only
@pytest.mark.disconnected
async def test_not_connected(async_redis: redis.asyncio.Redis):
    with pytest.raises(redis.asyncio.ConnectionError):
        await async_redis.ping()


@fake_only
async def test_disconnect_server(async_redis, fake_server):
    await async_redis.ping()
    fake_server.connected = False
    with pytest.raises(redis.asyncio.ConnectionError):
        await async_redis.ping()
    fake_server.connected = True


async def test_type(async_redis: redis.asyncio.Redis):
    await async_redis.set("string_key", "value")
    await async_redis.lpush("list_key", "value")
    await async_redis.sadd("set_key", "value")
    await async_redis.zadd("zset_key", {"value": 1})
    await async_redis.hset("hset_key", "key", "value")

    assert b"string" == await async_redis.type("string_key")  # noqa: E721
    assert b"list" == await async_redis.type("list_key")  # noqa: E721
    assert b"set" == await async_redis.type("set_key")  # noqa: E721
    assert b"zset" == await async_redis.type("zset_key")  # noqa: E721
    assert b"hash" == await async_redis.type("hset_key")  # noqa: E721
    assert b"none" == await async_redis.type("none_key")  # noqa: E721


async def test_xdel(async_redis: redis.asyncio.Redis):
    stream = "stream"

    # deleting from an empty stream doesn't do anything
    assert await async_redis.xdel(stream, 1) == 0

    m1 = await async_redis.xadd(stream, {"foo": "bar"})
    m2 = await async_redis.xadd(stream, {"foo": "bar"})
    m3 = await async_redis.xadd(stream, {"foo": "bar"})

    # xdel returns the number of deleted elements
    assert await async_redis.xdel(stream, m1) == 1
    assert await async_redis.xdel(stream, m2, m3) == 2


@pytest.mark.fake
async def test_from_url():
    r0 = aioredis.FakeRedis.from_url("redis://localhost?db=0")
    r1 = aioredis.FakeRedis.from_url("redis://localhost?db=1")
    # Check that they are indeed different databases
    await r0.set("foo", "a")
    await r1.set("foo", "b")
    assert await r0.get("foo") == b"a"
    assert await r1.get("foo") == b"b"
    await r0.connection_pool.disconnect()
    await r1.connection_pool.disconnect()


@pytest.mark.fake
async def test_from_url_with_version():
    r0 = aioredis.FakeRedis.from_url("redis://localhost?db=0", version=(6,))
    r1 = aioredis.FakeRedis.from_url("redis://localhost?db=1", version=(6,))
    # Check that they are indeed different databases
    await r0.set("foo", "a")
    await r1.set("foo", "b")
    assert await r0.get("foo") == b"a"
    assert await r1.get("foo") == b"b"
    await r0.connection_pool.disconnect()
    await r1.connection_pool.disconnect()


@fake_only
async def test_from_url_with_server(async_redis, fake_server):
    r2 = aioredis.FakeRedis.from_url("redis://localhost", server=fake_server)
    await async_redis.set("foo", "bar")
    assert await r2.get("foo") == b"bar"
    await r2.connection_pool.disconnect()


@pytest.mark.fake
async def test_without_server():
    r = aioredis.FakeRedis()
    assert await r.ping()


@pytest.mark.fake
async def test_without_server_disconnected():
    r = aioredis.FakeRedis(connected=False)
    with pytest.raises(redis.asyncio.ConnectionError):
        await r.ping()


@pytest.mark.fake
async def test_async():
    # arrange
    cache = aioredis.FakeRedis()
    # act
    await cache.set("fakeredis", "plz")
    x = await cache.get("fakeredis")
    # assert
    assert x == b"plz"


@testtools.run_test_if_redispy_ver("gte", "4.4.0")
@pytest.mark.parametrize("nowait", [False, True])
@pytest.mark.fake
async def test_connection_disconnect(nowait):
    server = FakeServer()
    r = aioredis.FakeRedis(server=server)
    conn = await r.connection_pool.get_connection("_")
    assert conn is not None

    await conn.disconnect(nowait=nowait)

    assert conn._sock is None


async def test_connection_with_username_and_password():
    server = FakeServer()
    r = aioredis.FakeRedis(server=server, username="username", password="password")

    test_value = "this_is_a_test"
    await r.hset("test:key", "test_hash", test_value)
    result = await r.hget("test:key", "test_hash")
    assert result.decode() == test_value


@pytest.mark.fake
async def test_init_args():
    sync_r1 = FakeStrictRedis()
    r1 = FakeAsyncRedis()
    r5 = FakeAsyncRedis()
    r2 = FakeAsyncRedis(server=FakeServer())

    shared_server = FakeServer()
    r3 = FakeAsyncRedis(server=shared_server)
    r4 = FakeAsyncRedis(server=shared_server)

    await r1.set("foo", "bar")
    await r3.set("bar", "baz")

    assert await r1.get("foo") == b"bar"
    assert await r5.get("foo") is None
    assert sync_r1.get("foo") is None
    assert await r2.get("foo") is None
    assert await r3.get("foo") is None

    assert await r3.get("bar") == b"baz"
    assert await r4.get("bar") == b"baz"
    assert await r1.get("bar") is None


@pytest.mark.asyncio
async def test_cause_fakeredis_bug(async_redis):
    if sys.version_info < (3, 11):
        return

    async def worker_task():
        assert await async_redis.rpush("list1", "list1_val") == 1  # 1
        assert await async_redis.blpop("list2") == (b"list2", b"list2_val")  # 4
        assert await async_redis.set("foo", "bar") is True  # 5

    async with asyncio.TaskGroup() as tg:
        tg.create_task(worker_task())
        assert await async_redis.blpop("list1") == (b"list1", b"list1_val")  # 2
        assert await async_redis.rpush("list2", "list2_val") == 1  # 3

    # await async_redis.get("foo")  # uncomment to make test pass
    assert await async_redis.get("foo") == b"bar"
