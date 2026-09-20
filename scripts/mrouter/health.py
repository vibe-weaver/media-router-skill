"""模型健康度与熔断。

思路：某个模型连续失败到阈值就进入冷却期，路由时被优先排除；
冷却期结束自动恢复，冷却期间只要成功一次就清空失败计数。

状态落在 state/health.json，跨进程共享，**损坏时静默重建** —— 这句话是认真的：
路由的每一步都要问 is_cooling，这里一旦抛异常，整个生成链路就全断了，而报错
内容（'str' object has no attribute 'get'）跟真实原因毫无关系。

所以所有对外读取都走"取不到就用默认值"的路子：
  * 文件整体不是 dict、models 不是 dict -> 整个重建
  * models 下某个值不是 dict -> 丢掉这一条，其余照常
  * cooldown_until / *_count 不是数字 -> 当 0
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _as_float(value: Any, default: float = 0.0) -> float:
    """能转成浮点就转，转不了（None / 'later' / 列表…）就用默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class HealthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "models": {}}
        self._load()

    # ---------- 读写 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            return
        # 逐条清洗：非 dict 的条目直接丢掉。只判断外层的话，一个
        # {"models": {"m1": "oops"}} 就能让后面每一次 is_cooling 都炸。
        models: dict[str, dict[str, Any]] = {}
        for model_id, entry in raw["models"].items():
            if isinstance(entry, dict):
                models[str(model_id)] = entry
        raw["models"] = models
        raw.setdefault("schema_version", SCHEMA_VERSION)
        self._data = raw

    def save(self) -> None:
        tmp: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # tmp 名带上 pid：锁只在进程内有效，两个进程写同一个 tmp 会互相覆盖，
            # 然后各自 replace —— 后写的那份内容会被前一个 replace 顶掉。
            tmp = self.path.parent / f"{self.path.name}.{os.getpid()}.tmp"
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            # 状态写入失败不应让整个生成流程失败
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ---------- 查询 ----------

    def state(self, model_id: str) -> dict[str, Any]:
        entry = self._data["models"].get(model_id)
        return entry if isinstance(entry, dict) else {}

    def is_cooling(self, model_id: str, now: float | None = None) -> bool:
        until = _as_float(self.state(model_id).get("cooldown_until"))
        return until > (now if now is not None else time.time())

    def cooling_remaining(self, model_id: str, now: float | None = None) -> float:
        until = _as_float(self.state(model_id).get("cooldown_until"))
        left = until - (now if now is not None else time.time())
        return max(0.0, left)

    # ---------- 更新 ----------

    def record_success(self, model_id: str) -> None:
        entry = self._data["models"].setdefault(model_id, {})
        if not isinstance(entry, dict):  # 兜底：外部写坏过
            entry = {}
            self._data["models"][model_id] = entry
        entry["consecutive_failures"] = 0
        entry["cooldown_until"] = 0
        entry["last_error"] = ""
        entry["success_count"] = _as_int(entry.get("success_count")) + 1
        entry["last_success_ts"] = time.time()
        self.save()

    def record_failure(
        self, model_id: str, error: str, threshold: int, cooldown_seconds: float
    ) -> dict[str, Any]:
        entry = self._data["models"].setdefault(model_id, {})
        if not isinstance(entry, dict):
            entry = {}
            self._data["models"][model_id] = entry
        failures = _as_int(entry.get("consecutive_failures")) + 1
        entry["consecutive_failures"] = failures
        entry["failure_count"] = _as_int(entry.get("failure_count")) + 1
        entry["last_error"] = (error or "")[:500]
        entry["last_failure_ts"] = time.time()
        tripped = False
        if failures >= max(1, _as_int(threshold, 1)):
            entry["cooldown_until"] = time.time() + max(
                0.0, _as_float(cooldown_seconds)
            )
            tripped = True
        self.save()
        return {"consecutive_failures": failures, "circuit_open": tripped}

    def reset(self, model_id: str | None = None) -> None:
        if model_id:
            self._data["models"].pop(model_id, None)
        else:
            self._data["models"] = {}
        self.save()

    def snapshot(self, model_ids: list[str] | None = None) -> dict[str, Any]:
        models = self._data["models"]
        ids = model_ids if model_ids is not None else sorted(models)
        out: dict[str, Any] = {}
        for mid in ids:
            entry = models.get(mid) or {}
            if not isinstance(entry, dict):
                entry = {}
            out[mid] = {
                "cooling": self.is_cooling(mid),
                "cooldown_remaining_seconds": round(self.cooling_remaining(mid), 1),
                "consecutive_failures": _as_int(entry.get("consecutive_failures")),
                "success_count": _as_int(entry.get("success_count")),
                "failure_count": _as_int(entry.get("failure_count")),
                "last_error": str(entry.get("last_error") or ""),
            }
        return out
