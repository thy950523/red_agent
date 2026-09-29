"""Runtime settings edited from the admin dashboard, hot-reloaded without restart.

Values the admin saves live as a sparse override set in runtime_config.json
(gitignored); anything not overridden falls back to the defaults below, so
environment variables such as WIDGET_AUTH_ENABLED keep working until the
admin explicitly takes over a setting from the dashboard.  Reads check the
file mtime, which keeps multiple uvicorn workers and hand edits in sync.
"""

import json
import os
from pathlib import Path
import threading


ROOT = Path(__file__).resolve().parent

# 与既有行为保持一致的默认值；后台保存后会写入 runtime_config.json 覆盖它们。
GENERATION_PROMPT_DEFAULT = (
    "可以帮我将第一张图的元素保持大致不变，把第二张图里的“路人鱼”形象替换到第一张图中，也就是将人物变成鱼。\n"
    "\n"
    "具体修改要求如下：\n"
    "1. 形象替换：保留第二张图“路人鱼”的所有特征。唯一可以添加的特色是它的表情、姿势，以及符合当前“路人鱼”设定的发型。\n"
    "2. 背景替换：将第一张图的背景元素替换成卡通海底元素。"
)


def _auth_enabled_default() -> bool:
    return os.environ.get("WIDGET_AUTH_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


# default 为可调用对象表示“每次取值时再计算”，目前只有 auth_enabled 需要读环境变量。
SPEC: dict[str, dict] = {
    "generation_enabled": {"type": "bool", "default": True},
    "auth_enabled": {"type": "bool", "default": _auth_enabled_default},
    "daily_limit": {"type": "int", "default": 5, "min": 1, "max": 200},
    "max_upload_mb": {"type": "int", "default": 20, "min": 1, "max": 50},
    "session_days": {"type": "int", "default": 7, "min": 1, "max": 90},
    "pending_minutes": {"type": "int", "default": 10, "min": 1, "max": 1440},
    "admin_session_days": {"type": "int", "default": 7, "min": 1, "max": 90},
    # 同一 IP 两次 /admin/login 的最小间隔；0 关闭限速。
    "admin_login_rate_limit_seconds": {"type": "int", "default": 3, "min": 0, "max": 60},
    "admin_password": {"type": "str", "default": "", "max_length": 128},
    "generation_prompt": {"type": "str", "default": GENERATION_PROMPT_DEFAULT,
                          "max_length": 8000, "required": True},
    "ark_model": {"type": "str", "default": "doubao-seedream-5-0-flash-260915",
                  "max_length": 100, "required": True},
    "generation_size": {"type": "str", "default": "2K", "choices": ["1K", "2K", "4K"]},
    "watermark": {"type": "bool", "default": False},
    # 人脸检测门禁默认关闭（2026-09-29 用户要求）：未检出人脸时用整图兜底照常出匹配，
    # 开启后恢复“无脸/模糊照片要求重传”的严格行为。
    "face_check_enabled": {"type": "bool", "default": False},
    "face_confidence_threshold": {"type": "float", "default": 0.80, "min": 0.05, "max": 1.0},
    "min_face_size_px": {"type": "int", "default": 48, "min": 16, "max": 500},
    "sharpness_threshold": {"type": "float", "default": 8.0, "min": 0.0, "max": 1000.0},
    "match_count": {"type": "int", "default": 3, "min": 1, "max": 10},
    # 小组件「发布」时 xhs.postNote 预填的笔记内容，经 /auth/config 下发；留空则不预填。
    # 平台自身还会限制标题约 20 字、正文约 1000 字，超长部分由发布器处理。
    "post_note_title": {"type": "str", "default": "", "max_length": 100},
    "post_note_content": {"type": "str", "default": "", "max_length": 3000},
    "post_note_tags": {"type": "str", "default": "", "max_length": 500},
}

# RLock：update() 持锁期间还要调 effective_values()。
_lock = threading.RLock()
_cache: dict = {"path": None, "mtime": None, "overrides": {}}


def _config_path() -> Path:
    return Path(os.environ.get("RUNTIME_CONFIG_PATH", ROOT / "runtime_config.json"))


def _validate(key: str, value) -> object:
    spec = SPEC.get(key)
    if spec is None:
        raise ValueError(f"未知配置项：{key}")
    kind = spec["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} 需要 true/false")
        return value
    if isinstance(value, bool):  # bool 是 int 的子类，先排除
        raise ValueError(f"{key} 需要是数字")
    if kind in {"int", "float"}:
        if not isinstance(value, (int, float)) or isinstance(value, str):
            raise ValueError(f"{key} 需要是数字")
        number = float(value)
        if kind == "int" and number != int(number):
            raise ValueError(f"{key} 需要是整数")
        number = number if kind == "float" else int(number)
        if "min" in spec and number < spec["min"]:
            raise ValueError(f"{key} 不能小于 {spec['min']}")
        if "max" in spec and number > spec["max"]:
            raise ValueError(f"{key} 不能大于 {spec['max']}")
        return number
    if not isinstance(value, str):
        raise ValueError(f"{key} 需要是文本")
    if "choices" in spec and value not in spec["choices"]:
        raise ValueError(f"{key} 只能是 {' / '.join(spec['choices'])}")
    if spec.get("required") and not value.strip():
        raise ValueError(f"{key} 不能为空")
    if len(value) > spec.get("max_length", 10000):
        raise ValueError(f"{key} 超过最大长度 {spec['max_length']}")
    return value


def _load_locked() -> dict:
    """Return the sparse override set, refreshing from disk when it changed."""
    path = _config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["overrides"]
    overrides: dict = {}
    if mtime is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                overrides = {
                    key: _validate(key, value)
                    for key, value in data.items()
                    if value is not None
                }
        except (OSError, ValueError, TypeError):
            overrides = {}  # 文件损坏时回退默认值，别把整个服务拖垮
    _cache.update({"path": path, "mtime": mtime, "overrides": overrides})
    return overrides


def get(key: str):
    spec = SPEC.get(key)
    if spec is None:
        raise KeyError(key)
    with _lock:
        overrides = _load_locked()
        if key in overrides:
            return overrides[key]
    default = spec["default"]
    return default() if callable(default) else default


def overridden_keys() -> set[str]:
    with _lock:
        return set(_load_locked())


def effective_values() -> dict:
    with _lock:
        overrides = _load_locked()
        values = {}
        for key, spec in SPEC.items():
            if key in overrides:
                values[key] = overrides[key]
                continue
            default = spec["default"]
            values[key] = default() if callable(default) else default
        return values


def update(values: dict) -> dict:
    """Validate and persist overrides; None removes a key's override."""
    if not isinstance(values, dict):
        raise ValueError("请求体需要是 JSON 对象")
    with _lock:
        overrides = _load_locked()
        validated = {
            key: None if value is None else _validate(key, value)
            for key, value in values.items()
        }
        merged = dict(overrides)
        for key, value in validated.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".pending.json")
        pending.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(pending, path)
        _cache.update({"path": path, "mtime": path.stat().st_mtime, "overrides": merged})
        return effective_values()
