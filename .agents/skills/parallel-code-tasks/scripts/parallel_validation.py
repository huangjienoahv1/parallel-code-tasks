"""候选环境与验证执行；状态和锁由主协议管理，仅使用标准库。"""

import json
import os
from pathlib import Path
import socket
import subprocess
import uuid

from parallel_support import DEFAULT_CONFIG, DEFAULT_VERIFICATION, atomic_json, digest, process_identity, require, safe_path, task_environment


def verification_config(task):
    return {**DEFAULT_VERIFICATION, **task.get("_profile", {}).get("verification", {})}


def provision_candidate(task, candidate, git):
    """从任务目录复制白名单的当前内容，跟踪文件只使用合并树。"""
    source, target = Path(task["path"]), Path(candidate["path"])
    copied = []
    for relative in task.get("_profile", DEFAULT_CONFIG)["copy_files"]:
        original, destination = safe_path(source, relative), safe_path(target, relative)
        if any(git(path, "ls-files", "--error-unmatch", "--", relative, check=False).returncode == 0
               for path in (source, target)):
            continue
        require(original.is_file(), f"任务白名单文件不存在：{relative}")
        require(all(git(path, "check-ignore", "--quiet", "--", relative, check=False).returncode == 0
                    for path in (source, target)), f"候选白名单文件必须在任务和候选中均被忽略：{relative}")
        content = original.read_bytes()
        if destination.exists():
            require(destination.is_file() and destination.read_bytes() == content,
                    f"候选白名单已有不同内容，保留：{relative}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
        copied.append({"path": relative, "sha256": digest(content)})
    environment = Path(task["workspace"]) / ("candidate-" + candidate["id"]) / "environment.json"
    values = task_environment({**task, "path": candidate["path"]})
    if environment.exists():
        require(json.loads(environment.read_text(encoding="utf-8")) == values,
                "候选环境文件已有不同内容，请重新 prepare，保留原现场")
    else:
        atomic_json(environment, values)
    candidate.update(copied_files=copied, environment_path=str(environment),
                     environment_sha256=digest(environment.read_bytes()), environment_keys=sorted(values))


def candidate_inputs(task, candidate, tree, git):
    """指纹只覆盖代码快照、冻结验证配置和受管环境，不展开环境值。"""
    environment = Path(candidate["environment_path"])
    require(not environment.is_symlink() and environment.is_file(), "候选环境文件缺失或为符号链接")
    raw = environment.read_bytes()
    require(digest(raw) == candidate["environment_sha256"], "候选环境文件改变，请重新 prepare 并验证")
    values = json.loads(raw)
    require(values == task_environment({**task, "path": candidate["path"]}), "候选环境与冻结配置不一致")
    files = []
    for copied in candidate["copied_files"]:
        relative = copied["path"]
        path = safe_path(candidate["path"], relative)
        require(path.is_file(), f"候选白名单文件缺失：{relative}")
        require(git(candidate["path"], "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0
                and git(candidate["path"], "check-ignore", "--quiet", "--", relative, check=False).returncode == 0,
                f"候选白名单文件跟踪/忽略状态改变，请重新 prepare：{relative}")
        files.append({"path": relative, "sha256": digest(path.read_bytes())})
    inputs = {"tree": tree, "candidate": candidate["id"], "environment_sha256": digest(raw),
              "files": files, "verification": verification_config(task)}
    fingerprint = digest(json.dumps(inputs, sort_keys=True, ensure_ascii=True).encode("utf-8"))
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("GIT_", "PARALLEL_"))}
    env.update(values)
    return fingerprint, env


def run_commands(task, candidate, record, env, save):
    """逐条执行；日志在候选源码外，超时/中断只终止本次直接子进程。"""
    config = verification_config(task)
    log_directory = Path(task["workspace"]) / ("candidate-" + candidate["id"]) / "verification" / record["id"]
    log_directory.mkdir(parents=True)
    for index, argv in enumerate(config["commands"]):
        log_path = log_directory / f"command-{index + 1}.log"
        step = {"number": index + 1, "program": Path(argv[0]).name,
                "command_sha256": digest(json.dumps(argv, ensure_ascii=True).encode("utf-8")),
                "log_path": str(log_path), "exit_code": None}
        record["steps"].append(step)
        record.pop("pid", None)
        save()
        with log_path.open("xb") as stream:
            try:
                process = subprocess.Popen(argv, cwd=candidate["path"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
            except OSError as error:
                step["failure_kind"] = type(error).__name__
                return 1
            try:
                record.update(pid=process.pid, host=socket.gethostname(), start=process_identity(process.pid)["start"])
                save()
                step["exit_code"] = process.wait(timeout=config["timeout_seconds"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                step["failure_kind"] = "timeout"
                return 1
            except BaseException:
                # 避免状态保存失败或 Ctrl+C 后继续留下本次直接验证进程。
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise
        if step["exit_code"] != 0:
            return step["exit_code"]
        save()
    return 0


def new_record(tree, fingerprint, started):
    return {"id": uuid.uuid4().hex, "status": "running", "tree": tree,
            "fingerprint": fingerprint, "started": started, "steps": []}
