"""看门狗触发逻辑测试 (不真退出进程)。"""
from __future__ import annotations

import asyncio

import pytest

from app.watchdog import HealthWatchdog, default_probe


async def _run_watchdog(probe_results, *, threshold=2, interval=0.01, timeout=0.2):
    exits: list[int] = []
    idx = 0

    def probe() -> None:
        nonlocal idx
        if idx < len(probe_results):
            result = probe_results[idx]
            idx += 1
            if isinstance(result, BaseException):
                raise result
        # 脚本耗尽后恒为成功

    wd = HealthWatchdog(
        probe,
        exit_cb=exits.append,
        interval_s=interval,
        probe_timeout_s=timeout,
        failure_threshold=threshold,
    )
    wd.start()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if exits or wd._task.done():
            break
    await wd.stop()
    return exits


async def test_consecutive_failures_trigger_exit() -> None:
    exits = await _run_watchdog([RuntimeError("wedge"), TimeoutError("wedge")])
    assert exits == [70]


async def test_success_resets_failure_counter() -> None:
    # 失败 1 次 → 成功 → 再失败 1 次: 未达连续阈值, 不退出。
    exits = await _run_watchdog([RuntimeError("slow"), None, RuntimeError("slow")])
    assert exits == []


async def test_default_probe_passes_on_healthy_resources() -> None:
    import threading

    lock = threading.Lock()
    default_probe(lock)  # 不抛即通过
    default_probe(None)


# ---------------------------------------------------------------------------
# 关闭路径回归 (2026-09-21: 取消被兜底 except 吞掉)
# ---------------------------------------------------------------------------

async def test_loop_propagates_cancelled_error() -> None:
    """_loop 必须让 CancelledError 向上传播, 不得计入探测失败。

    回归守护(2026-09-21): CancelledError 继承 BaseException 而非 Exception, 旧实现的
    `except BaseException` 会把它当成"连续失败" —— 达到阈值时在关闭过程中调用
    exit_cb(70)(生产实现是 os._exit, 即"正常关闭"被误判为僵死而强杀), 未达阈值时
    协程继续下一轮、任务永不结束, `stop()` 里的 await self._task 永久挂起
    (实测把 pytest 吊死 51 分钟, 应用关不掉进程)。

    这里让探测直接抛 CancelledError 并驱动 _loop, 不依赖真实取消时序: 修复前会被
    吞掉并走退出分支(不抛异常), 修复后原样传播 —— 两种实现下本用例都有界, 回归时
    是干净的断言失败而不是无限挂起。
    """
    exits: list[int] = []

    def probe() -> None:
        raise asyncio.CancelledError()

    wd = HealthWatchdog(
        probe,
        exit_cb=exits.append,
        interval_s=0.01,
        probe_timeout_s=1.0,
        failure_threshold=1,  # 修复前: 这一次"失败"立即触发退出, 最能暴露误判
    )

    with pytest.raises(asyncio.CancelledError):
        await wd._loop()

    assert exits == [], "取消被当成探测失败, 关闭动作会误触发进程退出"

    assert exits == [], "关闭动作被当成探测失败, 会误触发进程退出"

