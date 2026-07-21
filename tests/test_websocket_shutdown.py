# -*- coding: utf-8 -*-
"""
Ctrl+C 종료 시 웹소켓 watcher 태스크가 함께 정리되는지 검증 (2026-07-22)

7/21 실거래 로그에서 발견된 문제: trading_loop가 KeyboardInterrupt를
자체적으로 잡아 정상 반환하면, asyncio.gather로 묶여 있던 watcher
태스크는 계속 살아남아 무한정 재연결을 반복함 (app.log에
"application stopped by user" 이후에도 [WS] 연결 시도가 계속 찍힘).

main.py를 asyncio.gather -> asyncio.wait(FIRST_COMPLETED) + 명시적
취소로 바꿨는데, 이 테스트는 main.py 전체를 실행하지 않고 동일한
패턴을 재현해서 로직 자체가 의도대로 동작하는지 확인한다.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


async def main() -> int:
    # ── 시나리오 1: trading_loop가 먼저 정상 종료되면 watcher가 취소되는지 ──
    watcher_cancelled = False
    watcher_stop_called = False

    async def fake_trading_loop():
        # 실제 trading_loop가 KeyboardInterrupt를 자체적으로 잡고
        # break로 반환하는 것과 동일한 상황을 재현
        await asyncio.sleep(0.05)
        return  # 정상 반환 (예외 없이)

    async def fake_watcher_start_guarded():
        nonlocal watcher_cancelled
        try:
            while True:
                await asyncio.sleep(1.0)  # 무한 재연결 루프를 흉내
        except asyncio.CancelledError:
            watcher_cancelled = True
            raise

    async def fake_watcher_stop():
        nonlocal watcher_stop_called
        watcher_stop_called = True

    # main.py에 적용한 것과 동일한 패턴
    trading_task = asyncio.create_task(fake_trading_loop())
    watcher_task = asyncio.create_task(fake_watcher_start_guarded())

    done, pending = await asyncio.wait(
        {trading_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED,
    )

    check("1) trading_loop가 done에 포함됨", trading_task in done)
    check("   watcher_task가 pending에 포함됨(아직 안 끝남)", watcher_task in pending)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    await fake_watcher_stop()

    check("2) pending 취소 후 watcher_task가 실제로 취소됨", watcher_cancelled is True)
    check("   watcher.stop()이 명시적으로 호출됨", watcher_stop_called is True)
    check("   watcher_task가 완료(cancelled) 상태", watcher_task.done())

    # ── 시나리오 2: watcher가 예외로 먼저 죽으면 trading_loop도 함께 정리되는지 ──
    trading_cancelled = False

    async def fake_trading_loop_2():
        nonlocal trading_cancelled
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            trading_cancelled = True
            raise

    async def fake_watcher_dies():
        await asyncio.sleep(0.05)
        raise RuntimeError("웹소켓 연결 영구 실패 시뮬레이션")

    trading_task2 = asyncio.create_task(fake_trading_loop_2())
    watcher_task2 = asyncio.create_task(fake_watcher_dies())

    done2, pending2 = await asyncio.wait(
        {trading_task2, watcher_task2}, return_when=asyncio.FIRST_COMPLETED,
    )

    check("3) watcher가 예외로 먼저 끝나면 done에 포함됨", watcher_task2 in done2)
    check("   trading_task는 아직 pending", trading_task2 in pending2)

    for task in pending2:
        task.cancel()
    if pending2:
        await asyncio.gather(*pending2, return_exceptions=True)

    check("4) watcher 예외 발생 시 trading_loop도 함께 취소됨", trading_cancelled is True)

    # 예외 전파 확인 (main.py의 "for task in done: if task.exception()" 로직)
    propagated_exception = None
    for task in done2:
        if task.exception() is not None:
            propagated_exception = task.exception()
    check("5) 먼저 죽은 태스크의 예외가 감지됨", propagated_exception is not None)
    check("   예외 메시지가 정확함", "웹소켓 연결 영구 실패" in str(propagated_exception))

    print()
    print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
