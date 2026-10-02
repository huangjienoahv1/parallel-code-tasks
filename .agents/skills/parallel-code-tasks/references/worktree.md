# 操作说明

scripts/parallel_tasks.py 使用 Git 和 Python 标准库；只有显式 launch 才启动调用者指定的程序。所有工具调用同一版本。Windows 使用 pwsh.exe（PowerShell 7），Python 3.9+。

## 持久登记与目录

- 首个 enter 的 --repo 使用目标原项目；之后可从任意关联 worktree 查同一登记。
- 登记在 <git-common-dir>/parallel-code-tasks/project.json。不同克隆/不同主机不会自动共享，即使 remote URL 相同也不算同一登记。
- 每个任务目录为 <项目名>.parallel/<任务ID-随机ID>/work，独立分支；每次 prepare 新建 publish-<随机ID> 候选。目录均在原项目之外。
- task ID 在仓库内唯一，agent 是任意产品标签。token 用于防止误接管，存在本机登记中，不是密码或强身份认证。status 不显示 token；丢失时在确认所有者后查看本机登记，不猜测或重新领取同 ID。
- enter --worktree 可接入原生工具创建的 worktree，要求干净、同仓库、独立未分配分支、HEAD 等于登记的 source.head。原生归档由原生工具处理。
- 成功输出 JSON，失败在 stderr 输出 JSON 并返回非零码。第一次 status 的 source 是 null，不创建项目内容。
- V1 state.json 保留；有活动批次时拒绝混用。先用保存的 V1 版本处理完成或人工归档，不能直接删旧登记绕过。V2 不再使用 init/add/coordinator/handoff/close。
- 当前协议增强版本为 3，沿用 version=2 的登记结构，读取既有任务时补默认字段，保留原路径/凭据/基线。升级前停止旧版协议命令，不让目录锁和内核锁混用。
- 全局 mutex.lock 保护共享登记、worktree 注册和回填；task-locks/<ID>.lock 保护本任务变更。任务快照、候选应用和 CLI 等待在全局锁外，最终 publish 仍检查共享基线。
- events.jsonl 记录操作、状态、候选和失败类型；不记录环境值、接续码及完整启动参数。登记成功但日志写入失败会明确报告，不能假定操作未发生。

## 安装、升级及规则入口

~~~powershell
$parallelScript = 'C:/tools/parallel-code-tasks/.agents/skills/parallel-code-tasks/scripts/parallel_tasks.py'
$projectPath = 'C:/code/project'
python $parallelScript --repo $projectPath install
# 多个实际 Markdown 入口可以一起安装，保留文件已有内容：
python $parallelScript --repo $projectPath install --rules-file AGENTS.md --rules-file CLAUDE.md
# 已有安装需升级时，只允许覆盖上次安装后未被用户改动的 Skill：
python $parallelScript --repo $projectPath install --update
~~~

首次安装创建默认空配置和完整 Skill，规则使用唯一 begin/end 标记。重复安装不追加重复块；保留既有规则 BOM/换行和标记之外内容。非 Markdown 产品规则入口手动接入；不修改全局产品配置。更新所有目标前先预检，权限或磁盘故障仍可能造成部分写入，检查返回错误及实际文件后处理。

安装会产生可审查的未提交文件，请按自己的项目流程授权纳入 Git 基线，再开始并行任务。脚本不自动提交；未纳入基线的规则要在接续包中通过原目录绝对路径读取，未知安装改动会阻止 prepare/publish。

## 从任意工具加入

以下在 PowerShell 7 中执行；脚本路径固定指向同一份协议：

~~~powershell
$parallelScript = 'C:/path/to/parallel-code-tasks/scripts/parallel_tasks.py'
$projectPath = 'C:/code/project'
python $parallelScript --repo $projectPath status
$entry = python $parallelScript --repo $projectPath enter --agent codex --task login-fix --goal '修复登录过期提示，保持接口不变' --scope src/login --resource port:auto
if ($LASTEXITCODE -ne 0) { throw '登记失败，请查看错误' }
$task = ($entry | ConvertFrom-Json).task
$task | Select-Object id, path, branch, baseline_head, baseline_tree, token
~~~

其他工具分别用新 task ID、自己的 agent 标签和资源执行 enter，无须先指定协调者。身份可以是 zcode、opencode、cc、antigravity 或任意标签，同一产品的多个任务也分别登记。

**接下来在产品里打开 task.path，并给新/接续会话带入任务信息。** Set-Location 只切换当前终端，不能保证产品编辑器也切换：

~~~powershell
Set-Location -LiteralPath $task.path
python $parallelScript --repo . guard --task $task.id --token $task.token
~~~

guard 要求真正命令 cwd 在该 worktree 内。重启同任务先 guard；已暂停任务先 resume --workers-stopped。产品不能读取 skill 时使用 agent-prompts.md 的通用任务提示词。

## 接续包和 CLI 启动

~~~powershell
python $parallelScript --repo $projectPath bundle --task $task.id --token $task.token --format text
# 将上面的自包含提示词粘贴到实际 worktree 的产品会话。
# CLI 以真实可执行程序和参数数组启动；请替换为本机产品入口：
$toolExecutable = 'C:/tools/your-agent/agent.exe'
python $parallelScript --repo $projectPath launch --task $task.id --token $task.token -- $toolExecutable
~~~

bundle 带入目标、路径、分支、基线、scope、认领、资源、原项目规则和统一脚本。接续码属于任务上下文，别贴到其他任务。必要的验收细节由任务执行者补齐。

launch 设置真实 cwd、校验并注入任务环境、记录程序名/PID/创建时间/退出码。不通过 shell 拼接，也不自动猜测 CLI 提示参数或发送消息；当前工具如何接收任务提示以其本机入口为准。CLI 输出直接传递，结束后再输出包装结果 JSON，所以整段 stdout 不保证是单个 JSON。程序退出不自动释放资源，也不证明其所有子进程/服务已停止。

等待 CLI 时释放任务锁，子进程可在自己的目录调用 guard/ready；同任务前次启动仍运行或无法确认时拒绝重复 launch。配置与白名单见 [configuration.md](configuration.md)。

## 范围、资源和基线

scope 可重复，使用具体仓库相对文件或目录，不支持 glob、..、Git 元数据。目录覆盖后代，改名检查旧新两端。确实负责整个项目才用 "."。同文件可在独立 worktree 开发，最终仍可能冲突；公共契约和紧密依赖先安排次序。

resource 可重复：port:18081、port:auto、db:主机/库/schema、redis:主机/库/前缀、docker:上下文/项目名、path:C:/temp/task。使用同一命名规则；path 拒绝相同和嵌套路径。数据库名称相同但别名不同不会自动识别，须明确实际连接身份。自动端口仅探测当下可绑定，不持续占用，也不协调其他仓库。关键文件用 --exclusive 或配置 exclusive_paths 认领；普通 scope 可以重叠。

enter 创建的基线是 source.head 的已提交内容加已知成功回填树，后者可仍在原目录暂存区。baseline_tree 记录实际起点，ready 差异以它为准，避免重复交付继承代码。源码未提交变化通过快照补丁进入新 worktree 暂存区；不自动生成提交。

原目录有未知未提交内容时允许从最后已知基线隔离开发，但 prepare/publish 拒绝回填；这部分原内容不进入任务。干净的新提交，或用户提交/重新暂存后的完整已知成果可自动接受；不同于已知成果的未暂存改动不会覆盖。

原目录切换分支、目录丢失、未解决 Git 操作、sparse checkout、submodule 均需先明确处理；脚本拒绝支持不充分的情况。Git 过滤器及 worktree hooks 仍按实际仓库配置执行。

## 单个任务交付

~~~powershell
# 在自己的 worktree 开发，执行项目需要的验证。
$readyJson = python $parallelScript --repo . ready --task $task.id --token $task.token
if ($LASTEXITCODE -ne 0) { throw '冻结失败' }
$ready = $readyJson | ConvertFrom-Json
# 检查实际差异和验证证据；不是仅复制 SHA。
git diff $task.baseline_tree $ready.tree --
$prepareJson = python $parallelScript --repo . prepare --task $task.id --token $task.token --reviewed-tree $ready.tree
if ($LASTEXITCODE -ne 0) { throw '候选准备失败；status 查看保留的现场' }
$candidate = ($prepareJson | ConvertFrom-Json).candidate
# 在 candidate.path 执行整体验证并检查最终差异。
git -C $candidate.path diff $candidate.source_tree $candidate.tree --
# 审查通过才回填；其他任务仍可继续开发。
python $parallelScript --repo . publish --task $task.id --token $task.token --reviewed-tree $candidate.tree
# 实际停止所有相关执行者和服务后：
python $parallelScript --repo . release --task $task.id --token $task.token --workers-stopped
~~~

每次命令都检查退出码再执行下一步。脚本不代替业务审查或构建，--reviewed-tree 是执行者确认具体成果已审查。prepare 的 task SHA 与 publish 的 candidate SHA 用途不同。

ready 使用临时索引，包含非忽略新文件及显式暂存的文件（含 force 添加的忽略文件），不修改任务真实索引；未跟踪的忽略文件、运行状态/数据库不包含。prepare 使用三方补丁，publish 先检查再应用到原目录索引，保持结果暂存；不保留任务 commit 历史。

快照树用 refs/parallel-code-tasks/ 保留避免 GC 回收，不创建 commit；不会替代业务分支的提交历史。不要手动删除这些共享引用。脚本不修改全局配置、不推送、不强制清理、不删除 worktree/分支。

## 冲突、过期、暂停、中断

- conflict：status 找到该任务的当前 candidate/path。只在该候选解决冲突，git add 相关文件，必要验证后 inspect 得到 actual_tree；审查此树后 resolve --reviewed-tree <actual_tree>，再 publish。其他任务照常进入/开发/交付。
- prepared 已过期：其他任务先回填或用户产生新提交，publish 拒绝。再次 prepare --reviewed-tree <同一任务tree> 创建新候选，保留旧目录；验证、重新审查新候选再 publish。不要把旧候选修复直接覆盖原目录。若新候选仍冲突，按语义重新解决。
- ready 后修改：先 reopen --workers-stopped，使旧候选失效；修订后重新 ready。不能用旧快照 SHA。
- 暂停：确认全部写入者、服务停止后 release --workers-stopped；状态 parked，资源释放但目录/成果保留。之后 resume --workers-stopped 重新占用资源并恢复暂停前状态，基线不自动变化。已交付任务 released 后新需求新建 ID。
- publishing/pending：执行 recover。已完整应用则完成登记，未应用则恢复 prepared；HEAD/树/索引有额外改动则拒绝，保留现场人工核查。不要盲目再 apply。
- preparing：停止任务/候选写入后 recover --workers-stopped --reviewed-tree <任务tree>。发现真实未解决冲突则登记 conflict；其余场景保留旧候选，创建新候选重新验证及审查，不把未知部分应用宣称为成功。
- creating：停止写入后 recover --workers-stopped。仅恢复缺失的受管 worktree 或内容精确等于已提交/完整基线的目录，再复制白名单和配置环境；HEAD、索引或文件存在未知内容时拒绝，保留现场。不强制移除 worktree 注册，不直接重新 enter 同 ID。
- 启动记录 starting/running/interrupted：先 doctor 核对 launch.id，实际停止全部相关进程。登记进程必须已退出，才能 recover --workers-stopped --expected-launch <launch.id>；未记录 PID 的启动窗口依赖执行者确认，不能证明程序自动发现或停止子进程。只结束登记，不释放资源。
- 内核锁：进程退出自动释放，锁文件永久保留。残留 owner 只是诊断记录，下一次合法获取锁可替换它；不要删除锁文件制造第二个锁。
- 旧目录 lock：doctor 返回 owner 主机/PID、进程状态及 fingerprint。确认为同主机失效 owner 并停止旧版脚本后 recover-lock --workers-stopped --expected-owner <fingerprint>。仅删除对应 owner 和空目录；活进程、其他主机、未知内容或指纹变化均拒绝。

共同锁只保护遵守脚本的登记/回填。Git 操作之外的文件编辑没有全局强制锁，main 原目录上的外部写入可能造成竞争，所有参与者必须遵守原目录只用于接收成果的规则。

## 状态、日志及只读诊断

~~~powershell
python $parallelScript --repo $projectPath status --format table
python $parallelScript --repo $projectPath doctor
python $parallelScript --repo $projectPath events --limit 20
~~~

status 默认 JSON，隐藏任务接续码和配置环境值。table 直接显示状态、路径、是否保留资源/认领及下一步。doctor 不创建登记、修改文件或自动修复，只探测已有锁并读取实际 Git 状态；原目录脏可能是已知暂存成果，由 prepare/publish 进一步核对。

--lock-wait 位于子命令之前，范围 0..30 秒，默认 5。等待超时明确失败，不按超时抢占；外部长进程或慢磁盘使短暂竞争失败时，核实 doctor 后再执行，不无限重试。events 的损坏行会显式列出，保留日志供检查。
