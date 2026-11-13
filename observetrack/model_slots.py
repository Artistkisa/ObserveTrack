import asyncio
import weakref
from collections import OrderedDict, deque
from contextlib import asynccontextmanager


class FairPool:
    def __init__(self, capacity=2):
        self.capacity = capacity
        self.active = 0
        self.waiting = OrderedDict()
        self.last_group = None

    def pump(self):
        while self.active < self.capacity and self.waiting:
            groups = list(self.waiting)
            group = next((candidate for candidate in groups if candidate != self.last_group), groups[0])
            queue = self.waiting[group]
            future = queue.popleft()
            if not queue:
                del self.waiting[group]
            if future.cancelled():
                continue
            self.active += 1
            self.last_group = group
            future.set_result(True)

    @asynccontextmanager
    async def slot(self, group):
        future = asyncio.get_running_loop().create_future()
        self.waiting.setdefault(group, deque()).append(future)
        self.pump()
        acquired = False
        try:
            await future
            acquired = True
            yield
        finally:
            if acquired or future.done() and not future.cancelled():
                self.active -= 1
            else:
                future.cancel()
            self.pump()


POOLS = weakref.WeakKeyDictionary()


def slot(group):
    loop = asyncio.get_running_loop()
    if loop not in POOLS:
        POOLS[loop] = FairPool()
    return POOLS[loop].slot(group)
