"""一次候选的生成证据；HTTP 成败不能替代任务和产物生命周期。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .transport import HttpError, redact

RECOVERY_HINT = (
    "不要再次生成或切换候选重跑，以免重复计费。"
    "请先检查平台任务及账单；已有 files 可直接使用，urls 可单独下载取回。"
)


def _remote_urls(values):
    # 内联媒体不是远程恢复入口，不把整段 base64 复制到失败 JSON。
    return [str(value) for value in values if str(value).lower().startswith(("http://", "https://"))]


class GenerationInterrupted(HttpError):
    """已有结果或任务未确认：返回恢复入口，不允许再创建生成任务。"""
    stop_generation = True

    def __init__(self, message, *, files=None, urls=None, tasks=None,
                 phase="delivery", submission_uncertain=False, status=None, body=""):
        super().__init__(message, status, body, submission_uncertain=submission_uncertain)
        self.files = list(files or [])
        self.urls = _remote_urls(urls or [])
        self.tasks = [dict(task) for task in (tasks or [])]
        self.phase = phase
        self.hint = RECOVERY_HINT


@dataclass
class _Evidence:
    unresolved: bool = False
    generated: bool = False
    phase: str = "submission"
    files: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    current_task: int | None = None


_CURRENT = ContextVar("media_router_generation_evidence", default=None)


def submission_started():
    state = _CURRENT.get()
    if state is not None:
        state.phase = "submission"
        state.current_task = None


def submission_received():
    state = _CURRENT.get()
    if state is not None:
        state.unresolved = True


def task_accepted(task_id, query_url):
    state = _CURRENT.get()
    if state is not None:
        state.unresolved = True
        state.phase = "polling"
        state.tasks.append({"task_id": redact(str(task_id))[:512],
                            "query_url": redact(str(query_url))[:2048], "status": "pending"})
        state.current_task = len(state.tasks) - 1


def submission_rejected():
    """仅在协议明确拒绝或任务明确终止失败时调用；旧产物不清除。"""
    state = _CURRENT.get()
    if state is not None:
        state.unresolved = False
        if state.generated:
            state.phase = "partial"
        if state.current_task is not None:
            state.tasks[state.current_task]["status"] = "failed"
            state.current_task = None


def outputs_received(urls=(), *, inline=False, completed=True):
    state = _CURRENT.get()
    if state is not None and (urls or inline):
        state.generated = True
        if completed:
            state.unresolved = False
            state.phase = "delivery"
        for url in _remote_urls(urls):
            if url not in state.urls:
                state.urls.append(url)
        if completed and state.current_task is not None:
            state.tasks[state.current_task]["status"] = "completed"
            state.current_task = None


def file_completed(path, *, replaces=None):
    state = _CURRENT.get()
    if state is not None:
        state.generated = True
        state.unresolved = False
        state.phase = "delivery"
        if replaces is not None and str(replaces) in state.files:
            state.files.remove(str(replaces))
        if str(path) not in state.files:
            state.files.append(str(path))


def _extend_unique(target, values):
    for value in values:
        if value not in target:
            target.append(value)


@contextmanager
def generation_attempt():
    """隔离候选/线程；只转换带实际提交或产物证据的 Exception。"""
    state = _Evidence()
    token = _CURRENT.set(state)
    try:
        yield
    except Exception as exc:
        uncertain = state.unresolved or bool(getattr(exc, "submission_uncertain", False))
        if isinstance(exc, GenerationInterrupted):
            _extend_unique(exc.files, state.files)
            _extend_unique(exc.urls, state.urls)
            _extend_unique(exc.tasks, state.tasks)
            exc.submission_uncertain = uncertain
            raise
        if not state.generated and not state.unresolved:
            raise
        phase = "partial" if state.generated and state.phase != "delivery" else state.phase
        raise GenerationInterrupted(
            f"生成已产生结果或任务尚未确认，已停止后续生成：{type(exc).__name__}: {exc}",
            files=state.files, urls=state.urls, tasks=state.tasks, phase=phase,
            submission_uncertain=uncertain, status=getattr(exc, "status", None),
            body=getattr(exc, "body", ""),
        ) from exc
    finally:
        _CURRENT.reset(token)
