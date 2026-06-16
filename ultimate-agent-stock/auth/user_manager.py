"""用户管理 — 简易多用户系统

设计：
  用户数据: data_store/users.json  {用户名: {password: 密码, display_name: 昵称}}
  会话: 内存 dict，token → username
  数据隔离: data_store/users/{username}/positions.json
"""
import json, hashlib, secrets, time
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).parent.parent.resolve()
USERS_FILE = ROOT_DIR / "data_store" / "users.json"
USERS_DATA_DIR = ROOT_DIR / "data_store" / "users"

# 会话存储 {token: {username, expiry}}
_SESSION_FILE = ROOT_DIR / "data_store" / "sessions.json"
_sessions = {}

def _load_sessions():
    """从磁盘加载会话（web重启后恢复）"""
    global _sessions
    try:
        if _SESSION_FILE.exists():
            data = json.loads(_SESSION_FILE.read_text())
            # 清理过期的
            now = time.time()
            _sessions = {k: v for k, v in data.items() if v.get("expiry", 0) > now}
    except Exception:
        _sessions = {}

def _save_sessions():
    """保存会话到磁盘"""
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(_sessions))
    except Exception:
        pass


def _ensure():
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not USERS_FILE.exists():
        with open(USERS_FILE, "w") as f:
            json.dump({}, f)


def _load_users() -> dict:
    _ensure()
    with open(USERS_FILE) as f:
        return json.load(f)


def _save_users(users: dict):
    _ensure()
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()[:16]


def create_user(username: str, password: str, display_name: str = "") -> bool:
    """创建用户，如果已存在返回 False"""
    users = _load_users()
    if username in users:
        return False
    users[username] = {
        "password": _hash_password(password),
        "display_name": display_name or username,
        "created_at": time.strftime("%Y-%m-%d"),
    }
    _save_users(users)
    # 创建用户数据目录
    user_dir = USERS_DATA_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    if not (user_dir / "positions.json").exists():
        with open(user_dir / "positions.json", "w") as f:
            json.dump([], f)
    return True


def verify_user(username: str, password: str) -> bool:
    users = _load_users()
    user = users.get(username)
    if not user:
        return False
    return user["password"] == _hash_password(password)


def login(username: str, password: str) -> Optional[str]:
    """登录，成功返回 token"""
    if not verify_user(username, password):
        return None
    _load_sessions()
    token = secrets.token_hex(16)
    _sessions[token] = {"username": username, "expiry": time.time() + 86400 * 7}
    _save_sessions()
    return token


def logout(token: str):
    _sessions.pop(token, None)
    _save_sessions()


def get_user(token: str) -> Optional[dict]:
    """通过 token 获取用户信息"""
    _load_sessions()
    session = _sessions.get(token)
    if not session:
        return None
    if time.time() > session["expiry"]:
        _sessions.pop(token, None)
        return None
    users = _load_users()
    user = users.get(session["username"])
    if not user:
        return None
    return {"username": session["username"], "display_name": user.get("display_name", session["username"])}


def get_user_positions(username: str) -> list:
    """获取用户的持仓数据"""
    path = USERS_DATA_DIR / username / "positions.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def save_user_positions(username: str, positions: list):
    path = USERS_DATA_DIR / username / "positions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def list_users() -> list[str]:
    return list(_load_users().keys())
