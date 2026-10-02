#!/usr/bin/env python3
"""任意 AI 工具独立登记任务，以 worktree 隔离开发、逐个审查回填。"""

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import uuid

import parallel_support as support
from parallel_support import WorkflowError, atomic_json, digest, file_mutex, install_bundle, load_config, lock_info, owner_state, render, safe_path, task_environment


STATE_VERSION = 2
STATE_DIRECTORY = "parallel-code-tasks"
RESOURCE_TYPES = {"port", "db", "redis", "docker", "path"}
ACTION = "update"
ACTION_TASK = None


class TaskStatus(str, Enum):
    CREATING = "creating"
    WORKING = "working"
    READY = "ready"
    PREPARING = "preparing"
    CONFLICT = "conflict"
    PREPARED = "prepared"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARKED = "parked"
    RELEASED = "released"


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def git(path, *args, data=None, extra_env=None, check=True):
    """显式 cwd 和参数数组执行 Git，不继承可重定向仓库的环境变量。"""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(extra_env or {})
    result = subprocess.run(
        ["git", "-C", str(path), *args], input=data, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, check=False,
    )
    if check and result.returncode:
        raise WorkflowError(result.stderr.decode("utf-8", "replace").strip()
                            or f"Git 执行失败，退出码 {result.returncode}")
    return result


def git_text(path, *args, **kwargs):
    return git(path, *args, **kwargs).stdout.decode("utf-8", "surrogateescape").strip()


def repository(path):
    root = Path(git_text(path, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    return root, common


def branch(path):
    result = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    require(result.returncode == 0, "需要有独立分支的 worktree，不能使用 detached HEAD")
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def commit(path, ref):
    return git_text(path, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def clean(path):
    return not git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout


def unresolved(path):
    return bool(git(path, "ls-files", "-u", "-z").stdout)


def no_operation(path):
    """拒绝在其他 Git 流程尚未结束时启动新集成。"""
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        location = git_text(path, "rev-parse", "--path-format=absolute", "--git-path", marker)
        require(not Path(location).exists(), f"目录存在未完成的 Git 操作：{marker}")
    require(not unresolved(path), "存在尚未解决的 Git 冲突")


def supported_checkout(path):
    """临时索引快照要求完整普通 checkout，避免把稀疏未检出文件当成删除。"""
    sparse = git_text(path, "config", "--bool", "--get", "core.sparseCheckout", check=False)
    require(sparse != "true", "辅助脚本不支持 sparse checkout，请使用完整 worktree")
    require(not (Path(path) / ".gitmodules").exists(),
            "辅助脚本不管理 submodule；请使用原生工具并单独设计隔离流程")


def label(value):
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value)),
            "任务和身份须为小写字母开头、至多40字符的字母数字或连字符")
    return value


def scope(value):
    value = value.replace("\\", "/").rstrip("/")
    if value == ".":
        return value
    pieces = value.split("/")
    require(value and not value.startswith("/") and ":" not in value
            and not any(piece in {"", ".", "..", ".git"} for piece in pieces)
            and not any(char in value for char in "*?[]\n\r\0"),
            "scope 必须是具体仓库相对文件或目录，不支持 glob、.. 或 Git 元数据")
    return str(PurePosixPath(value))


def permitted(file, scopes):
    # Git 路径保持大小写；改名、删除等通过 --no-renames 检查两端路径。
    return any(item == "." or file == item or file.startswith(item + "/") for item in scopes)


def resource(value):
    kind, separator, name = value.partition(":")
    require(separator and kind in RESOURCE_TYPES and name.strip(), "资源格式为 port/db/redis/docker/path:值")
    name = name.strip()
    if kind == "port":
        require(name.isdigit() and 1 <= int(name) <= 65535, "port 必须在1到65535之间")
        name = str(int(name))
    elif kind == "path":
        location = Path(name)
        require(location.is_absolute(), "path 资源必须是绝对路径")
        name = os.path.normcase(str(location.resolve()))
    else:
        name = name.casefold()
    return kind + ":" + name


def collides(left, right):
    if left == right:
        return True
    if left.startswith("path:") and right.startswith("path:"):
        first, second = Path(left[5:]), Path(right[5:])
        return first in second.parents or second in first.parents
    return False


def read_state(directory):
    legacy = directory / "state.json"
    if legacy.exists():
        previous = json.loads(legacy.read_text(encoding="utf-8"))
        require(not previous.get("active"), "V1 批次仍在活动；先用原版本交付或人工归档，不能混用两套登记")
    path = directory / "project.json"
    if not path.exists():
        return {"version": STATE_VERSION, "revision": 0, "protocol": 3, "source": None, "pending": None, "tasks": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    require(state.get("version") == STATE_VERSION, "不支持的登记版本")
    state.setdefault("revision", 0)
    for task in state["tasks"].values():
        task.setdefault("revision", 0)
        task.setdefault("exclusive", [])
        task.setdefault("source_path", state["source"]["path"])
        task.setdefault("_profile", copy.deepcopy(support.DEFAULT_CONFIG))
    return state


def write_state(directory, state):
    state["revision"] = state.get("revision", 0) + 1
    state["protocol"] = 3
    temporary = directory / ("state-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "project.json")
        task = state["tasks"].get(ACTION_TASK, {})
        audit(directory, {"revision": state["revision"], "task": ACTION_TASK,
                          "status": task.get("status"), "candidate": task.get("candidate")})
    finally:
        if temporary.exists():
            temporary.unlink()


def audit(directory, details):
    event = {"id": uuid.uuid4().hex, "time": now(), "action": ACTION, **details}
    try:
        with (directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise WorkflowError(f"登记/操作可能已完成，但审计日志写入失败，请 doctor 核查：{error}") from error


@contextmanager
def locked(directory, ignore_legacy=False, wait=None):
    with file_mutex(directory / "mutex.lock", wait=wait):
        require(ignore_legacy or not (directory / "lock").exists(), "发现旧版目录锁，请 doctor 核查后 recover-lock，不混用版本")
        yield


def task_locked(directory, identity):
    return file_mutex(directory / "task-locks" / (label(identity) + ".lock"))


def snapshot(path, directory, working=True):
    """用临时索引快照当前内容，不改变真实索引或产生提交。"""
    require(not unresolved(path), "快照包含尚未解决的冲突")
    supported_checkout(path)
    temporary = directory / ("index-" + uuid.uuid4().hex)
    env = {"GIT_INDEX_FILE": str(temporary)}
    try:
        # 从真实索引的条目建立临时索引，保留显式暂存的新文件（包括 force 添加的忽略文件）。
        # 只读取条目，不复制真实索引的缓存标志，不写入真实索引。
        entries = git(path, "ls-files", "--stage", "-z").stdout
        git(path, "read-tree", "--empty", extra_env=env)
        git(path, "update-index", "-z", "--index-info", data=entries, extra_env=env)
        if working:
            git(path, "add", "-A", "--", ".", extra_env=env)
        return git_text(path, "write-tree", extra_env=env)
    finally:
        if temporary.exists():
            temporary.unlink()
        temporary_lock = Path(str(temporary) + ".lock")
        if temporary_lock.exists():
            temporary_lock.unlink()


def files_changed(path, base, tree):
    result = git(path, "diff", "--name-only", "--no-renames", "-z", base, tree, "--")
    return [name.decode("utf-8", "surrogateescape") for name in result.stdout.split(b"\0") if name]


def check_scope(path, base, tree, scopes):
    files = files_changed(path, base, tree)
    outside = [file for file in files if not permitted(file, scopes)]
    require(not outside, "修改超出 scope：" + ", ".join(outside))
    return files


def patch(path, base, tree):
    return git(path, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
               "--no-renames", base, tree, "--").stdout


def check_worktree(info, common):
    root, actual_common = repository(info["path"])
    require(str(root) == info["path"] and actual_common == common, "登记目录的 Git 归属已变化")
    require(branch(root) == info["branch"], "登记目录的分支已变化")
    return root


def pin(path, name, tree):
    """保留快照对象供跨时间协作，不创建提交；只使用本协议的引用命名空间。"""
    git(path, "update-ref", "refs/parallel-code-tasks/" + name, tree)


def observe_source(state, common, directory):
    source = state["source"]
    path = check_worktree(source, common)
    no_operation(path)
    tree = snapshot(path, directory)
    staged = snapshot(path, directory, working=False)
    untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return path, {"head": commit(path, "HEAD"), "tree": tree,
                  "staged": tree == staged and not untracked}


def refresh_source(state, common, directory):
    """接受干净的新提交或完整的已知回填成果，保留其他未登记修改。"""
    path, observed = observe_source(state, common, directory)
    source = state["source"]
    if state["pending"]:
        return path, False
    safe = clean(path) or (observed["tree"] == source["tree"] and observed["staged"])
    if safe:
        source.update(head=observed["head"], tree=observed["tree"])
        pin(path, "published", source["tree"])
        write_state(directory, state)
    return path, safe


def task_lookup(args, state):
    require(args.task in state["tasks"], "任务未登记，请先 enter")
    task = state["tasks"][args.task]
    require(args.token == task["token"], "任务凭据不匹配；不要接管其他工具的任务")
    return task


def overlap(left, right, ignore_case=False):
    if ignore_case:
        left, right = left.casefold(), right.casefold()
    return left == "." or right == "." or left == right or left.startswith(right + "/") or right.startswith(left + "/")


def reserve_resources(state, resources, excluding=None, exclusive=(), ignore_case=False):
    for task in state["tasks"].values():
        if task["id"] == excluding or not task["resources_reserved"]:
            continue
        require(not any(collides(left, right) for left in resources for right in task["resources"]),
                f"资源与任务 {task['id']} 冲突；先分配其他资源或由原执行者停止服务并 release")
        require(not any(overlap(left, right, ignore_case) for left in exclusive for right in task.get("exclusive", [])),
                f"独占文件认领与任务 {task['id']} 冲突；不因超时自动释放")


def check_claims(state, task, files):
    for other in state["tasks"].values():
        if other["id"] == task["id"] or not other["resources_reserved"]:
            continue
        blocked = [path for path in files if any(overlap(path, claim, state.get("ignore_case", False))
                                                for claim in other.get("exclusive", []))]
        require(not blocked, f"文件已由任务 {other['id']} 独占认领：" + ", ".join(blocked))


def seed_worktree(path, head, tree):
    committed_tree = git_text(path, "rev-parse", f"{head}^{{tree}}")
    if committed_tree != tree:
        changes = patch(path, committed_tree, tree)
        git(path, "apply", "--check", "--index", data=changes)
        git(path, "apply", "--index", data=changes)


def frozen(task, common, directory):
    path = check_worktree(task, common)
    no_operation(path)
    require(commit(path, "HEAD") == task["ready_head"]
            and snapshot(path, directory) == task["tree"],
            "ready 后任务又有修改；先 reopen、重新 ready 并审查")
    return path


def load_task(args, directory):
    with locked(directory):
        return copy.deepcopy(task_lookup(args, read_state(directory)))


def save_task(directory, task):
    """仅替换本任务；重新读取共享状态，保留同时完成的其他任务。"""
    with locked(directory):
        state = read_state(directory)
        require(state["tasks"][task["id"]]["revision"] == task["revision"], "任务登记已变化，保留现场后重新检查")
        task["revision"] += 1
        state["tasks"][task["id"]] = task
        write_state(directory, state)


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def provision(task):
    """仅复制白名单忽略文件，不复制未知源码；环境写在任务目录之外。"""
    source, target = Path(task["source_path"]), Path(task["path"])
    copied = []
    for relative in task["_profile"]["copy_files"]:
        original, destination = safe_path(source, relative), safe_path(target, relative)
        if git(source, "ls-files", "--error-unmatch", "--", relative, check=False).returncode == 0:
            copied.append({"path": relative, "mode": "tracked-baseline"})
            continue
        require(original.is_file(), f"白名单文件不存在或不是文件：{relative}")
        require(git(source, "check-ignore", "--quiet", "--", relative, check=False).returncode == 0
                and git(target, "check-ignore", "--quiet", "--", relative, check=False).returncode == 0,
                f"白名单复制仅允许源目录和任务目录都忽略的文件：{relative}")
        content = original.read_bytes()
        if destination.exists():
            require(destination.is_file() and destination.read_bytes() == content,
                    f"任务白名单文件已有不同内容，保留：{relative}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
        copied.append({"path": relative, "sha256": digest(content), "mode": "copy"})
    values = task_environment(task)
    environment = Path(task["workspace"]) / "environment.json"
    if environment.exists():
        require(json.loads(environment.read_text(encoding="utf-8")) == values,
                "任务环境文件已被修改，保留原文件，请核查配置")
    else:
        atomic_json(environment, values)
    task.update(copied_files=copied, environment_path=str(environment),
                environment_sha256=digest(environment.read_bytes()), environment_keys=sorted(values))


def resources_for(state, profile, requested, variables):
    resources = []
    for template in [*profile["resource_templates"], *requested]:
        value = render(template, variables)
        if value == "port:auto":
            selected = None
            for port in range(profile["port_range"][0], profile["port_range"][1] + 1):
                value = resource(f"port:{port}")
                if any(task["resources_reserved"] and value in task["resources"] for task in state["tasks"].values()):
                    continue
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError:
                        continue
                selected = value
                break
            require(selected, "配置端口区间没有可登记的空闲端口")
            value = selected
        value = resource(value)
        if value not in resources:
            resources.append(value)
    return resources


def enter(args, directory, root, common):
    identity, agent = label(args.task), label(args.agent)
    scopes = list(dict.fromkeys(scope(value) for value in args.scope))
    with locked(directory):
        state = read_state(directory)
        require(identity not in state["tasks"], "任务 ID 已存在；接续用原凭据，新任务使用新 ID")
        if state["source"] is None:
            no_operation(root)
            supported_checkout(root)
            head = commit(root, "HEAD")
            state["source"] = {"path": str(root), "branch": branch(root), "head": head,
                               "tree": git_text(root, "rev-parse", f"{head}^{{tree}}")}
            pin(root, "published", state["source"]["tree"])
            write_state(directory, state)
        source, safe = refresh_source(state, common, directory)
        known = state["source"]
        profile = load_config(source)
        state["ignore_case"] = git_text(source, "config", "--bool", "--get", "core.ignorecase", check=False) == "true"
        key = identity + "-" + uuid.uuid4().hex[:10]
        workspace = source.parent / (source.name + ".parallel") / key
        info = {"path": str(workspace / "work"), "branch": f"parallel/{key}/work", "managed_by": "cli"}
        if args.worktree:
            supplied, supplied_common = repository(args.worktree)
            require(supplied == Path(args.worktree).resolve() and supplied_common == common, "须提供同仓库 worktree 根目录")
            taken = [known, *state["tasks"].values()]
            taken.extend(candidate for task in state["tasks"].values() for candidate in task["candidates"])
            require(str(supplied) not in {item["path"] for item in taken}, "目录已被分配")
            require(branch(supplied) not in {item["branch"] for item in taken}, "分支已被分配")
            require(clean(supplied) and commit(supplied, "HEAD") == known["head"], "接入 worktree 须干净且 HEAD 等于登记基线")
            no_operation(supplied)
            supported_checkout(supplied)
            info = {"path": str(supplied), "branch": branch(supplied), "managed_by": "external"}
        variables = {"task": identity, "worktree": info["path"], "source": str(source)}
        resources = resources_for(state, profile, args.resource, variables)
        exclusive = [scope(value) for value in args.exclusive]
        exclusive.extend(scope(value) for value in profile["exclusive_paths"]
                         if any(overlap(scope(value), item, state["ignore_case"]) for item in scopes))
        exclusive = list(dict.fromkeys(exclusive))
        require(all(any(overlap(value, item, state["ignore_case"]) for item in scopes) for value in exclusive),
                "独占认领须与任务 scope 相关")
        reserve_resources(state, resources, exclusive=exclusive, ignore_case=state["ignore_case"])
        task = {"id": identity, "agent": agent, "token": uuid.uuid4().hex, "key": key, "revision": 0,
                "status": TaskStatus.CREATING, "goal": args.goal, "scope": scopes, "exclusive": exclusive,
                "resources": resources, "resources_reserved": True, "source_path": str(source), "_profile": profile,
                "baseline_head": known["head"], "baseline_tree": known["tree"], "workspace": str(workspace),
                "created": now(), "last_seen": now(), "candidates": [], **info}
        task_environment(task)
        state["tasks"][identity] = task
        write_state(directory, state)
        if not args.worktree:
            workspace.mkdir(parents=True)
            git(source, "worktree", "add", "-b", info["branch"], info["path"], known["head"])
    seed_worktree(task["path"], task["baseline_head"], task["baseline_tree"])
    provision(task)
    pin(source, f"tasks/{key}/baseline", task["baseline_tree"])
    task["status"] = TaskStatus.WORKING
    save_task(directory, task)
    return {"task": task, "source_ready_for_publish": safe, "handoff": bundle_data(task),
            "message": "在返回的 path 打开工具，或用 launch 指定 CLI；没有创建提交"}


def guard(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] == TaskStatus.WORKING, "任务不可开发；暂停先 resume，冻结先 reopen")
    path = check_worktree(task, common)
    require(root == path and Path.cwd().resolve().is_relative_to(path), "真实 cwd 不是任务 worktree，请核实产品实际工作区")
    no_operation(path)
    require(git(path, "merge-base", "--is-ancestor", task["baseline_head"], "HEAD", check=False).returncode == 0,
            "任务 HEAD 已丢失登记基线")
    tree = snapshot(path, directory)
    files = check_scope(path, task["baseline_tree"], tree, task["scope"])
    with locked(directory):
        state = read_state(directory)
        check_claims(state, task, files)
        newer = task["baseline_tree"] != state["source"]["tree"]
    task["last_seen"] = now()
    save_task(directory, task)
    return {"task": task, "newer_shared_result": newer, "message": "目录及范围检查通过；不自动拉取新成果"}


def ready(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] in {TaskStatus.WORKING, TaskStatus.READY}, "任务状态不允许 ready")
    path = check_worktree(task, common)
    no_operation(path)
    require(git(path, "merge-base", "--is-ancestor", task["baseline_head"], "HEAD", check=False).returncode == 0,
            "任务 HEAD 已丢失登记基线")
    tree = snapshot(path, directory)
    files = check_scope(path, task["baseline_tree"], tree, task["scope"])
    require(files, "任务没有可交付改动")
    with locked(directory):
        check_claims(read_state(directory), task, files)
    pin(path, f"tasks/{task['key']}/ready", tree)
    task.update(status=TaskStatus.READY, tree=tree, ready_head=commit(path, "HEAD"), files=files, ready_at=now())
    save_task(directory, task)
    return task


def reopen(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] in {TaskStatus.READY, TaskStatus.PREPARED, TaskStatus.CONFLICT, TaskStatus.PREPARING}, "任务不能重新开发")
    require(args.workers_stopped, "先停止旧执行者和候选写入，再 --workers-stopped")
    check_worktree(task, common)
    task.update(status=TaskStatus.WORKING, last_seen=now())
    task.pop("candidate", None)
    save_task(directory, task)
    return task


def candidate_lookup(task, common):
    require(task.get("candidate"), "没有当前候选结果，请 prepare")
    candidate = next(item for item in task["candidates"] if item["id"] == task["candidate"])
    path = check_worktree(candidate, common)
    require(commit(path, "HEAD") == candidate["source_head"], "候选 HEAD 已变化，不能混用提交和快照流程")
    supported_checkout(path)
    return candidate, path


def staged_candidate(task, common, directory):
    candidate, path = candidate_lookup(task, common)
    no_operation(path)
    tree = snapshot(path, directory)
    require(snapshot(path, directory, working=False) == tree
            and not git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout,
            "候选有未暂存修改或新文件；显式暂存后重新验证")
    files = check_scope(path, candidate["source_tree"], tree, task["scope"])
    return candidate, path, tree, files


def prepare(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] in {TaskStatus.READY, TaskStatus.PREPARED, TaskStatus.CONFLICT, TaskStatus.PREPARING}, "任务尚未 ready 或已回填")
    require(args.reviewed_tree == task["tree"], "任务审查 SHA 已过期")
    path = frozen(task, common, directory)
    with locked(directory):
        state = read_state(directory)
        require(not state["pending"], "有中断回填，请先 recover")
        check_claims(state, task, task["files"])
        source, safe = refresh_source(state, common, directory)
        require(safe, "原目录有未知修改，保留原工作；请原写入者处理后再 prepare")
        known = state["source"]
        attempt = uuid.uuid4().hex[:10]
        candidate = {"id": attempt, "path": str(Path(task["workspace"]) / ("publish-" + attempt)),
                     "branch": f"parallel/{task['key']}/publish-{attempt}", "source_head": known["head"],
                     "source_tree": known["tree"], "created": now(), "managed_by": "cli"}
        task["candidates"].append(candidate)
        task.update(status=TaskStatus.PREPARING, candidate=attempt, revision=task["revision"] + 1)
        state["tasks"][task["id"]] = task
        write_state(directory, state)
        Path(task["workspace"]).mkdir(parents=True, exist_ok=True)
        git(source, "worktree", "add", "-b", candidate["branch"], candidate["path"], known["head"])
    # 独立目录的耗时应用仅持本任务锁；其他任务可以准备或登记。
    seed_worktree(candidate["path"], candidate["source_head"], candidate["source_tree"])
    result = git(candidate["path"], "apply", "--3way", "--index",
                 data=patch(path, task["baseline_tree"], task["tree"]), check=False)
    if result.returncode:
        task["status"] = TaskStatus.CONFLICT if unresolved(candidate["path"]) else TaskStatus.PREPARING
        candidate["error"] = result.stderr.decode("utf-8", "replace").strip()
        save_task(directory, task)
        raise WorkflowError(f"候选集成失败，现场保留在 {candidate['path']}：{candidate['error']}")
    _, _, tree, files = staged_candidate(task, common, directory)
    pin(source, f"tasks/{task['key']}/candidate-{attempt}", tree)
    candidate.update(tree=tree, files=files)
    task["status"] = TaskStatus.PREPARED
    save_task(directory, task)
    return {"task": task["id"], "candidate": candidate, "message": "验证候选并审查此 tree 后 publish"}


def inspect(args, directory, root, common):
    task = load_task(args, directory)
    candidate, path = candidate_lookup(task, common)
    result = {"task": task["id"], "status": task["status"], "candidate": candidate,
              "git_status": git_text(path, "status", "--short"), "unresolved": unresolved(path)}
    if not result["unresolved"]:
        result["actual_tree"] = snapshot(path, directory)
        result["actual_index_tree"] = snapshot(path, directory, working=False)
        result["files"] = files_changed(path, candidate["source_tree"], result["actual_tree"])
    return result


def resolve(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] == TaskStatus.CONFLICT, "只有实际冲突可 resolve；应用中断请 recover")
    frozen(task, common, directory)
    candidate, path, tree, files = staged_candidate(task, common, directory)
    require(args.reviewed_tree == tree, "冲突解决成果与审查 SHA 不一致")
    with locked(directory):
        check_claims(read_state(directory), task, files)
    pin(path, f"tasks/{task['key']}/candidate-{candidate['id']}", tree)
    candidate.update(tree=tree, files=files, resolved_at=now())
    task["status"] = TaskStatus.PREPARED
    save_task(directory, task)
    return {"task": task["id"], "candidate": candidate}


def finish_publish(task, state, directory, path, tree):
    state["source"].update(head=commit(path, "HEAD"), tree=tree)
    pin(path, "published", tree)
    task.update(status=TaskStatus.PUBLISHED, published_at=now(), revision=task["revision"] + 1)
    state["tasks"][task["id"]] = task
    state["pending"] = None
    write_state(directory, state)
    return {"task": task["id"], "status": task["status"], "source": state["source"],
            "message": "成果回填原目录暂存区，没有 commit；停止全部服务后 release"}


def publish(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] == TaskStatus.PREPARED, "没有准备好的候选")
    frozen(task, common, directory)
    candidate, _, tree, files = staged_candidate(task, common, directory)
    require(args.reviewed_tree == tree == candidate["tree"], "候选已变化，请重新 inspect、验证并审查")
    with locked(directory):
        state = read_state(directory)
        require(not state["pending"], "有中断回填，请先 recover")
        check_claims(state, task, files)
        source, safe = refresh_source(state, common, directory)
        require(safe, "原目录有未知修改，保留工作；处理后重新 prepare")
        known = state["source"]
        require(known["head"] == candidate["source_head"] and known["tree"] == candidate["source_tree"],
                "候选基线已过期；重新 prepare 并审查新结果")
        changes = patch(source, known["tree"], tree)
        if changes:
            git(source, "apply", "--check", "--index", data=changes)
        state["pending"] = {"task": task["id"], "source_head": known["head"], "before_tree": known["tree"],
                            "after_tree": tree, "started": now()}
        task.update(status=TaskStatus.PUBLISHING, revision=task["revision"] + 1)
        state["tasks"][task["id"]] = task
        write_state(directory, state)
        if changes:
            git(source, "apply", "--index", data=changes)
        _, observed = observe_source(state, common, directory)
        require(observed["head"] == candidate["source_head"] and observed["tree"] == tree and observed["staged"],
                "回填后校验失败，保留现场；执行 recover 核查")
        return finish_publish(task, state, directory, source, tree)


def recover(args, directory, root, common):
    task = load_task(args, directory)
    if task["status"] == TaskStatus.CREATING:
        require(args.workers_stopped, "创建恢复前停止该任务写入，再 --workers-stopped")
        path = Path(task["path"])
        if not path.exists() or (path.is_dir() and not any(path.iterdir())):
            require(task["managed_by"] == "cli", "外部 worktree 缺失，请由原工具恢复")
            with locked(directory):
                source = Path(task["source_path"])
                existing = git(source, "rev-parse", "--verify", "refs/heads/" + task["branch"], check=False)
                if existing.returncode == 0:
                    require(existing.stdout.decode().strip() == task["baseline_head"], "已有任务分支 HEAD 不同，保留现场")
                    git(source, "worktree", "add", str(path), task["branch"])
                else:
                    git(source, "worktree", "add", "-b", task["branch"], str(path), task["baseline_head"])
        path = check_worktree(task, common)
        no_operation(path)
        require(commit(path, "HEAD") == task["baseline_head"], "创建中断后 HEAD 变化，保留现场")
        committed_tree = git_text(path, "rev-parse", "HEAD^{tree}")
        actual = snapshot(path, directory)
        require(actual in {committed_tree, task["baseline_tree"]}
                and snapshot(path, directory, working=False) == actual
                and not git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout,
                "创建中断后存在未知修改，保留全部文件，不强制覆盖")
        if actual == committed_tree:
            seed_worktree(path, task["baseline_head"], task["baseline_tree"])
        provision(task)
        pin(path, f"tasks/{task['key']}/baseline", task["baseline_tree"])
        task["status"] = TaskStatus.WORKING
        save_task(directory, task)
        return task
    if task["status"] == TaskStatus.PREPARING:
        require(args.workers_stopped, "准备恢复前停止任务及候选写入，再 --workers-stopped")
        candidate = next(item for item in task["candidates"] if item["id"] == task["candidate"])
        if Path(candidate["path"]).exists() and unresolved(candidate["path"]):
            candidate_lookup(task, common)
            task["status"] = TaskStatus.CONFLICT
            save_task(directory, task)
            return {"task": task["id"], "candidate": candidate, "status": task["status"]}
        require(args.reviewed_tree == task["tree"], "准备中断无法确认成功；需 --reviewed-tree <任务tree> 创建新候选")
        return prepare(args, directory, root, common)
    launch_record = task.get("launch")
    if task["status"] == TaskStatus.WORKING and launch_record and launch_record["status"] in {"starting", "running", "interrupted"}:
        require(args.workers_stopped and args.expected_launch == launch_record["id"],
                "启动恢复需核查记录及全部子进程已停止，再 --workers-stopped --expected-launch <launch.id>")
        require(not launch_record.get("pid") or owner_state(launch_record) == "missing",
                "登记启动进程仍存在或无法确认已退出，保留记录")
        # 没有 PID 的启动窗口只能依赖执行者确认；不清理目录，也不释放资源。
        launch_record.update(status="abandoned", ended=now(), stopped_confirmed=True)
        save_task(directory, task)
        return {"task": task["id"], "launch": launch_record, "message": "启动登记已结束，成果与资源保留；没有停止任何进程"}
    with locked(directory):
        state = read_state(directory)
        pending = state["pending"]
        require(pending and pending["task"] == task["id"], "任务没有待恢复回填")
        source, observed = observe_source(state, common, directory)
        require(observed["head"] == pending["source_head"] and observed["staged"],
                "中断后 HEAD/暂存区发生额外变化，保留现场")
        if observed["tree"] == pending["after_tree"]:
            return finish_publish(task, state, directory, source, pending["after_tree"])
        require(observed["tree"] == pending["before_tree"], "中断后有额外修改，保留现场，不盲目重放")
        state["pending"] = None
        task.update(status=TaskStatus.PREPARED, revision=task["revision"] + 1)
        state["tasks"][task["id"]] = task
        write_state(directory, state)
        return {"task": task["id"], "status": task["status"], "message": "尚未应用，恢复 prepared；重新审查后 publish"}


def release(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] not in {TaskStatus.CREATING, TaskStatus.PUBLISHING, TaskStatus.PARKED, TaskStatus.RELEASED},
            "状态不允许释放；过渡状态先恢复")
    require(args.workers_stopped, "先停止全部写入者及服务，再 --workers-stopped")
    task["parked_from"] = task["status"]
    task.update(status=TaskStatus.RELEASED if task["status"] == TaskStatus.PUBLISHED else TaskStatus.PARKED,
                resources_reserved=False, released_at=now())
    save_task(directory, task)
    return {"task": task, "message": "资源登记已释放，目录成果保留；没有停止外部进程"}


def resume(args, directory, root, common):
    task = load_task(args, directory)
    require(task["status"] == TaskStatus.PARKED, "仅暂停的未回填任务可 resume")
    require(args.workers_stopped, "确认没有旧执行者运行，再 --workers-stopped")
    check_worktree(task, common)
    with locked(directory):
        state = read_state(directory)
        reserve_resources(state, task["resources"], excluding=task["id"], exclusive=task["exclusive"],
                          ignore_case=state.get("ignore_case", False))
        task.update(status=task.pop("parked_from"), resources_reserved=True, last_seen=now(), revision=task["revision"] + 1)
        state["tasks"][task["id"]] = task
        write_state(directory, state)
    return {"task": task, "message": "接续原目录与基线，不自动拉取其他成果"}


def bundle_data(task):
    script = str(Path(__file__).resolve())
    skill_root = Path(script).parents[1]
    prompt = "\n".join([
        "按多工具协作协议接续此任务，不重新 enter，也不接管其他任务。",
        f"先读取 {skill_root / 'SKILL.md'}，以及原项目 {task['source_path']} 的 AGENTS/README 和实际生效规则。",
        f"所有读写和命令的真实工作区：{task['path']}",
        f"任务：{task['id']}；工具标签：{task['agent']}；接续码：{task['token']}",
        f"分支：{task['branch']}；基线 HEAD：{task['baseline_head']}；基线 tree：{task['baseline_tree']}",
        f"目标：{task.get('goal') or '沿用原任务目标；开始前补充具体需求和验收标准'}",
        f"范围：{', '.join(task['scope'])}；独占：{', '.join(task['exclusive']) or '无'}",
        f"登记资源：{', '.join(task['resources']) or '无'}",
        f"统一脚本：{script}",
        f"在任务目录 guard --task {task['id']} --token {task['token']}，确认实际编辑器也以此目录为根。",
        "若状态已暂停先 resume；已冻结要继续修改先 reopen。验证后 ready，审查任务树，prepare，验证及审查候选，publish。",
        "不擅自提交、推送、部署或删除成果；停止本任务全部写入者和服务后才 release --workers-stopped。",
        "图形工具启动不会自动注入任务环境；读取环境文件并按实际项目启动方式使用，勿公开环境值。",
    ])
    return {"cwd": task["path"], "script": script, "prompt": prompt,
            "environment_path": task.get("environment_path"), "environment_keys": task.get("environment_keys", [])}


def bundle(args, directory, root, common):
    task = copy.deepcopy(task_lookup(args, read_state(directory)))
    check_worktree(task, common)
    data = bundle_data(task)
    return data["prompt"] if args.format == "text" else data


def launch(args, directory, root, common):
    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    require(argv, "launch 需要 -- <真实可执行程序> <参数>；不接受 shell 命令字符串")
    with task_locked(directory, args.task):
        task = load_task(args, directory)
        require(task["status"] == TaskStatus.WORKING, "只能在 working 任务启动写入者")
        path = check_worktree(task, common)
        previous = task.get("launch")
        if previous and previous["status"] in {"starting", "running", "interrupted"}:
            require(previous.get("pid") and owner_state(previous) == "missing",
                    "前次启动进程仍运行或无法确认已退出，请停止并核查，不能重复启动")
        if not task.get("environment_path"):
            provision(task)
        environment = Path(task["environment_path"])
        require(environment.exists() and digest(environment.read_bytes()) == task["environment_sha256"],
                "任务环境文件被修改或缺失，保留现场，请核查")
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(json.loads(environment.read_text(encoding="utf-8")))
        launch_id = uuid.uuid4().hex
        task["launch"] = {"id": launch_id, "status": "starting", "pid": None, "start": None,
                          "host": socket.gethostname(), "program": Path(argv[0]).name, "started": now()}
        save_task(directory, task)
        try:
            process = subprocess.Popen(argv, cwd=path, env=env)
        except OSError:
            task["launch"].update(status="failed", ended=now())
            save_task(directory, task)
            raise
        task["launch"].update(status="running", pid=process.pid, start=support.process_identity(process.pid)["start"])
        save_task(directory, task)
    interrupted = False
    try:
        exit_code = process.wait()
    except KeyboardInterrupt:
        interrupted, exit_code = True, process.poll()
    # 等待期间不持任务锁，CLI 可在自己的目录调用 guard/ready。
    with task_locked(directory, args.task):
        task = load_task(args, directory)
        require(task.get("launch", {}).get("id") == launch_id, "启动记录发生变化，请 doctor 核查")
        task["launch"].update(status="interrupted" if interrupted else "exited", exit_code=exit_code, ended=now())
        save_task(directory, task)
    require(not interrupted, "启动等待被中断，不保证子进程/服务已停止；请核查后继续，不自动释放资源")
    return {"task": task["id"], "cwd": str(path), "exit_code": exit_code,
            "message": "程序退出码已记录；没有自动释放资源、提交或回填"}


NEXT = {"creating": "doctor；停止写入后 recover --workers-stopped", "working": "guard 后开发/验证，再 ready",
        "ready": "审查任务 tree 后 prepare", "preparing": "doctor；停止写入后 recover --workers-stopped --reviewed-tree <任务tree>",
        "conflict": "在候选解决并暂存；inspect、验证、审查后 resolve", "prepared": "验证、审查候选后 publish；过期先重新 prepare",
        "publishing": "recover 核查回填现场", "published": "停止全部服务后 release --workers-stopped",
        "parked": "确认旧写入者已停止后 resume --workers-stopped", "released": "成果保留；新需求新建任务"}


def status(args, directory, root, common):
    state = read_state(directory)
    tasks = [{**{key: value for key, value in public(task).items() if key != "token"},
              "next_action": NEXT.get(task["status"], "doctor 核查")} for task in state["tasks"].values()]
    data = {"repository": str(root), "common": str(common), "state_file": str(directory / "project.json"),
            "revision": state["revision"], "locked": (directory / "lock").exists() or lock_info(directory / "mutex.lock")["held"],
            "source": state["source"], "pending": state["pending"], "tasks": tasks}
    if args.format == "table":
        lines = ["任务 | 工具 | 状态 | 保留资源/独占 | 下一步", "--- | --- | --- | --- | ---"]
        for task in tasks:
            held = ", ".join([*task["resources"], *task["exclusive"]]) if task["resources_reserved"] else "已释放"
            lines.extend([f"{task['id']} | {task['agent']} | {task['status']} | {held or '无'} | {task['next_action']}",
                          f"  目录：{task['path']}"])
        if not tasks:
            lines.append("尚无任务；先 enter")
        return "\n".join(lines)
    return data


def events(args, directory, root, common):
    records, invalid = [], []
    path = directory / "events.jsonl"
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                require(isinstance(record, dict), "审计行不是对象")
                records.append(record)
            except (ValueError, WorkflowError):
                invalid.append(number)
    return {"events": records[-args.limit:], "invalid_lines": invalid, "log_path": str(path)}


def doctor(args, directory, root, common):
    state = read_state(directory)
    findings = []
    if state["source"]:
        try:
            path = check_worktree(state["source"], common)
            no_operation(path)
            if not clean(path):
                findings.append({"task": None, "kind": "source-dirty", "message": "原目录有改动；可能是已知暂存回填，prepare/publish 会进一步核对"})
        except (WorkflowError, OSError) as error:
            findings.append({"task": None, "kind": "source-error", "message": str(error)})
    for task in state["tasks"].values():
        try:
            check_worktree(task, common)
            environment = task.get("environment_path")
            if environment:
                require(Path(environment).is_file() and digest(Path(environment).read_bytes()) == task["environment_sha256"],
                        "任务环境文件缺失或改变，保留现场")
            if task.get("candidate"):
                candidate, path = candidate_lookup(task, common)
                if unresolved(path):
                    findings.append({"task": task["id"], "kind": "candidate-conflict", "message": f"冲突保留于 {candidate['path']}"})
        except (WorkflowError, OSError) as error:
            findings.append({"task": task["id"], "kind": "task-error", "message": str(error)})
        if task["status"] in {TaskStatus.CREATING, TaskStatus.PREPARING, TaskStatus.CONFLICT, TaskStatus.PUBLISHING}:
            findings.append({"task": task["id"], "kind": task["status"], "message": NEXT[task["status"]]})
        launch_record = task.get("launch")
        if launch_record and launch_record["status"] in {"starting", "running", "interrupted"}:
            findings.append({"task": task["id"], "kind": "launch", "process_state": owner_state(launch_record),
                             "launch": launch_record,
                             "message": "核查该启动及其子进程；不能仅由 PID 状态证明全部服务已停止"})
    legacy = None
    owner_file = directory / "lock" / "owner.json"
    if (directory / "lock").exists():
        raw = owner_file.read_bytes() if owner_file.is_file() else None
        owner = json.loads(raw) if raw else None
        legacy = {"path": str(owner_file.parent), "owner": owner, "fingerprint": digest(raw) if raw else None,
                  "process_state": owner_state(owner) if owner else "unknown"}
        findings.append({"task": None, "kind": "legacy-lock", "message": "停止旧脚本并核实该 owner；仅确认为失效的记录可 recover-lock"})
    logs = events(argparse.Namespace(limit=1), directory, root, common)
    if logs["invalid_lines"]:
        findings.append({"task": None, "kind": "audit-error", "lines": logs["invalid_lines"], "message": "日志存在损坏行，保留核查"})
    return {"read_only": True, "findings": findings, "legacy_lock": legacy,
            "global_lock": lock_info(directory / "mutex.lock"),
            "task_locks": [lock_info(path) for path in sorted((directory / "task-locks").glob("*.lock"))],
            "pending": state["pending"], "message": "未修复、未删除文件；锁探测不改变登记"}


def recover_lock(args, directory, root, common):
    require(args.workers_stopped, "先停止旧版脚本及其写入者，再 --workers-stopped")
    with locked(directory, ignore_legacy=True):
        legacy = directory / "lock"
        owner_file = legacy / "owner.json"
        require(not legacy.is_symlink() and owner_file.is_file() and not owner_file.is_symlink(), "缺少可核实的旧锁 owner，保留现场")
        require({item.name for item in legacy.iterdir()} == {"owner.json"}, "旧锁含未知内容，保留现场")
        raw = owner_file.read_bytes()
        require(digest(raw) == args.expected_owner, "锁 owner 已变化或指纹错误，不能移除")
        require(owner_state(json.loads(raw)) == "missing", "旧锁进程仍存在、主机不同或无法确认，不能移除")
        require(owner_file.read_bytes() == raw, "锁记录发生变化，不能移除")
        owner_file.unlink()
        legacy.rmdir()
        audit(directory, {"legacy_owner_fingerprint": args.expected_owner, "result": "recovered"})
    return {"message": "仅移除已核实失效的旧 owner 和空 lock 目录，登记和成果保留"}


def install(args, directory, root, common):
    with locked(directory):
        result = install_bundle(root, directory, Path(__file__).resolve().parents[1], args.rules_file or ["AGENTS.md"], args.update)
        audit(directory, {"changed_files": result["changed_files"], "result": "installed"})
        return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", default=".", help="同仓库任意 worktree；首次 enter 使用原项目")
    result.add_argument("--lock-wait", type=float, default=5.0, help="有限等待秒数（0..30），不是抢锁期限")
    sub = result.add_subparsers(dest="command", required=True)
    installation = sub.add_parser("install", help="幂等安装项目 Skill、短规则和默认配置，不提交")
    installation.add_argument("--rules-file", action="append", help="仓库相对 Markdown 规则文件；默认 AGENTS.md")
    installation.add_argument("--update", action="store_true", help="仅更新上次安装且未被用户修改的 Skill 文件")
    admission = sub.add_parser("enter", help="独立加入，无需统一协调者")
    admission.add_argument("--task", required=True)
    admission.add_argument("--agent", required=True)
    admission.add_argument("--scope", action="append", required=True)
    admission.add_argument("--resource", action="append", default=[])
    admission.add_argument("--exclusive", action="append", default=[])
    admission.add_argument("--goal", default="")
    admission.add_argument("--worktree")
    for name in ("guard", "ready", "reopen", "prepare", "inspect", "resolve", "publish", "recover", "release", "resume", "bundle", "launch"):
        command = sub.add_parser(name)
        command.add_argument("--task", required=True)
        command.add_argument("--token", required=True, help="任务接续码，不是安全认证")
        if name in {"prepare", "resolve", "publish"}:
            command.add_argument("--reviewed-tree", required=True)
        if name == "recover":
            command.add_argument("--reviewed-tree")
            command.add_argument("--expected-launch", help="核查并停止全部子进程后，确认 doctor 给出的 launch.id")
        if name in {"reopen", "release", "resume", "recover"}:
            command.add_argument("--workers-stopped", action="store_true")
        if name == "bundle":
            command.add_argument("--format", choices=("json", "text"), default="json")
        if name == "launch":
            command.add_argument("argv", nargs=argparse.REMAINDER)
    view = sub.add_parser("status")
    view.add_argument("--format", choices=("json", "table"), default="json")
    sub.add_parser("doctor", help="只读诊断锁、任务、候选、环境和中断")
    history = sub.add_parser("events")
    history.add_argument("--limit", type=int, default=20)
    recovery = sub.add_parser("recover-lock")
    recovery.add_argument("--expected-owner", required=True, help="doctor 返回的旧 owner 指纹")
    recovery.add_argument("--workers-stopped", action="store_true")
    return result


def main():
    global ACTION, ACTION_TASK
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    ACTION, ACTION_TASK = args.command, getattr(args, "task", None)
    directory = None
    try:
        require(0 <= args.lock_wait <= 30, "lock-wait 必须在0..30秒之间")
        if args.command == "events":
            require(1 <= args.limit <= 1000, "limit 必须在1..1000之间")
        support.LOCK_WAIT = args.lock_wait
        root, common = repository(args.repo)
        directory = common / STATE_DIRECTORY
        if args.command in {"status", "doctor", "events", "bundle", "install", "recover-lock", "launch"}:
            output = globals()[args.command.replace("-", "_")](args, directory, root, common)
        else:
            with task_locked(directory, args.task):
                output = globals()[args.command](args, directory, root, common)
        print(output if isinstance(output, str) else json.dumps(public(output), ensure_ascii=True, indent=2))
        return output["exit_code"] if args.command == "launch" else 0
    except (WorkflowError, OSError, ValueError) as error:
        details = {"error": str(error), "command": args.command}
        if directory and args.command not in {"status", "doctor", "events", "bundle"}:
            try:
                with locked(directory, wait=0):
                    audit(directory, {"task": ACTION_TASK, "result": "failed", "error_type": type(error).__name__})
            except (WorkflowError, OSError, ValueError) as audit_error:
                details["audit_warning"] = str(audit_error)
        print(json.dumps(details, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
