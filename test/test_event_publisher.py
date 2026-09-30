"""EventPublisher 背压语义：慢消费者丢事件不丢回合。"""

import asyncio

from wiki_agent.events import EventPublisher, RunContext


def test_full_queue_drops_without_blocking():
    async def main():
        pub = EventPublisher(queue_size=1)
        ctx = RunContext(session_key="s", run_id="r1")
        async with pub.subscribe("r1") as q:
            await pub.publish(ctx, "first")
            await pub.publish(ctx, "second")  # 队列满——丢弃而非挂起
            await pub.publish(ctx, "third")
            assert q.get_nowait().type == "first"
            assert pub._dropped["r1"] == 2

    asyncio.run(main())


def test_unsubscribed_run_leaks_nothing():
    async def main():
        pub = EventPublisher()
        ctx = RunContext(session_key="s", run_id="r2")
        async with pub.subscribe("r2"):
            pass
        assert pub._subscribers == {}
        await pub.publish(ctx, "after")  # 无订阅者也要安然无事

    asyncio.run(main())
