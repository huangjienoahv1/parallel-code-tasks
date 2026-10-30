# 小白使用手册：多工具并行开发、出错恢复与 worktree 清理

这份手册适合第一次接触 Git worktree，想让 Codex、ZCode、OpenCode、Claude Code 等工具开发同一个项目的人。同一工具开多个开发会话，也按同样流程操作。

本文对应本仓库当前脚本：协议增强版本 3，登记文件结构版本 2。命令以 Windows 的 PowerShell 7 为例。**先在可丢弃的演练项目操作一遍，再用到正式项目。** 示例路径、文件名和需求都需要替换成自己的实际值。

## 阅读路线

| 你现在想做什么 | 看哪一节 |
|---|---|
| 完全没接触过 Git | 第 1～3 节 |
| 第一次安装并开始任务 | 第 4～6 节 |
| 把做好的功能放回原项目 | 第 7 节 |
| 关闭工具后继续、暂时不做了 | 第 8 节 |
| 出错了，不知道该怎么办 | 第 9～10 节 |
| 任务结束，想回收磁盘空间 | 第 11 节 |
| 记不住命令 | 第 12～14 节 |

## 1. 这个工具解决什么问题

例如，你有一个网站：工具 A 修登录，工具 B 做报表。如果两个工具直接修改 `C:/code/my-app`，一个工具可能覆盖另一个工具刚写的文件，构建输出、端口和数据库也可能互相干扰。

本工具为每个任务分配一个独立工作目录。A 在自己的目录修登录，B 在另一个目录做报表。完成后先检查，再把成果逐个放回原项目。

```text
C:/code/my-app/                              原项目：接收成果
C:/code/my-app.parallel/
  login-fix-1-随机编号/
    work/                                   A 的开发目录
    publish-随机编号/                        A 的集成候选目录
    environment.json                        A 的任务环境
  report-1-随机编号/
    work/                                   B 的开发目录
    publish-随机编号/                        B 的集成候选目录
```

目录由脚本返回，**不要照着示例自己猜路径**。以后再次准备交付，可能产生新的候选目录，旧候选保留。

你需要遵守三件事：每个任务一个实际写入者；开发工具真正打开返回的目录；原项目不同时接受其他工具直接编辑业务代码。

只在终端执行 `cd` 或 `Set-Location`，不会自动改变编辑器和 AI 编辑工具的工作区。产品无法打开独立目录时，先停止其他写入者，轮流开发。

## 2. 必须知道的概念

| 概念 | 通俗解释 | 在这里的用途 |
|---|---|---|
| Git 仓库 | 管理代码版本的项目 | 必须是已经有提交的本地仓库 |
| 原项目、source | 你平时打开的那份项目目录 | 接收各任务回填成果 |
| worktree | 同一个仓库的另一个工作目录 | 各自编辑文件，不互相覆盖 |
| 分支、branch | 一条独立的版本发展路线 | 每个任务和候选使用各自分支 |
| HEAD | 当前目录所在的提交 | 与当前尚未提交文件内容不同 |
| commit | 把暂存内容记录成正式版本 | 需要你按项目流程另行执行 |
| 暂存区、index | 准备纳入下一次提交的内容 | publish 把成果放到原项目的这里 |
| 工作区 | 磁盘上正在编辑的文件 | 可以与暂存区内容不同 |
| tree、SHA | 某一份文件内容快照及其编号 | 用于确认审查的是哪一份具体成果 |
| 基线、baseline | 任务开始时的代码起点 | 对比任务实际新增了什么 |
| scope | 本任务允许修改的文件或目录 | 防止顺带修改其他模块 |
| exclusive | 本任务独占的关键路径 | 公共接口、迁移、依赖锁文件等可独占 |
| resource | 声明要使用的端口、数据库等 | 协调同仓库参与者的资源占用 |
| task ID | 任务的唯一名字 | 新任务使用新名字，接续沿用原名字 |
| agent | 工具标签 | 如 codex、zcode，仅用于识别 |
| token | 本机任务接续码 | 防止误接管任务，不是安全认证 |
| candidate | 临时集成后的候选目录 | 在这里解决冲突、验证最终结果 |
| 锁、lock | 避免脚本同时修改共享登记的机制 | 最终回填串行进行 |
| common directory | 所有关联 worktree 共用的 Git 存储 | 保存共享对象、引用和任务登记 |

`worktree` 与复制项目、重新 `git clone` 不一样：它共享同一个仓库的历史和登记，同时拥有独立源码、HEAD 和索引。另一个克隆，即使 GitHub 地址一样，也不会自动共享本协议登记。[Git worktree 官方说明](https://git-scm.com/docs/git-worktree)

`tree` 只描述文件内容，不代表构建通过、业务正确或正式提交。你会遇到三个不同的编号：`baseline_tree` 是起点；`ready.tree` 是任务成果；`candidate.tree` 是和最新共享成果集成后的结果。**prepare 用任务成果编号，publish 用候选编号。**

### 2.1 四个“完成”不能混淆

| 操作 | 实际发生什么 | 还需要做什么 |
|---|---|---|
| ready | 冻结任务文件快照 | 审查任务成果 |
| publish | 回填到原项目并暂存 | 按项目流程 commit，需要时 push、部署 |
| release | 释放本任务资源和独占登记 | 保留目录，按需归档清理 |
| worktree remove | 删除指定关联工作目录及 Git 注册 | 分支和本协议历史登记仍可能保留 |

本工具不会自动提交、推送到 GitHub、部署，也不会替你关闭服务或删除目录。

### 2.2 worktree 没有隔离全部环境

代码目录分开后，两个程序仍可能连接同一个数据库，使用同一个 Redis 前缀或端口。你仍需要为任务准备不同端口、数据库/schema、缓存前缀、Docker 项目名和可写目录。

`port:auto` 只登记并探测当前可用端口，不会一直占住端口。数据库登记也不会自动创建数据库。环境值必须被项目真实读取。不要共享可写的 `node_modules`、虚拟环境或构建输出。

## 3. 操作前准备

需要 Git、Python 3.9 或以上，以及 Windows 的 PowerShell 7。打开 PowerShell 7，程序名应为 `pwsh.exe`，不要使用 Windows PowerShell 5.1。

```powershell
$PSVersionTable.PSVersion.Major
git --version
python --version
```

第一行应至少为 `7`。命令不存在时，先安装或配置对应程序，再重新打开终端。不要通过改脚本来绕过缺少 Git/Python 的问题。

目标项目还必须满足：有至少一次 Git 提交、处于普通分支、没有未完成的 merge/rebase、不是 sparse checkout，也不含 submodule。如果项目尚未纳入 Git，让 AI 先单独检查并初始化；不要直接把包含密码、数据库和运行文件的整个目录加入 Git。

首次开始前查看：

```powershell
$projectPath = 'C:/code/my-app'
git -C $projectPath status --short
git -C $projectPath branch --show-current
git -C $projectPath log -1 --oneline
git -C $projectPath worktree list
```

`status --short` 没有输出通常表示 Git 关注的文件没有改动。它不证明忽略文件不存在。已有修改要先搞清楚是谁写的、是否需要保留；本协议不会替你清空它们。

## 4. 首次安装：只做一次

### 4.1 区分“工具仓库”和“业务项目”

工具仓库存放协议和脚本；业务项目是你要开发的 Java、Vue 或其他项目。通常安装目标是业务项目，而不是再次安装到工具仓库自身。

如果还没有工具仓库，可以克隆：

```powershell
git clone https://github.com/huangjienoahv1/parallel-code-tasks.git C:/tools/parallel-code-tasks
if ($LASTEXITCODE -ne 0) { throw '克隆失败，停止操作' }
```

如果你正在使用本仓库，不必再次克隆，可直接设置：

```powershell
$parallelScript = 'C:/code/mynew/处理单项目且并行任务/.agents/skills/parallel-code-tasks/scripts/parallel_tasks.py'
$projectPath = 'C:/code/my-app' # 替换成真实业务项目
```

所有参与者使用同一份、同一版本的脚本。不要让 A 使用旧版、B 使用新版。

### 4.2 安装到业务项目

```powershell
python $parallelScript --repo $projectPath install
if ($LASTEXITCODE -ne 0) { throw '安装失败，检查错误及实际文件，停止操作' }
git -C $projectPath status --short
git -C $projectPath diff -- AGENTS.md
```

安装会创建或补充：

| 文件 | 用途 |
|---|---|
| `.agents/skills/parallel-code-tasks/` | 完整协议、操作文档和脚本，包含本手册 |
| `AGENTS.md` | 在已有规则末尾追加协作入口，保留原内容 |
| `.parallel-code-tasks.json` | 默认项目配置，已有配置不覆盖 |

默认配置不自动为业务建立数据库或配置启动环境。需要 Claude 等产品的实际 Markdown 规则入口时，可明确追加：

```powershell
python $parallelScript --repo $projectPath install --rules-file AGENTS.md --rules-file CLAUDE.md
if ($LASTEXITCODE -ne 0) { throw '安装失败，停止操作' }
```

这不保证产品已读取规则。没有自动规则入口的产品，把第 5 节提示词粘贴到会话。

### 4.3 审查安装内容并纳入基线

**先检查已有修改，再按项目流程提交安装文件，之后启动并行任务。** 未提交的安装文件可能不在新 worktree 中，而且会成为阻止回填的未知修改。

下面只暂存安装路径；AGENTS.md 中已有用户修改也会被一同暂存，执行前必须审查。这是由你主动执行的 Git 提交，不是 install 自动提交：

```powershell
git -C $projectPath add -- AGENTS.md .parallel-code-tasks.json .agents/skills/parallel-code-tasks
if ($LASTEXITCODE -ne 0) { throw '暂存失败' }
git -C $projectPath diff --cached --stat
git -C $projectPath diff --cached
# 确认整个暂存区只含准备提交的内容后，再执行：
git -C $projectPath commit -m 'chore(collaboration): 安装多工具并行开发协议'
if ($LASTEXITCODE -ne 0) { throw '提交失败，停止操作' }
```

如果还安装了 CLAUDE.md，审查后也明确暂存该文件。暂存区有其他未完成内容时，不要直接 commit；先让原写入者处理。无需为了开始本地任务先推送到 GitHub。

## 5. 最适合小白的使用方式：让 AI 执行流程

你负责提供需求和验收、打开正确目录、决定提交和清理；AI 负责登记、开发、验证与审查回填。第一次可先只开一个任务，熟悉后再开第二个。

### 5.1 创建一个任务

把下面内容发给准备开发的工具，替换路径和需求：

```text
请阅读 C:/code/my-app/.agents/skills/parallel-code-tasks/SKILL.md，
按通用协作协议执行，先查看项目规则、Git 状态和共享任务状态。
原项目：C:/code/my-app。
工具标签：codex；新任务 ID：login-fix-1。
需求：修复登录过期时的提示，保持接口不变。
验收：正常登录不受影响；会话过期能给出正确提示。
允许修改：src/login。
资源：如果需要启动服务，登记独立端口；不要连接其他任务的测试数据库。
先 enter；返回真实任务目录后，让我在那里打开开发会话。
所有编辑和命令必须在该任务 worktree；guard 通过后开发。
你不是唯一写入者，不撤销其他工具修改。
开发后执行必要验证、ready、审查、prepare、候选验证和审查、publish。
真正停止本任务所有写入者和服务后 release。
不要自动提交、推送、部署或删除目录；汇报实际完成和验证结果。
```

如果 AI 当前会话无法切换根目录，它应先登记并交接；你在返回的 `task.path` 中打开一个新会话，再粘贴 bundle。不要在原目录继续开发。

### 5.2 同时做第二个任务

在另一个会话提出独立任务，例如 `report-1`、标签 `zcode`、范围 `src/report`。同一产品也可以再开一个会话，用新 ID。

任务 ID 和标签必须以小写字母开头，只含小写字母、数字、连字符，最长 40 字符，如 `login-fix-1`。不要使用中文、空格、下划线，也不要把同一 ID 给两个写入会话。

公共接口和数据库迁移等紧密相关任务，适合先后执行。B 依赖 A 的结果时，等 A **publish 成功**后再登记 B。不要把“AI 说已经写好了”当作成果已回填。

### 5.3 日常查看进度

```powershell
python $parallelScript --repo $projectPath status --format table
```

它显示任务状态、目录、资源和建议下一步。一个任务完成就能交付，不需要等所有任务完成。

## 6. 手动操作：登记、打开、开发

如果希望自己执行命令，以下步骤按顺序做。一个终端建议只管理一个任务，避免变量串到其他任务。新开终端时重新设置脚本与项目路径；变量不会自动保存。

### 6.1 定义一个检查错误的辅助函数

```powershell
function Invoke-ParallelJson {
    param([string[]]$CommandArguments, [string]$RepositoryPath = $projectPath)
    $resultLines = & python $parallelScript --repo $RepositoryPath @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw '协议命令失败，请先查看错误和 status，不执行后续步骤' }
    return (($resultLines -join "`n") | ConvertFrom-Json)
}
```

这个函数只适用于默认 JSON 输出，不用于 `--format table`、`bundle --format text` 或输出混合了 CLI 文本的 `launch`。下文使用它是为了防止上一条失败后仍继续操作。

### 6.2 登记任务

```powershell
$entryResult = Invoke-ParallelJson -CommandArguments @(
    'enter', '--agent', 'codex', '--task', 'login-fix-1',
    '--goal', '修复登录过期提示，保持接口不变',
    '--scope', 'src/login', '--resource', 'port:auto'
)
$taskInfo = $entryResult.task
$taskInfo | Select-Object id, path, branch, baseline_head, baseline_tree
```

`$taskInfo.token` 保存接续码，避免贴到公开讨论、截图或另一个开发会话。范围可以多次使用 `--scope`，必须写具体相对路径，不支持通配符。`--scope '.'` 表示全仓库，只有任务确实负责全部内容时才使用。

公共接口可以增加 `--exclusive src/contracts`，且该路径必须与 scope 相交。资源/独占冲突时先核查占用者，不抢占仍在工作的任务。

### 6.3 给正确目录带入上下文

```powershell
python $parallelScript --repo $projectPath bundle --task $taskInfo.id --token $taskInfo.token --format text
if ($LASTEXITCODE -ne 0) { throw '生成接续包失败' }
```

在产品中打开 `$taskInfo.path`，粘贴输出的完整提示词。bundle 包含目录、基线、目标、范围、脚本和资源，可能含任务接续码，仅用于本任务。

在真实任务终端检查：

```powershell
Set-Location -LiteralPath $taskInfo.path
Get-Location
git rev-parse --show-toplevel
$guardResult = Invoke-ParallelJson -RepositoryPath $taskInfo.path -CommandArguments @('guard', '--task', $taskInfo.id, '--token', $taskInfo.token)
```

两个路径应指向任务 worktree。guard 还要求 `--repo` 指向这个任务目录，所以这里显式传入 `-RepositoryPath $taskInfo.path`；其他共享命令通常可以使用默认原项目路径。guard 不通过就先修正工作目录，不绕过检查。

### 6.4 开发和启动

AI 按项目原有要求修改和验证。worktree 不自动安装依赖；按项目 README 在各自目录安装、构建。数据库和账号按项目流程准备。

CLI 可使用包装启动，替换成真实程序及它实际支持的参数：

```powershell
$toolExecutable = 'C:/tools/your-agent/agent.exe'
python $parallelScript --repo $projectPath launch --task $taskInfo.id --token $taskInfo.token -- $toolExecutable
if ($LASTEXITCODE -ne 0) { throw 'CLI 启动或运行失败，查看实际输出及 doctor' }
```

launch 设置 cwd、注入任务环境并记录退出码，但不自动发送需求、不自动停止子服务，也不验证整个产品的目录沙箱。图形工具的环境加载按其真实启动方式配置。详细模板见 [configuration.md](configuration.md)。

## 7. 完成交付：每个任务自己走完

```text
开发与验证 → ready → 审查任务差异 → prepare
→ 在候选目录验证与审查 → publish → 停止进程 → release
→ 原项目按流程提交 → 按需归档和清理
```

不能把整段交付命令不看结果就一口气执行。每次冻结、集成后，都要检查这次具体成果。

### 7.1 冻结和审查任务成果

在开发目录完成必要验证并停止继续修改后：

```powershell
$readyResult = Invoke-ParallelJson -CommandArguments @('ready', '--task', $taskInfo.id, '--token', $taskInfo.token)
$taskTree = $readyResult.tree
git -C $taskInfo.path diff $taskInfo.baseline_tree $taskTree --
if ($LASTEXITCODE -ne 0) { throw '读取任务差异失败' }
```

ready 会收集非忽略新文件、修改和删除，以及明确暂存的文件；未跟踪的忽略文件不交付。它不会自动提交，也不修改真实暂存区。开始时继承的已回填成果不是本任务新增内容。

审查应核对：是否符合需求、是否只改 scope、是否有密码或运行数据、必要验证是否真的通过。让 AI 说明实际验证证据，不能只复制 SHA 当作审查。

### 7.2 准备集成候选

审查通过后：

```powershell
$prepareResult = Invoke-ParallelJson -CommandArguments @(
    'prepare', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--reviewed-tree', $taskTree
)
$candidateInfo = $prepareResult.candidate
$candidateInfo | Select-Object id, path, branch, tree, source_tree
git -C $candidateInfo.path diff $candidateInfo.source_tree $candidateInfo.tree --
if ($LASTEXITCODE -ne 0) { throw '读取候选差异失败' }
```

如果 prepare 报冲突，函数会停止，**不会获得可直接回填的候选**。使用第 9.3 节找回现场。prepare 失败可能已创建候选；不要把“命令失败”理解为没有任何文件产生。

### 7.3 在候选目录验证，之后回填

候选包含最新共享成果与本任务结果。项目构建、已有后端验证等应在候选目录执行；依赖和真实环境需要按项目准备。遵守项目约定，不新增前端测试环境，不把未进行的人工验收说成通过。

```powershell
Set-Location -LiteralPath $candidateInfo.path
# 在这里执行项目实际需要的构建/验证，命令以项目 README 为准。
```

验证和审查通过、候选未被修改，再回填：

```powershell
$publishResult = Invoke-ParallelJson -CommandArguments @(
    'publish', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--reviewed-tree', $candidateInfo.tree
)
git -C $projectPath status --short
git -C $projectPath diff --cached --stat
```

如果候选被额外修改，旧 tree 不能继续使用。发现任务本身需要调整，走 reopen、重新 ready/prepare；实际冲突按第 9.3 节处理，不直接把候选文件复制到原项目。

### 7.4 停止写入者，释放资源

关闭本任务开发会话的写入、CLI、应用服务、文件监听器和相关容器。核对任务登记的进程与服务，随后：

```powershell
$releaseResult = Invoke-ParallelJson -CommandArguments @(
    'release', '--task', $taskInfo.id, '--token', $taskInfo.token, '--workers-stopped'
)
```

`--workers-stopped` 是你作出的确认，脚本不会替你终止进程。成功回填后 release 得到 `released`；未回填就 release 会得到 `parked`，含义是暂停。资源释放不等于端口上的程序已经停止。

### 7.5 在原项目提交

可以接收多个任务后统一提交，也可以逐个提交。提交原目录前，先暂停针对原目录的协议回填，避免提交过程与 publish 并发；各任务仍可在自己的目录开发。

```powershell
Set-Location -LiteralPath $projectPath
git status --short
git diff --cached --stat
git diff --cached
# 审查整个暂存区、确认这是本次准备提交的完整成果后：
git commit -m 'fix(login): 修复会话过期提示'
if ($LASTEXITCODE -ne 0) { throw '提交失败，保留现场检查' }
```

提交信息按实际内容填写。Git commit 提交整个暂存区；如果里面还有其他任务成果，不要把它们误记成只有登录修复。不要为了“整理干净”对原项目执行 reset、clean 或 stash。

push 和部署按项目流程另行授权、执行和验证。当前流程并没有发布网站或服务。

## 8. 关闭工具、暂停和再次接续

### 8.1 只是关闭工具，没有 release

文件和任务登记仍保留，资源通常仍处于登记占用状态。确认旧会话及服务的实际情况，用同一 ID/token、同一 worktree 继续；先 status。working 状态先 guard，再开发；ready/conflict/prepared 等状态按对应交付步骤继续，不能直接 guard 后修改；published 状态应停止进程再 release。若 launch 登记异常，先按第 9.7 节核查。

### 8.2 暂时不做，释放资源但保留工作

实际停止本任务所有写入者和服务后执行 release。未回填任务进入 `parked`，目录和候选保留，不要删除。

恢复时先确认没有旧写入者：

```powershell
$resumeResult = Invoke-ParallelJson -CommandArguments @(
    'resume', '--task', $taskInfo.id, '--token', $taskInfo.token, '--workers-stopped'
)
Set-Location -LiteralPath $taskInfo.path
if ($resumeResult.task.status -eq 'working') {
    $guardResult = Invoke-ParallelJson -RepositoryPath $taskInfo.path -CommandArguments @('guard', '--task', $taskInfo.id, '--token', $taskInfo.token)
}
```

resume 重新检查资源和独占路径，可能因为别人已占用而失败。它保留原任务基线，不会自动拉取别人刚完成的成果。原来暂停在 ready/prepared 等状态，恢复后仍应按那个状态的流程继续。

### 8.3 ready 后又想改代码

停止旧写入者和候选修改，执行：

```powershell
$reopenResult = Invoke-ParallelJson -CommandArguments @(
    'reopen', '--task', $taskInfo.id, '--token', $taskInfo.token, '--workers-stopped'
)
Set-Location -LiteralPath $taskInfo.path
```

再 guard、开发、验证、ready、审查、prepare。旧候选失效，旧 tree 不再作为新成果使用。已经 published/released 的任务有新需求，创建新 ID。

### 8.4 重开终端，变量和 token 丢了

重新设置 `$parallelScript`、`$projectPath` 和第 6.1 节函数。确认是自己负责的任务，并且没有第二个会话同时接管，再只读获取本机登记：

```powershell
$statusInfo = Invoke-ParallelJson -CommandArguments @('status')
$savedState = Get-Content -LiteralPath $statusInfo.state_file -Raw -Encoding utf8 | ConvertFrom-Json
$taskId = 'login-fix-1'
$taskProperty = $savedState.tasks.PSObject.Properties[$taskId]
if ($null -eq $taskProperty) { throw '没有这个任务，核对项目路径和 ID' }
$taskInfo = $taskProperty.Value
$taskTree = $taskInfo.tree # 已 ready 的任务可从登记找回冻结树；working 时可能为空
```

这会把原 token 放回变量，不需要打印整个登记文件。不要手动改 JSON、重新 enter 同一 ID 或向公共平台上传登记。status 默认隐藏 token。

## 9. 出错恢复：先诊断，再按状态处理

### 9.1 所有错误先做这几件事

停止在发生错误的任务/候选中继续编辑，保留错误全文和当前目录。执行只读检查：

```powershell
python $parallelScript --repo $projectPath status --format table
python $parallelScript --repo $projectPath doctor
python $parallelScript --repo $projectPath events --limit 20
git -C $projectPath status --short
git -C $projectPath worktree list
```

doctor 不会修复、删除或抢锁。原目录显示 dirty 可能是正常回填的暂存成果，不一定出错。先查看任务状态、候选和 pending，不要立即清理目录，也不要对所有错误都执行 recover。

| 现象或状态 | 正确入口 |
|---|---|
| guard 指出目录错误 | 切到实际 task.path，并确认编辑器也打开它 |
| 任务 ID 已存在 | 原任务接续，新需求使用新 ID |
| 原目录未知修改 | 找原写入者处理，保留成果，之后重新 prepare |
| conflict | 在当前候选解决、暂存、inspect、resolve |
| prepared 但过期 | 重新 prepare 同一个任务 tree，审查新候选 |
| creating | 停止写入后 recover |
| preparing | 核实候选后 recover，可能转 conflict 或重建候选 |
| publishing / pending | 暂停原目录写入和回填，核查后 recover |
| launch starting/running/interrupted | 核查真实进程，停止后按 launch.id 恢复 |
| 锁等待超时 | doctor 核查持锁进程，等待正常操作结束 |
| 环境文件缺失或变化 | 保留现场，找回原始文件，不改登记哈希绕过 |

### 9.2 原目录有未知修改

此时新任务可能可以从已知基线隔离开发，但 prepare/publish 会拒绝回填。先查看原项目的 `git diff`、`git diff --cached` 和未跟踪文件，确认归属。

由原写入者审查、验证并按项目流程保存或提交；已知回填成果若仅暂存状态被改变，核实内容后可重新暂存完整已知成果。不要把不认识的内容一股脑 add/commit，也不要删除它们。处理完成后重新 prepare、审查候选和 publish。

### 9.3 文本冲突：只在当前候选解决

两个任务可能都改了同一个文件。A 先回填后，B 的 prepare 可能产生 `conflict`，现场留在 B 的候选目录。

```powershell
$statusInfo = Invoke-ParallelJson -CommandArguments @('status')
$currentTask = $statusInfo.tasks | Where-Object { $_.id -eq $taskInfo.id }
$candidateInfo = $currentTask.candidates | Where-Object { $_.id -eq $currentTask.candidate }
if ($null -eq $candidateInfo) { throw '没有当前候选，先核查 doctor' }
Set-Location -LiteralPath $candidateInfo.path
git status --short
```

文件里可能出现 `<<<<<<<`、`=======`、`>>>>>>>`。让 AI 根据业务意图合并双方修改、去掉标记并解释处理结果，不要全部选择“我的”或“他们的”。删除/改名/二进制冲突未必有这些标记，应结合 Git 状态检查。

只暂存实际解决的文件，示例路径替换为真实冲突文件：

```powershell
git add -- src/login/session.js
if ($LASTEXITCODE -ne 0) { throw '暂存冲突解决文件失败' }
git diff --name-only --diff-filter=U
git diff --cached
```

第一条 diff 不应再输出未解决文件。执行候选必要验证并审查后：

```powershell
$inspection = Invoke-ParallelJson -CommandArguments @('inspect', '--task', $taskInfo.id, '--token', $taskInfo.token)
if ($inspection.unresolved) { throw '仍有未解决冲突' }
git -C $candidateInfo.path diff $candidateInfo.source_tree $inspection.actual_tree --
if ($LASTEXITCODE -ne 0) { throw '读取解决后差异失败' }
# 确认上面的实际树已验证、审查，再登记解决结果：
$resolvedResult = Invoke-ParallelJson -CommandArguments @(
    'resolve', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--reviewed-tree', $inspection.actual_tree
)
$candidateInfo = $resolvedResult.candidate
$publishResult = Invoke-ParallelJson -CommandArguments @(
    'publish', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--reviewed-tree', $candidateInfo.tree
)
```

如果 publish 提示过期，按下一节重建。候选中不要 commit；候选流程要求 HEAD 保持原集成基线。

### 9.4 候选过期

这意味着准备候选后，共享成果又发生变化。不是文件丢失，也不能强制回填。

保持任务开发树不变，用 ready 时的 `$taskTree` 重新 prepare，得到新的 `$candidateInfo`。重做候选验证和审查，之后 publish。旧候选保留用于核查；之前在旧候选解决过冲突，可能需要在新候选重新按语义解决。

### 9.5 创建或准备途中断开

对于 `creating`，确认该任务写入者停止后：

```powershell
$recoveryResult = Invoke-ParallelJson -CommandArguments @(
    'recover', '--task', $taskInfo.id, '--token', $taskInfo.token, '--workers-stopped'
)
```

仅在脚本能证明内容符合基线时恢复。已有未知文件、HEAD 或索引变化时会拒绝，保留现场，不重新 enter 同一 ID。

对于 `preparing`，停止任务及候选写入，使用已审查的任务 tree：

```powershell
$recoveryResult = Invoke-ParallelJson -CommandArguments @(
    'recover', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--workers-stopped', '--reviewed-tree', $taskTree
)
```

存在真实未解决冲突时转为 conflict；其他情况可能创建新候选。查看返回结果和 status，重新验证、审查，不把 recover 成功等同于 publish 成功。

### 9.6 回填途中断开

状态 `publishing` 或存在本任务 pending 时，先暂停其他回填和原目录编辑/提交，核查 Git 状态和 doctor，再执行：

```powershell
$recoveryResult = Invoke-ParallelJson -CommandArguments @('recover', '--task', $taskInfo.id, '--token', $taskInfo.token)
```

脚本根据原目录真实内容判断：完整应用后补全 published 登记；尚未应用则回到 prepared；额外变化则拒绝并保留现场。回到 prepared 后重新审查，再 publish。不要手动再应用一遍补丁。

### 9.7 CLI 崩溃或启动登记异常

doctor 会给出 launch 记录、ID、PID 和进程状态。先确认进程是谁，关闭对应 CLI 及其子服务。不要按进程名批量结束所有 Node、Java 或 AI 工具。

确认全部停止后，把 doctor 返回的实际 launch.id 填入：

```powershell
$launchId = '替换为 doctor 返回的 launch.id'
$recoveryResult = Invoke-ParallelJson -CommandArguments @(
    'recover', '--task', $taskInfo.id, '--token', $taskInfo.token,
    '--workers-stopped', '--expected-launch', $launchId
)
```

这只结束异常启动登记，不停止进程、不释放资源，也不会删除成果。任务状态不符合该恢复条件或进程仍存在时会拒绝。

### 9.8 锁超时与旧锁

当前版本使用操作系统文件锁。持锁进程退出后锁会释放，但 `mutex.lock` 和任务 `.lock` 文件会保留。**存在锁文件不等于仍被锁住，不要删除锁文件。**

先 doctor 核查；正常脚本仍运行时等它结束。确认只是短暂竞争后，可有限等待再执行，例如：

```powershell
python $parallelScript --repo $projectPath --lock-wait 15 status --format table
```

`--lock-wait` 放在子命令之前，范围 0～30 秒；不会按超时抢占，不应无限重试。

只有 doctor 明确报告旧版目录锁时，停止所有旧版协议脚本，核实 owner 是本机已退出进程，再使用其实际 fingerprint：

```powershell
$ownerFingerprint = '替换为 doctor 的 legacy_lock.fingerprint'
python $parallelScript --repo $projectPath recover-lock --workers-stopped --expected-owner $ownerFingerprint
if ($LASTEXITCODE -ne 0) { throw '旧锁恢复被拒绝，保留现场' }
```

这是旧目录锁专用操作，不用于当前内核锁。其他主机、活进程、未知内容或指纹变化会被拒绝。

## 10. 其他常见问题

| 问题 | 怎么处理 |
|---|---|
| ready 提示没有可交付改动 | 查看是否真的改了代码，是否全是忽略文件，是否在错误目录编辑 |
| 修改超出 scope | 核对实际 diff，保留其他人的工作；确实有新范围时先重新安排任务，不能改登记绕过 |
| 资源已占用 | 查 status，等待占用任务正常 release，或新任务使用独立资源 |
| release/resume 被拒绝 | 核对状态；过渡状态先恢复，parked 不重复 release，released 不 resume |
| 环境哈希不一致 | 找回与登记匹配的原文件，无法恢复时让 AI 核查保留现场；不要篡改哈希 |
| 启动报端口占用 | 查真正占用者，不杀无关程序；核查任务环境，必要时保留旧任务并新建独立任务 |
| 功能缺少别人的最新成果 | 进行中任务不会自动同步；交付时 prepare 集成；依赖任务等前置 publish 后再 enter |
| 没有文本冲突却功能不对 | 检查公共接口、配置、数据和执行顺序，回到任务目录修复并重新交付 |
| 安装升级被拒绝 | 停止旧协议命令，用来源新版 install --update；用户改过的文件要人工审查，不强行覆盖 |
| worktree 被手工移动 | 原路径被登记固定；Git repair 只修 Git 联系，不会改协议路径，保留现场让 AI 专门核查 |
| V1 活动批次阻止运行 | 用保留的 V1 协议完成或人工归档；不能删除旧登记强行混用 |

需要 AI 帮忙排查时，可以发：

```text
这个并行任务出错了，请先只读诊断，不重置、删除或强制回填。
原项目：<路径>；任务 ID：<ID>；任务目录：<path>。
最后执行的命令：<完整命令，隐藏 token 和敏感值>。
错误输出：<实际错误>。
请检查 status、doctor、events 和实际 Git 状态，说明当前状态和最小恢复方案。
能按协议安全恢复就执行；恢复后报告成果是否回填、资源是否仍占用。
```

## 11. worktree 什么时候清理、怎么清理

### 11.1 先区分五种东西

| 对象 | release 会删除吗 | 建议 |
|---|---|---|
| 资源、独占登记 | 释放占用 | 真正停止进程后 release |
| work 开发目录 | 不会 | 已交付并保存成果后归档，再删除 |
| publish 候选目录 | 不会 | 当前和旧候选分别核查归档 |
| 任务与候选分支 | 不会 | 可保留，磁盘占用通常远小于依赖目录 |
| 协议历史、日志、快照引用 | 不会 | 当前无 prune/forget/cleanup 命令，保留 |

**默认建议：先完成回填与原项目提交，再 release 和备份，最后只清理明确结束的任务目录。** 暂停的 parked 任务要继续做，就保留目录。对于放弃的未交付任务，先 parked 和归档保留；当前没有“取消并删除登记”的完整生命周期命令，小白不要直接抹掉它。

### 11.2 清理前检查单

必须逐项确认：

1. 任务已 publish，原项目已审查并正式提交了预期成果。
2. 本任务已 released，资源登记不再占用。
3. 本任务所有 AI 写入者、CLI、服务、监听器和相关容器已停止。
4. 已查清 task.path、workspace、当前与历史 candidate 路径和分支。
5. 没有其他任务借用这些目录、没有外部文件或必要本地数据未备份。
6. 备份已打开核对，未跟踪和忽略文件也已检查。

候选目录可能留有冲突、调试修改；任务目录可能留有未提交代码。即使 published/released，也不能跳过这些检查。

### 11.3 从登记取出清理对象

使用第 6.1 节函数，获取指定任务：

```powershell
$statusInfo = Invoke-ParallelJson -CommandArguments @('status')
$cleanupTaskId = 'login-fix-1'
$cleanupTask = $statusInfo.tasks | Where-Object { $_.id -eq $cleanupTaskId }
if ($null -eq $cleanupTask) { throw '任务不存在' }
if ($cleanupTask.status -ne 'released' -or $cleanupTask.resources_reserved) {
    throw '任务未完成交付和释放，本节不能清理'
}
$cleanupTask | Select-Object id, status, path, branch, workspace
$cleanupTask.candidates | Select-Object id, path, branch, tree
git -C $projectPath worktree list
```

一个任务每次 prepare 都可能增加候选，所以不要只处理最近一个候选，也不要删除其他任务的兄弟目录。

### 11.4 完整备份任务目录

选择项目目录和 `.parallel` 之外的备份位置。先停止本任务文件写入；记录操作时不要把 token 或环境秘密贴到公共平台。

```powershell
$backupRoot = 'C:/backups/parallel-code-tasks'
$backupPath = Join-Path $backupRoot ($cleanupTaskId + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $backupPath) { throw '备份路径已存在，换一个新路径' }
New-Item -ItemType Directory -Path $backupPath -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $cleanupTask.workspace -Destination (Join-Path $backupPath 'workspace') -Recurse -Force -ErrorAction Stop
```

脚本自身创建的任务，workspace 包含开发目录、候选和环境文件。用 `enter --worktree` 接入的外部开发目录可能在 workspace 之外，必须另行备份该目录，原工具管理的 worktree 应优先用原工具归档。

```powershell
if ($cleanupTask.managed_by -eq 'external') {
    Copy-Item -LiteralPath $cleanupTask.path -Destination (Join-Path $backupPath 'external-work') -Recurse -Force -ErrorAction Stop
}
```

复制成功不等于备份验证成功：打开备份中的关键源码、配置、未跟踪文件和本地数据，核对文件数、大小，重要数据可比较哈希。确认隐藏文件也存在。备份可能含密码，应保存在受保护的本机位置。

**复制的 worktree 里的 `.git` 通常只是指向原仓库的文件；这份备份可用于取回普通文件，不是独立完整 Git 仓库。** 分支历史另按下一节使用 Git bundle 保存。协议 tree 快照不能代替完整文件备份，忽略文件和数据库内容不在代码快照中。

### 11.5 检查每个 worktree；脏目录先归档

先检查开发目录，再对每个候选目录分别执行相同检查：

```powershell
$cleanupPath = $cleanupTask.path # 下次换成某个 candidate.path
git -C $cleanupPath status --short --untracked-files=all
git -C $cleanupPath ls-files --others --ignored --exclude-standard
git -C $cleanupPath diff --stat
git -C $cleanupPath diff --cached --stat
```

Git 状态为空也可能有 `.env`、依赖、本地数据库等忽略文件；删除目录前仍要完整备份。

Git 的普通 `worktree remove` 要求工作区没有跟踪文件修改和未跟踪文件；脏目录会被拒绝。**这通常是在保护成果，不是让你加 force 的提示。** [Git worktree remove 官方说明](https://git-scm.com/docs/git-worktree)

若已 released 且不再继续协议流程，可在该目录做一次**本地归档提交**，把代码变化保存到自己的分支。只在确认备份、内容及提交范围后主动执行：

```powershell
git -C $cleanupPath add --update
if ($LASTEXITCODE -ne 0) { throw '暂存已跟踪文件失败' }
# 有需要保留的非忽略新源码时，逐个使用真实路径，不要原样复制示例：
# git -C $cleanupPath add -- src/实际新增文件.js
git -C $cleanupPath diff --cached --stat
git -C $cleanupPath diff --cached
# 暂存区有已审查内容才执行；没有变化就跳过 commit：
git -C $cleanupPath commit -m 'chore(archive): 保存已结束任务的本地代码成果'
if ($LASTEXITCODE -ne 0) { throw '归档提交失败，停止清理' }
git -C $cleanupPath status --short --untracked-files=all
```

归档提交会改变该目录 HEAD，**仅用于协议已结束的历史目录**，不能在还要 resolve/publish/resume 的候选或任务上这样操作。它不应再回填，不自动推送。未跟踪文件没有全部处理时目录仍脏，应保留现场，不强删。

这些提交保存文件结果，不会自动包含被忽略文件或未暂存的新文件。已回填的任务源码与 HEAD 仍有差异是正常现象：协议用内容快照回填，没有自动生成任务 commit。

不确定哪些文件能提交时，停在这里，让 AI 核查并给出具体归档清单，不要 `git add -A` 把秘密或运行数据全部纳入。

### 11.6 为每个归档分支保存 Git 历史

对当前待删目录取得实际分支，生成单独 bundle：

```powershell
$cleanupBranch = git -C $cleanupPath branch --show-current
if ($LASTEXITCODE -ne 0 -or -not $cleanupBranch) { throw '无法确认清理分支' }
$branchBackupFile = Join-Path $backupPath (('branch-' + [guid]::NewGuid().ToString('N')) + '.bundle')
git -C $projectPath bundle create $branchBackupFile $cleanupBranch
if ($LASTEXITCODE -ne 0) { throw '分支备份失败' }
git -C $projectPath bundle verify $branchBackupFile
if ($LASTEXITCODE -ne 0) { throw '分支备份校验失败' }
git -C $projectPath bundle list-heads $branchBackupFile
```

把输出分支名、目录与 bundle 文件对应关系记到本地备份说明中。每个候选分支分别保存。bundle 保存引用可达的提交历史；工作区、索引和未提交/忽略文件仍依靠前面的文件备份。[Git bundle 官方说明](https://git-scm.com/docs/git-bundle)

### 11.7 删除已备份、干净的关联 worktree

回到原项目，不要站在即将删除的目录中执行。一次只删一个从登记核实过的 worktree：

```powershell
Set-Location -LiteralPath $projectPath
$resolvedCleanupPath = (Resolve-Path -LiteralPath $cleanupPath -ErrorAction Stop).Path
$resolvedProjectPath = (Resolve-Path -LiteralPath $projectPath -ErrorAction Stop).Path
$approvedTaskPaths = @($cleanupTask.path) + @($cleanupTask.candidates | ForEach-Object { $_.path })
$resolvedApprovedPaths = @($approvedTaskPaths | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object {
    (Resolve-Path -LiteralPath $_ -ErrorAction Stop).Path
})
if ($resolvedCleanupPath -eq $resolvedProjectPath -or $resolvedCleanupPath -notin $resolvedApprovedPaths) {
    throw '目标不是已核实的本任务关联目录，停止删除'
}
git -C $projectPath worktree remove $resolvedCleanupPath
if ($LASTEXITCODE -ne 0) { throw '删除被拒绝，保留现场，不加 force' }
git -C $projectPath worktree list
```

这是你手动决定的真实删除操作，文档没有替你执行。候选与开发目录逐个处理；不要批量按 `parallel` 目录名删除。不要删除原项目、其他任务目录或正在运行程序的目录。

如果提示锁定，先确认锁的用途和归属，不为了删除而盲目 unlock。外接盘/网络盘临时掉线也不等于目录被永久删除。

### 11.8 分支需要删吗

不急着删。删除工作目录通常已能回收依赖和构建产物的大部分空间，保留分支便于找回。

确认分支已不被 worktree 使用、已归档且无独有待保留提交后，可尝试普通删除：

```powershell
git -C $projectPath branch -d $cleanupBranch
```

`-d` 会检查提交是否合并，拒绝时先保留。协议按内容回填，不保留任务 commit 历史，因此功能已回填但分支仍可能显示“未合并”；这并不证明成果丢失。本手册不把 `-D` 强删作为日常清理步骤。[Git branch 删除规则](https://git-scm.com/docs/git-branch)

### 11.9 文件夹删了，Git 仍显示它

先核实不是磁盘未挂载或目录被移动。如果只是手工移动，先找真实目录让 AI 核查 Git 与协议两层路径；不要 prune 掉尚有成果的注册。

确认目录确实已永久删除且备份完好，先预览残留 Git 注册。下面的 `--expire now` 会把近期丢失目录也列入检查，因此必须核对预览中的所有对象，包括其他任务：

```powershell
git -C $projectPath worktree prune --dry-run --verbose --expire now
```

只在预览对象全部明确可清理时，再执行 `git worktree prune --verbose --expire now`。不指定 `--expire now` 时，默认到期策略可能保留近期记录。prune 清理的是丢失工作区的 Git 管理记录，不删除仍存在的源码目录，也不清除本协议任务历史。[Git worktree prune 官方说明](https://git-scm.com/docs/git-worktree)

### 11.10 剩余 workspace 和历史登记怎么办

worktree 全部移除后，workspace 可能仍有 environment.json、旧提示词或其他文件。先确认每个残留文件已备份，且没有任何关联目录、服务或外部任务依赖它。需要继续删时，让 AI 根据这个任务的绝对路径给出具体残留清单；不要直接删除整个 `<项目名>.parallel`。

当前脚本**没有协议历史清理命令**。即使 Git worktree 已正常移除，status 仍保留历史 released 任务，doctor 可能报告这些任务目录缺失。这是当前登记保留方式的限制，不应对已归档任务反复 recover。

保留 `<git-common-dir>/parallel-code-tasks` 内的登记、日志、锁文件，以及 `refs/parallel-code-tasks/` 快照引用。手动改 project.json 或删除共享引用可能影响其他任务、诊断和恢复，不属于本手册支持的清理流程。不要为了让 doctor “全绿”删除历史证据。

### 11.11 清理后要找回文件

普通源码、忽略文件和配置：从完整文件备份提取需要的文件到新目录，核对后按项目流程使用，不覆盖原项目已有修改。

归档提交历史：将对应 bundle 克隆到**新的、尚不存在的目录**：

```powershell
$restorePath = 'C:/recovery/login-fix-1'
if (Test-Path -LiteralPath $restorePath) { throw '恢复目录已存在，请换新路径' }
git clone --branch $cleanupBranch $branchBackupFile $restorePath
if ($LASTEXITCODE -ne 0) { throw '从分支备份恢复失败' }
```

这是独立恢复仓库，不会自动接续旧协议任务或带回原数据库。已删除的 released 历史任务不 resume；有后续需求在原项目创建新任务，按规则引用恢复成果。

## 12. 任务状态速查

| 状态 | 中文含义 | 下一步 |
|---|---|---|
| creating | 正在创建或创建中断 | 停止写入并核查 recover |
| working | 开发中 | guard、开发、验证、ready |
| ready | 任务快照已冻结 | 审查后 prepare；继续改则 reopen |
| preparing | 集成正在准备或中断 | doctor、核查后 recover |
| conflict | 候选存在冲突 | 候选解决、暂存、inspect、resolve |
| prepared | 候选可审查 | 验证审查后 publish；过期重新 prepare |
| publishing | 正在回填或中断 | 核对 pending 和原目录，recover |
| published | 已回填，资源仍可能占用 | 停止写入者和服务后 release |
| parked | 未交付任务暂停 | 保留目录，以后 resume |
| released | 已交付且资源释放 | 原项目提交确认、归档，按需清理 |

## 13. 命令速查

以下子命令前都要有 `python $parallelScript --repo $projectPath`。任务操作还需要本任务 `--task` 和 `--token`，具体参数可用 `<子命令> --help` 查看。

| 命令 | 用途 |
|---|---|
| install / install --update | 安装或更新协议文件，保留用户修改 |
| enter | 创建新登记和开发 worktree |
| bundle --format text | 获取本任务接续提示词 |
| launch -- 程序 参数 | 在真实 cwd、任务环境中启动 CLI |
| guard | 检查当前任务目录、范围、基线和认领 |
| ready | 冻结任务树 |
| reopen --workers-stopped | 冻结后返回开发 |
| prepare --reviewed-tree 任务tree | 生成最新集成候选 |
| inspect | 读取候选现场及实际树 |
| resolve --reviewed-tree 实际候选tree | 登记已验证审查的冲突解决结果 |
| publish --reviewed-tree 候选tree | 串行回填原目录并暂存 |
| release --workers-stopped | 已交付则释放，未交付则暂停 |
| resume --workers-stopped | 恢复 parked 任务 |
| recover | 按具体状态恢复中断，不是通用撤销 |
| recover-lock | 核实失效 owner 后恢复旧目录锁 |
| status --format table | 查看状态、路径、资源和下一步 |
| doctor | 只读诊断 |
| events --limit 20 | 查看最近审计记录 |

更精确的协议条件见 [worktree.md](worktree.md)，环境配置见 [configuration.md](configuration.md)，提示词见 [agent-prompts.md](agent-prompts.md)。

## 14. 使用边界和每日操作清单

本协议适合**同一台机器、同一 Git common directory** 的多个独立写入任务。它是协作约定，不是认证、强制沙箱或跨机器调度器。绕过规则的程序仍可能访问其他目录。

本仓库既有行为验证主要在 Windows 完成；本手册不代表所有 AI 产品都已验证自动规则读取、目录隔离或环境加载。业务验收、真实外部服务隔离仍需要项目自己的证据。

每天开始：看 status；确认任务目录和旧写入者；暂停任务先 resume；开发任务 guard；按项目规则工作。

每天结束：记录实际进度和验证；需要暂停就先停服务再 release；保留 ID、接续码和目录；不要手工清空 worktree。

功能交付：ready 后审查、prepare 后验证审查、publish 后核对原目录、停进程再 release、按项目流程提交。

空间清理：只处理结束任务；先确认原项目成果提交、完整备份并验证，再逐个移除干净 worktree；分支和协议历史可以保留。
