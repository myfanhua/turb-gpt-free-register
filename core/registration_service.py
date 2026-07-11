# -*- coding: utf-8 -*-
"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from core import db

logger = logging.getLogger(__name__)

# 全局线程池，最大并发数（WebUI 每次提交时可按最新 workers 重建）
_DEFAULT_MAX_WORKERS = 4
_MIN_MAX_WORKERS = 1
_MAX_MAX_WORKERS = 16
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()


class StopRequested(RuntimeError):
    """用户手动停止注册任务。"""


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    job_id = getattr(_THREAD_CTX, "job_id", None)
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def _append_job_log(job_id: int, message: str) -> None:
    try:
        job = db.get_job(job_id)
        log_file = job.get("log_file") if job else None
        if not log_file:
            return
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with Path(log_file).open("a", encoding="utf-8") as f:
            f.write(f"{ts} [WARNING] [manual-stop] {message}\n")
    except Exception:
        pass


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    import random
    import string

    first = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    last = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    return f"{first} {last}"


def _prepare_registration_args() -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import register as _r, email as _e
    from core.email_provider import acquire_email
    from core.profile_utils import generate_random_birthday

    email = str(getattr(_r, "REGISTER_EMAIL", "") or "").strip()
    name = str(getattr(_r, "REGISTER_NAME", "") or "").strip()
    # WebUI/配置里有时会把空值存成 "-"，这不是合法 OpenAI 显示名，按空处理并自动生成
    if name in {"-", "—", "无", "空", "none", "None", "null", "NULL"}:
        name = ""

    if not email:
        if _e.USE_EMAIL_SERVICE:
            email = acquire_email()
        else:
            raise RuntimeError(
                "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
            )

    if not name:
        # 手动模式也自动生成显示名，减少配置负担
        name = _random_display_name()

    return email, name, generate_random_birthday()


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_WORKERS
    return max(_MIN_MAX_WORKERS, min(_MAX_MAX_WORKERS, value))


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回注册线程池。

    旧逻辑只在首次创建线程池时使用 max_workers，后续 WebUI 改线程数再提交仍会复用
    上一次的池。这里改成：每次传入的 max_workers 和当前池不一致时，立即创建新池供
    新提交任务使用；旧池不接收新任务，但会继续把已经排队/运行的任务跑完。
    """
    global _executor, _executor_workers, _executor_generation
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _executor_workers
    with _executor_lock:
        if _executor is None or requested_workers != _executor_workers:
            old_executor = _executor
            if old_executor is not None:
                # 不取消旧池里已提交的任务，只是不再往旧池追加新任务。
                old_executor.shutdown(wait=False, cancel_futures=False)
                _retired_executors.append(old_executor)
                logger.info(
                    "[Service] 注册线程池 workers 从 %s 切换为 %s；旧池继续处理已排队任务",
                    _executor_workers,
                    requested_workers,
                )
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
    return _executor


def get_executor_workers() -> int:
    """当前新提交注册任务会使用的线程数。"""
    with _executor_lock:
        return _executor_workers


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for ex in executors:
        ex.shutdown(wait=wait, cancel_futures=False)


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _run_one_job(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)
    _THREAD_CTX.job_id = int(job_id)
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))

    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.get_job(job_id)
    if not current:
        log_logger.info(f"[Job {job_id}] 任务记录已删除，跳过执行")
        return
    if current.get("status") == "cancelled":
        log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

    try:
        with _JobLogContext(log_file):
            from main import run_registration
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            email, name, birthday = _prepare_registration_args()
            db.update_job(job_id, email=email)
            check_stop_requested()
            result = run_registration(email=email, name=name, birthday=birthday)
            if is_stop_requested(job_id):
                db.update_job(
                    job_id,
                    status="stopped",
                    error="用户手动停止",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.warning(f"[Job {job_id}] 已按用户请求停止")
                return
            if isinstance(result, dict) and result.get("success"):
                db.update_job(
                    job_id,
                    status="success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.info(f"[Job {job_id}] 成功: {result.get('email')}")
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                db.update_job(
                    job_id,
                    status="failed",
                    email=(result or {}).get("email") if isinstance(result, dict) else None,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except StopRequested as exc:
        log_logger.warning(f"[Job {job_id}] 已停止: {exc}")
        db.update_job(
            job_id,
            status="stopped",
            error="用户手动停止",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        if is_stop_requested(job_id):
            log_logger.warning(f"[Job {job_id}] 停止中捕获异常，按停止处理: {type(exc).__name__}: {exc}")
            db.update_job(
                job_id,
                status="stopped",
                error="用户手动停止",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        with _STOP_LOCK:
            _STOP_EVENTS.pop(int(job_id), None)
            _ACTIVE_JOBS.discard(int(job_id))
        try:
            delattr(_THREAD_CTX, "job_id")
        except Exception:
            pass


# ============================================================
# 公共接口
# ============================================================

def submit_registration(count: int = 1, email_source: str | None = None, workers: int | None = None) -> list[dict]:
    """
    创建 N 个注册任务并提交到线程池。
    email_source 仅记录到 DB；实际邮箱来源固定为 Outlook 账号池。

    Returns:
        N 个新创建的 job dict
    """
    if email_source is None:
        from config import email as _email_cfg
        email_source = _email_cfg.EMAIL_SOURCE

    # 创建/切换线程池和提交本批任务必须整体串行化：否则另一请求在本批提交中途
    # 切换 workers 并 shutdown 旧池，会导致后续 submit 报 cannot schedule new futures after shutdown。
    with _executor_lock:
        executor = get_executor(max_workers=workers)
        effective_workers = get_executor_workers()
        jobs = []
        for _ in range(count):
            job = db.create_job(email_source=email_source)
            jobs.append(job)
            executor.submit(_run_one_job, job["id"], job["log_file"])
    logger.info(f"[Service] 已提交 {count} 个注册任务，源={email_source}，workers={effective_workers}")
    return jobs


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            db.update_job(
                int(job["id"]),
                status="cancelled",
                completed_at=now_iso,
                error="用户手动取消",
            )
            cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def request_stop_job(job_id: int) -> dict:
    """手动停止单个注册任务。pending 直接取消；running 设置停止标记，运行线程会在检查点退出。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在", "status": 404}
    status = job.get("status")
    now_iso = datetime.now().isoformat(timespec="seconds")
    if status == "pending":
        db.update_job(job_id, status="cancelled", completed_at=now_iso, error="用户手动停止/取消排队")
        _append_job_log(job_id, "用户手动停止：任务尚未运行，已取消排队。")
        return {"ok": True, "message": "排队任务已取消", "job_id": job_id, "state": "cancelled"}
    if status in ("success", "failed", "cancelled", "stopped"):
        return {"ok": True, "message": f"任务已结束：{status}", "job_id": job_id, "state": status}
    if status in ("running", "stopping"):
        with _STOP_LOCK:
            active = int(job_id) in _ACTIVE_JOBS
            ev = _STOP_EVENTS.get(int(job_id)) if active else None
            if ev is not None:
                ev.set()
        if not active or ev is None:
            # Web 服务重启、线程异常退出、历史残留 stopping，或之前手动停止时只创建了 stop event
            # 但没有真实线程实例：直接落为 stopped，避免永远卡在“停止中”。
            with _STOP_LOCK:
                _STOP_EVENTS.pop(int(job_id), None)
                _ACTIVE_JOBS.discard(int(job_id))
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="用户手动停止（任务实例不存在）",
            )
            _append_job_log(job_id, "用户手动停止：未找到运行中的任务实例，已直接标记为已停止。")
            logger.warning("[Service] 用户停止任务 #%s：任务实例不存在，已直接标记 stopped", job_id)
            return {"ok": True, "message": "任务实例不存在，已直接标记为已停止", "job_id": job_id, "state": "stopped"}
        db.update_job(job_id, status="stopping", error="用户手动停止中")
        _append_job_log(job_id, "用户手动停止：已发送停止信号，任务会在当前步骤检查点退出。")
        logger.warning("[Service] 用户请求停止任务 #%s", job_id)
        return {"ok": True, "message": "已发送停止信号", "job_id": job_id, "state": "stopping"}
    return {"ok": False, "error": f"当前状态不支持停止：{status}", "status": 409}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
