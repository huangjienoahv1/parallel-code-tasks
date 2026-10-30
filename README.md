# 多工具共用项目 · parallel-code-tasks

让 Codex、智谱 ZCode、OpenCode、Claude Code、Antigravity 或其他编程工具，同时或先后开发**同一个本地 Git 仓库**。同一工具的多个任务也适用。

采用 **短项目规则 + Skill 工作流程 + Python/Git 脚本**：每个任务一个 worktree 和分支，共享持久登记，独立审查，逐个回填。不要求统一协调 AI，不等待所有任务结束，不自动提交代码。

**第一次使用请阅读 [小白使用手册](.agents/skills/parallel-code-tasks/references/beginner-guide.md)**：从 Git/worktree 概念、环境准备和安装开始，提供 AI 提示词与手动操作两种路线，详细说明交付、暂停接续、出错恢复、成果备份和 worktree 清理。`release` 不删除目录；清理前必须确认成果保存，当前没有自动删除协议历史的命令。

## 工作方式

~~~text
C:/code/project/                        原目录：查看成果、接收已审查回填
C:/code/project.parallel/
  login-<随机ID>/work/                  工具A的登录任务
  report-<随机ID>/work/                 工具B的报表任务
  fix-<随机ID>/work/                    任意工具的独立任务
  <任务ID-随机ID>/publish-<随机ID>/     每次交付的独立候选
~~~

这些目录共享 Git 历史，各自拥有源码、HEAD 和索引；共享登记存放在 Git common directory。一个任务写文件不会改变另一任务工作目录。同文件并行修改在交付时三方合并，业务兼容仍需检查。[Git worktree 官方说明](https://git-scm.com/docs/git-worktree)

~~~mermaid
flowchart LR
  A[任意工具 enter] --> B[独立 worktree 开发]
  B --> C[验证 ready 审查]
  C --> D[独立候选 prepare]
  D --> E[处理冲突 验证 审查]
  E --> F[串行 publish]
  F --> G[原目录暂存成果]
  G --> A
~~~

工具必须**实际打开自己的 worktree**。如果全部工具坚持直接写同一物理目录，Skill 无法防止覆盖。只在终端 cd 不代表编辑器切换了项目；强制限制需要产品沙箱、目录权限或容器。

## 安装到你的项目

需要 Git、Python 3.9+，只使用标准库，无第三方包。Windows 命令使用 PowerShell 7 的 `pwsh.exe`。当前行为验证在 Windows 完成，其他系统仍需实际验证。

先克隆本仓库到工具目录，再安装到**已有 Git 项目**。在 PowerShell 7 中：

~~~powershell
git clone https://github.com/huangjienoahv1/parallel-code-tasks.git C:/tools/parallel-code-tasks
$parallelScript = 'C:/tools/parallel-code-tasks/.agents/skills/parallel-code-tasks/scripts/parallel_tasks.py'
$projectPath = 'C:/code/你的项目'
python $parallelScript --repo $projectPath install
~~~

安装完整 Skill 到目标 `.agents/skills/parallel-code-tasks`，保留原 AGENTS.md 内容并追加短入口规则，创建默认 `.parallel-code-tasks.json`。已有配置不覆盖；重复安装不重复规则。需要多个实际 Markdown 规则入口时：

~~~powershell
python $parallelScript --repo $projectPath install --rules-file AGENTS.md --rules-file CLAUDE.md
~~~

不支持 Markdown 规则文件的产品，使用 [通用提示词](.agents/skills/parallel-code-tasks/references/agent-prompts.md)。安装不修改任何工具全局配置，不自动创建聊天，也不代表产品已读取规则。

**先审查安装文件，按项目流程授权纳入 Git 基线，再开始并行任务。** 未提交规则在新 worktree 中可能缺失，通过原目录绝对路径读取；未知安装改动会阻止交付回填。脚本不替你自动 commit。

升级先让所有参与者停止旧协议命令，使用新版来源执行 `install --update`；仅覆盖上次安装后未被用户修改的 Skill 文件，用户改动保留并明确报错。不混用旧目录锁版本与当前内核锁版本。

## 日常使用

你可以分别向任意工具发送：

~~~text
阅读 C:/code/你的项目/.agents/skills/parallel-code-tasks/SKILL.md，按通用协作协议开发。
原项目 C:/code/你的项目；工具标签 codex；新任务 login-fix-1。
需求：修复登录过期提示，保持已有接口；范围 src/login。
先登记，在返回的真实 worktree 开发和验证，逐个审查回填，不自动提交。
~~~

其他工具换自己的标签、唯一任务 ID、需求和范围即可，例如 zcode/report-1、opencode/import-1、cc/payment-1。任意产品名仅是标签；每个新任务使用新 ID，每个任务只能有一个实际写入者。

如果由你执行登记，使用：

~~~powershell
python $parallelScript --repo $projectPath status --format table
$entry = python $parallelScript --repo $projectPath enter --agent codex --task login-fix-1 --goal '修复登录过期提示，保持接口不变' --scope src/login --resource port:auto
if ($LASTEXITCODE -ne 0) { throw '登记失败，请查看错误' }
$task = ($entry | ConvertFrom-Json).task
$task | Select-Object id, path, branch, baseline_head, baseline_tree, token
python $parallelScript --repo $projectPath bundle --task $task.id --token $task.token --format text
~~~

在图形产品里打开 `$task.path`，将 bundle 生成的自包含提示词粘贴给开发会话。它包含实际目录、协议路径、目标、基线、scope、认领和资源。图形工具的环境需要按实际启动方式显式使用任务环境文件。

对于 CLI，包装命令会设置真实 cwd、注入任务环境、记录进程及退出码。替换为本机实际可执行入口，产品的提示参数由你或 AI 按本机使用方式添加：

~~~powershell
$toolExecutable = 'C:/tools/your-agent/agent.exe'
python $parallelScript --repo $projectPath launch --task $task.id --token $task.token -- $toolExecutable
~~~

launch 不自动发送任务提示，也不验证该产品所有编辑工具的目录沙箱。不能在独立目录运行的产品，需要停止其他写入者并轮流开发。

在实际任务目录执行 guard，然后开发与交付：

~~~powershell
Set-Location -LiteralPath $task.path
python $parallelScript --repo . guard --task $task.id --token $task.token
~~~

完整交付命令见 [操作说明](.agents/skills/parallel-code-tasks/references/worktree.md)：

~~~text
guard → 开发与验证 → ready → 审查任务树
→ prepare → 验证与审查候选 → publish
→ 真正停止本任务全部写入者及服务 → release --workers-stopped
~~~

publish 只把成果回填原目录暂存区，不生成 commit。资源继续保留直到 release；暂停任务可 resume 接续原目录，重新检查占用，不自动拉取其他任务成果。

## 六项增强

| 能力 | 用法及效果 |
|---|---|
| 规则安装与接续 | install 保留已有规则；bundle 带入真实上下文；launch 绑定 CLI cwd |
| 故障诊断与恢复 | doctor 只读；recover 恢复创建、准备、回填和启动登记；进程退出释放内核锁 |
| 关键路径独占 | --exclusive 或配置 exclusive_paths，检查文件与目录重叠；普通文件可并行 |
| 项目环境 | 配置模板、port:auto、白名单忽略文件复制、环境注入及哈希校验 |
| 缩小共享锁 | 本任务快照与候选应用不持全局锁；最终回填串行并拒绝过期基线 |
| 可读状态与审计 | status --format table 显示下一步；events 记录状态和失败类型 |

配置示例与真实资源边界见 [configuration.md](.agents/skills/parallel-code-tasks/references/configuration.md)。例如将公共接口、迁移、package-lock.json 加入独占路径，端口、数据库/schema、Redis 前缀、Docker 项目名按任务分配；业务代码必须真实使用这些值。

## 常见情况

- 同时修改同一文件：各写各的目录，交付时三方合并，冲突保留在当前任务候选中。
- A 已完成，B 仍在开发：A 可以单独 publish；B 以后按最新成果准备候选。
- C 几天后加入：继承最新提交及协议已回填的未提交成果，无需整批完成。
- 原目录有未知修改：原样保留，新任务可从已知基线开发；prepare/publish 拒绝覆盖。
- 两个候选同时准备：可并行；最终回填一次一个，后到的过期候选重新 prepare 和审查。
- 进程崩溃：内核锁自动释放，锁文件保留。旧目录锁要 doctor 核实 owner，再 recover-lock，不按超时抢占。
- 任务创建或集成中断：登记与成果保留，recover 按真实内容恢复；额外编辑时拒绝强制修复。
- 工具关闭后继续：原任务 ID/token，在原目录 guard；暂停过的任务先 resume。

~~~powershell
python $parallelScript --repo $projectPath status --format table
python $parallelScript --repo $projectPath doctor
python $parallelScript --repo $projectPath events --limit 20
~~~

## 使用边界

这是参与者共同遵守的本地协议，不是安全认证或操作系统隔离。忽略规则的程序仍能访问其他目录；接续码只防止误接管。`--workers-stopped` 是执行者确认，不代表脚本已停止全部进程。

只协调同一 Git common directory；不同克隆或机器不会自动共享。资源登记不创建数据库、不持续绑定端口，也不控制未登记进程。未声明的外部连接和全局工具配置仍可能互相影响。

不支持 sparse checkout/submodule。Git 过滤器和 hooks 按真实仓库执行。合并无文本冲突也不能证明业务正确，必须验证实际接口、数据和行为。

运行登记、环境文件、快照引用及历史目录需按项目维护；脚本不会自动清理。V1 有活动批次时拒绝混用；既有 V2 任务保留路径、凭据和基线，读取时补增强字段。

## GitHub 参考

2026-10-02 调研下列项目的 README/Skill，未安装或运行第三方工具；本实现采用标准库和 Git，复用其公开思路，没有引入产品调度依赖。

| 项目 | 参考点 |
|---|---|
| [obra/superpowers](https://github.com/obra/superpowers) | worktree 隔离、任务边界和审查 |
| [SpillwaveSolutions/parallel-worktrees](https://github.com/SpillwaveSolutions/parallel-worktrees) | 独立工作目录、会话状态和资源提醒 |
| [nekocode/agent-worktree](https://github.com/nekocode/agent-worktree) | worktree 生命周期和多工具接入 |
| [TheAhmadOsman/parallel-agent-worktree-skill](https://github.com/TheAhmadOsman/parallel-agent-worktree-skill) | AI CLI 的工作目录隔离 |

## 验证记录

2026-10-03 使用独立临时 Git 仓库、真实 Git 操作和自制 CLI 辅助进程验证；未向发布仓库新增测试文件，未启动真实 AI 产品。

已完成 98 项场景检查：独立任务、真实 cwd、凭据与范围、同文件三方合并、冲突解决及独立交付、未提交成果继承、过期候选拒绝、真实索引保护、独占认领、资源释放/重占、安装幂等及用户文件保护、忽略文件白名单、CLI 环境注入与退出码、只读诊断、内核锁崩溃释放、旧锁指纹、五进程资源竞争、缩小全局锁、创建/准备/回填中断恢复、启动记录核对、V2 接续兼容、中文空格、二进制、删除及显式暂存忽略文件。

Python 语法、Skill 格式、UTF-8/LF 和本地文档链接检查通过；fixture-demo 原有 6 个 unittest 通过，Git 状态和 HEAD 保持。Windows 上公共 Skill 校验器需 `python -X utf8` 运行，避免系统默认 GBK 解码 UTF-8 中文。

这里的故障是受控异常和辅助进程退出，验证脚本恢复路径；没有宣称真实产品发生故障或已完成产品目录沙箱、业务集成及外部数据库隔离验收。Skill 格式与静态检查不能替代行为验证。

原有本地 fixture-demo 独立仓库保留，不作为子模块或运行登记上传。
