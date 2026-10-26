"""共享边界的协作式停止；不在请求返回后抛错，以免丢失远端成功结果。"""
import time


def check_stop_requested():
    from core.registration_service import check_stop_requested as check
    check()


def sleep(seconds):
    """注册worker分段等待；非任务调用保留原有一次sleep语义。"""
    from core.registration_service import _THREAD_CTX
    check_stop_requested()
    if getattr(_THREAD_CTX, 'job_id', None) is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        check_stop_requested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.25, remaining))
