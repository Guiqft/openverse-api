import json
import pytest
import asyncio
import time
import random
import logging as log
from worker.consumer import poll_consumer, consume
from worker.stats_reporting import StatsManager
from worker.util import process_image
from worker.rate_limit import RateLimitedClientSession
from PIL import Image
from collections import deque
from enum import Enum, auto
from functools import partial

log.basicConfig(level=log.DEBUG)


class FakeMessage:
    def __init__(self, value):
        self.value = value


class FakeConsumer:
    def __init__(self):
        self.messages = []

    def insert(self, message):
        self.messages.append(
            FakeMessage(bytes(message, 'utf-8'))
        )

    def consume(self, block=True):
        if self.messages:
            return self.messages.pop()
        else:
            return None

    def commit_offsets(self):
        pass


class FakeImageResponse:
    def __init__(self, status=200, corrupt=False):
        self.status = status
        self.corrupt = False

    async def read(self):
        # 1024 x 768 sample image
        if self.corrupt:
            location = 'test/test_worker.py'
        else:
            location = 'test/test_image.jpg'
        with open(location, 'rb') as f:
            return f.read()


class FakeAioSession:
    def __init__(self, corrupt=False, status=200):
        self.corrupt = corrupt
        self.status = status

    async def get(self, url):
        return FakeImageResponse(self.status, self.corrupt)


class FakeRedisPipeline:
    def __init__(self, redis):
        self.redis = redis
        # Deferred pipeline tasks
        self.todo = []

    async def rpush(self, key, value):
        self.todo.append(partial(self.redis.rpush, key, value))

    async def incr(self, key):
        self.todo.append(partial(self.redis.incr, key))

    async def zadd(self, key, score, value):
        self.todo.append(partial(self.redis.zadd, key, score, value))

    async def zremrangebyscore(self, key, start, end):
        self.todo.append(partial(self.redis.zremrangebyscore, key, start, end))

    async def __aexit__(self, exc_type, exc, tb):
        return self

    async def __aenter__(self):
        return self

    async def execute(self):
        for task in self.todo:
            await task()


class FakeRedis:
    def __init__(self, *args, **kwargs):
        self.store = {}

    async def set(self, key, val):
        self.store[key] = val

    async def decr(self, key):
        if key in self.store:
            self.store[key] -= 1
        else:
            self.store[key] = 1
        return self.store[key]

    async def rpush(self, key, value):
        if key not in self.store:
            self.store[key] = []
        self.store[key].append(value)

    async def incr(self, key):
        if key in self.store:
            self.store[key] += 0
        self.store[key] = 1
        return self.store[key]

    async def zadd(self, key, score, value):
        if key not in self.store:
            self.store[key] = []
        self.store[key].append((score, value))

    async def zremrangebyscore(self, key, start, end):
        # inefficiency tolerated because this is a mock
        start = float(start)
        end = float(end)
        delete_idxs = []
        for idx, tup in enumerate(self.store[key]):
            score, f = tup
            if start < score < end:
                delete_idxs.append(idx)
        for idx in reversed(delete_idxs):
            del self.store[key][idx]

    async def pipeline(self):
        return FakeRedisPipeline(self)


class AioNetworkSimulatingSession:
    """
    It's a FakeAIOSession, but it can simulate network latency, errors,
    and congestion. At 80% of its max load, it will start to slow down and occasionally
    throw an error. At 100%, error rates become very high and response times slow.
    """

    class Load(Enum):
        LOW = auto()
        HIGH = auto()
        OVERLOADED = auto()

    # Under high load, there is a 1/5 chance of an error being returned.
    high_load_status_choices = [403, 200, 200, 200, 200]
    # When overloaded, there's a 4/5 chance of an error being returned.
    overloaded_status_choices = [500, 403, 501, 400, 200]

    def __init__(self, max_requests_per_second=10, fail_if_overloaded=False):
        self.max_requests_per_second = max_requests_per_second
        self.requests_last_second = deque()
        self.load = self.Load.LOW
        self.fail_if_overloaded = fail_if_overloaded
        self.tripped = False

    def record_request(self):
        """ Record a request and flush out expired records. """
        if self.requests_last_second:
            while (self.requests_last_second
                   and time.time() - self.requests_last_second[0] > 1):
                self.requests_last_second.popleft()
        self.requests_last_second.append(time.time())

    def update_load(self):
        original_load = self.load
        rps = len(self.requests_last_second)
        utilization = rps / self.max_requests_per_second
        if utilization <= 0.8:
            self.load = self.Load.LOW
        elif 0.8 < utilization < 1:
            self.load = self.Load.HIGH
        else:
            self.load = self.Load.OVERLOADED
            if self.fail_if_overloaded:
                assert False, f"You DDoS'd the server! Utilization: " \
                              f"{utilization}, reqs/s: {rps}"
        if self.load != original_load:
            log.debug(f'Changed simulator load status to {self.load}')

    def lag(self):
        """ Determine how long a request should lag based on load. """
        if self.load == self.Load.LOW:
            wait = random.uniform(0.05, 0.15)
        elif self.load == self.Load.HIGH:
            wait = random.uniform(0.15, 0.6)
        # Overloaded
        else:
            wait = random.uniform(2, 10)
        return wait

    async def get(self, url):
        self.record_request()
        self.update_load()
        await asyncio.sleep(self.lag())
        if self.load == self.Load.HIGH:
            status = random.choice(self.high_load_status_choices)
        elif self.load == self.Load.OVERLOADED:
            status = random.choice(self.overloaded_status_choices)
        else:
            status = 200
        return FakeImageResponse(status)


def test_poll():
    """ Test message polling and parsing."""
    consumer = FakeConsumer()
    msgs = [
        {
            'url': 'http://example.org',
            'uuid': 'c29b3ccc-ff8e-4c66-a2d2-d9fc886872ca'
        },
        {
            'url': 'https://creativecommons.org/fake.jpg',
            'uuid': '4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d'
        }
    ]
    encoded_msgs = [json.dumps(msg) for msg in msgs]
    for msg in encoded_msgs:
        consumer.insert(msg)
    res = poll_consumer(consumer=consumer, batch_size=2)
    assert len(res) == 2


def validate_thumbnail(img, identifier):
    """ Check that the image was resized. """
    i = Image.open(img)
    width, height = i.size
    assert width <= 640 and height <= 480


@pytest.mark.asyncio
async def test_pipeline():
    """ Test that the image processor completes with a fake image. """
    # validate_thumbnail callback performs the actual assertions
    redis = FakeRedis()
    stats = StatsManager(redis)
    await process_image(
        persister=validate_thumbnail,
        session=FakeAioSession(),
        url='https://example.gov/hello.jpg',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        semaphore=asyncio.BoundedSemaphore(),
        stats=stats
    )
    log.debug(f'store: {redis.store}')
    assert redis.store['num_resized'] == 1
    assert redis.store['num_resized:example.gov'] == 1


@pytest.mark.asyncio
async def test_handles_corrupt_images_gracefully():
    redis = FakeRedis()
    stats = StatsManager(redis)
    await process_image(
        persister=validate_thumbnail,
        session=FakeAioSession(corrupt=True),
        url='fake_url',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        semaphore=asyncio.BoundedSemaphore(),
        stats=stats
    )


@pytest.mark.asyncio
async def test_records_errors():
    redis = FakeRedis()
    stats = StatsManager(redis)
    session = FakeAioSession(status=403)
    await process_image(
        persister=validate_thumbnail,
        session=session,
        url='https://example.gov/image.jpg',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        semaphore=asyncio.BoundedSemaphore(),
        stats=stats
    )
    expected_keys = [
        'resize_errors',
        'resize_errors:example.gov',
        'resize_errors:example.gov:403',
        'err60s:example.gov',
        'err1hr:example.gov',
        'err12hr:example.gov'
    ]
    for key in expected_keys:
        assert key in redis.store


async def _replenish_tokens_10rps(redis):
    """ Replenish rate limit tokens at 10 requests per second. """
    while True:
        await redis.set('currtokens:staticflickr.com', 10)
        await redis.set('currtokens:example.gov', 10)
        await asyncio.sleep(1)


async def get_mock_consumer(msg_count=1000, max_rps=10):
    """ Create a mock consumer with a bunch of fake messages in it. """
    consumer = FakeConsumer()
    msgs = [
        {
            'url': 'https://example.gov/hewwo.jpg',
            'uuid': '96136357-6f32-4174-b4ca-ae67e963bc55'
        }
    ]*msg_count
    encoded_msgs = [json.dumps(msg) for msg in msgs]
    for msg in encoded_msgs:
        consumer.insert(msg)

    redis = FakeRedis()
    loop = asyncio.get_event_loop()
    loop.create_task(_replenish_tokens_10rps(redis))

    aiosession = RateLimitedClientSession(
        AioNetworkSimulatingSession(
            max_requests_per_second=max_rps,
            fail_if_overloaded=True
        ),
        redis=redis
    )
    stats = StatsManager(redis)
    image_processor = partial(
        process_image, session=aiosession,
        persister=validate_thumbnail,
        stats=stats
    )
    return consume(consumer, image_processor, terminate=True)


async def mock_listen():
    consumer = await get_mock_consumer(msg_count=100, max_rps=11)
    log.debug('Starting consumer')
    await consumer


@pytest.mark.asyncio
async def test_rate_limiting():
    """
    Fails if we crawl aggressively enough to kill the simulated server.
    """
    await mock_listen()
