"""协作协议的进程锁、项目配置与文件安装；仅使用 Python 标准库。"""

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from string import Template
import subprocess
import time
import uuid


CONFIG_NAME = ".parallel-code-tasks.json"
DEFAULT_VERIFICATION = {"commands": [], "timeout_seconds": 300}
DEFAULT_CONFIG = {"version": 1, "copy_files": [], "env": {}, "resource_templates": [],
                  "exclusive_paths": [], "port_range": [18000, 18999], "verification": DEFAULT_VERIFICATION}
RULE_BEGIN = "<!-- parallel-code-tasks:begin -->"
RULE_END = "<!-- parallel-code-tasks:end -->"
LOCK_WAIT = 5.0


class WorkflowError(RuntimeError):
    """可诊断的失败；不回滚用户文件，也不伪造成功。"""


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def process_identity(pid):
    """只查询进程；Windows 不使用 os.kill(pid, 0)，避免终止进程。"""
    if not isinstance(pid, int) or pid <= 0:
        return {"state": "missing", "start": None}
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {"state": "missing" if ctypes.get_last_error() == 87 else "unknown", "start": None}
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return {"state": "unknown", "start": None}
            if code.value != 259:
                return {"state": "missing", "start": None}
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return {"state": "alive", "start": None}
            start = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return {"state": "alive", "start": str(start)}
        finally:
            kernel.CloseHandle(handle)
    proc = Path(f"/proc/{pid}/stat")
    if Path("/proc").exists():
        try:
            fields = proc.read_text().rsplit(")", 1)[1].split()
            return {"state": "missing" if fields[0] == "Z" else "alive", "start": fields[19]}
        except FileNotFoundError:
            return {"state": "missing", "start": None}
        except (OSError, IndexError):
            return {"state": "unknown", "start": None}
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, check=False)
    value = result.stdout.decode("utf-8", "replace").strip()
    return {"state": "alive" if value else "missing" if result.returncode == 1 else "unknown", "start": value or None}


def owner_state(owner):
    if owner.get("host") != socket.gethostname():
        return "unknown"
    current = process_identity(owner.get("pid"))
    if current["state"] == "alive" and owner.get("start") and current["start"]:
        if owner["start"] != current["start"]:
            return "missing"
    return current["state"]


def _try_lock(stream):
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(error, "winerror", None) in {32, 33}:
            return False
        raise


def _unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_mutex(path, wait=None):
    """锁文件永久保留，内核锁随进程退出释放；不删除/重建锁文件抢占。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
        while not _try_lock(stream):
            require(time.monotonic() < deadline, f"等待锁超时：{path}；请用 doctor 检查，不按时间抢锁")
            time.sleep(0.05)
        owner_file = path.with_suffix(path.suffix + ".owner.json")
        identity = uuid.uuid4().hex
        try:
            atomic_json(owner_file, {"id": identity, "pid": os.getpid(), "host": socket.gethostname(),
                                     "start": process_identity(os.getpid())["start"], "time": time.time()})
            yield
        finally:
            try:
                if owner_file.exists() and json.loads(owner_file.read_text(encoding="utf-8")).get("id") == identity:
                    owner_file.unlink()
            finally:
                _unlock(stream)


def lock_info(path):
    """仅探测已有锁，不创建文件、不修改登记。"""
    path = Path(path)
    held = False
    if path.exists():
        with path.open("r+b") as stream:
            held = not _try_lock(stream)
            if not held:
                _unlock(stream)
    owner_file = path.with_suffix(path.suffix + ".owner.json")
    try:
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 持锁进程可能恰好正常退出；缺失 owner 不等于锁是否占用。
        owner = None
    return {"path": str(path), "held": held, "owner": owner, "stale_owner_record": bool(owner and not held)}


def safe_path(root, relative):
    """受管文件必须留在明确根目录内，不沿符号链接写入。"""
    root = Path(root).resolve()
    relative = str(relative).replace("\\", "/")
    require(relative and not relative.startswith("/") and ":" not in relative
            and all(part not in {"", ".", "..", ".git"} for part in relative.split("/")), "受管路径必须是仓库内具体相对路径")
    target = root / relative
    require(target.resolve().is_relative_to(root), "受管路径越出根目录")
    current = target
    while current != root:
        require(not current.is_symlink(), f"受管路径含符号链接：{current}")
        current = current.parent
    return target


def load_config(root):
    path = safe_path(root, CONFIG_NAME)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(DEFAULT_CONFIG)
    require(isinstance(config, dict) and config.get("version") == 1, "项目配置需要 version: 1")
    require(not set(config).difference(DEFAULT_CONFIG), "项目配置存在未知字段")
    config = {**DEFAULT_CONFIG, **config}
    for key in ("copy_files", "resource_templates", "exclusive_paths"):
        require(isinstance(config[key], list) and all(isinstance(item, str) for item in config[key]), f"{key} 必须是字符串数组")
    require(isinstance(config["env"], dict), "env 必须是对象")
    for key, value in config["env"].items():
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and isinstance(value, str), "env 名称或值无效")
        require(key.upper() not in {"PATH", "HOME", "USERPROFILE", "CODEX_HOME"}
                and not key.upper().startswith(("GIT_", "PARALLEL_")), "env 不能覆盖系统目录、Git 或协议变量")
    ports = config["port_range"]
    require(isinstance(ports, list) and len(ports) == 2 and all(type(p) is int for p in ports)
            and 1 <= ports[0] <= ports[1] <= 65535, "port_range 必须是合法端口区间")
    verification = config["verification"]
    require(isinstance(verification, dict) and not set(verification).difference(DEFAULT_VERIFICATION),
            "verification 仅支持 commands 和 timeout_seconds")
    verification = {**DEFAULT_VERIFICATION, **verification}
    commands = verification["commands"]
    require(isinstance(commands, list) and all(isinstance(argv, list) and argv
            and all(isinstance(arg, str) and "\0" not in arg for arg in argv) and argv[0].strip()
            for argv in commands), "verification.commands 必须是非空 argv 数组的列表，不接受 shell 字符串")
    require(type(verification["timeout_seconds"]) is int and verification["timeout_seconds"] > 0,
            "verification.timeout_seconds 必须是正整数，按每条命令计时")
    config["verification"] = verification
    return config


def render(value, variables):
    try:
        return Template(value).substitute(variables)
    except (KeyError, ValueError) as error:
        raise WorkflowError(f"配置模板变量缺失或格式无效：{error}") from error


def task_environment(task):
    variables = {"task": task["id"], "worktree": task["path"], "source": task["source_path"]}
    for item in task["resources"]:
        kind, _, value = item.partition(":")
        variables.setdefault(kind, value)
    values = {"PARALLEL_TASK_ID": task["id"], "PARALLEL_WORKTREE": task["path"], "PARALLEL_SOURCE": task["source_path"]}
    for key in ("port", "db", "redis", "docker"):
        if key in variables:
            values["PARALLEL_" + key.upper()] = variables[key]
    values.update({key: render(value, variables) for key, value in task.get("_profile", DEFAULT_CONFIG)["env"].items()})
    return values


def install_bundle(root, directory, skill_root, rules_files, update=False):
    """幂等安装；预检全部目标后写入，既有用户内容和换行保持。"""
    root, skill_root = Path(root), Path(skill_root)
    destination = safe_path(root, ".agents/skills/parallel-code-tasks")
    manifest_path = directory / "install.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": {}}
    plan = []
    files = {}
    for source in sorted(skill_root.rglob("*")):
        if not source.is_file() or any(part in {"__pycache__", ".git"} for part in source.relative_to(skill_root).parts):
            continue
        require(not source.is_symlink(), "安装包不能包含符号链接")
        relative = ".agents/skills/parallel-code-tasks/" + source.relative_to(skill_root).as_posix()
        target = safe_path(root, relative)
        content = source.read_bytes()
        files[relative] = digest(content)
        if target.exists() and target.read_bytes() != content:
            require(update and previous["files"].get(relative) == digest(target.read_bytes()),
                    f"目标有不同或用户修改的内容，保留原文件：{relative}")
        if not target.exists() or target.read_bytes() != content:
            plan.append((target, content))
    config_path = safe_path(root, CONFIG_NAME)
    if not config_path.exists():
        plan.append((config_path, (json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n").encode("utf-8")))
    else:
        load_config(root)
    installed_rules = []
    for relative in dict.fromkeys(rules_files):
        require(relative.lower().endswith(".md"), "规则安装仅处理 Markdown 文件；其他产品格式需手动接入")
        target = safe_path(root, relative)
        require(not target.is_relative_to(destination) and target != config_path,
                "规则文件不能覆盖受管 Skill 或项目配置")
        raw = target.read_bytes() if target.exists() else b""
        text = raw.decode("utf-8-sig")
        newline = "\r\n" if b"\r\n" in raw else "\n"
        block = "\n".join([RULE_BEGIN, "## 多工具协作入口", "",
            "任何代码修改前，读取 .agents/skills/parallel-code-tasks/SKILL.md 并按统一脚本登记任务。",
            "所有编辑及命令在返回的独立 worktree 中进行；使用 bundle 携带任务信息并核实实际工作区。",
            "不直接修改原目录或其他任务；按协议验证、审查、回填，不擅自提交、推送或删除成果。", RULE_END]).replace("\n", newline)
        require(text.count(RULE_BEGIN) == text.count(RULE_END) <= 1, f"规则标记不完整或重复，保留：{relative}")
        if RULE_BEGIN in text:
            start, finish = text.index(RULE_BEGIN), text.index(RULE_END)
            require(start < finish, "规则标记顺序无效")
            end = finish + len(RULE_END)
            result = text[:start] + block + text[end:]
        else:
            result = text + (newline * 2 if text and not text.endswith(newline * 2) else "") + block + newline
        encoded = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + result.encode("utf-8")
        if encoded != raw:
            plan.append((target, encoded))
        installed_rules.append(relative)
    for target, content in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    atomic_json(manifest_path, {"version": 1, "files": files, "rules": installed_rules})
    return {"skill_path": str(destination), "rules": installed_rules,
            "changed_files": [str(path.relative_to(root)) for path, _ in plan],
            "message": "安装内容可审查；未创建提交。请按项目流程纳入基线，不能假定工具已读取规则"}
