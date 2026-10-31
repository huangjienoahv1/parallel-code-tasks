# 项目配置与独占路径

`install` 在原项目创建 `.parallel-code-tasks.json`，已有文件只校验，不覆盖。配置每次 enter 读取，冻结在本任务登记中；修改配置不改变已开始任务的环境或基线。使用 UTF-8 JSON，版本为 1；未知字段、类型错误或缺失模板变量会明确拒绝。

## 可复制的示例

下面示例需按真实项目路径及资源命名调整；`.env.local` 必须在原目录存在，并由已知代码基线的 `.gitignore` 忽略。

~~~json
{
  "version": 1,
  "copy_files": [".env.local"],
  "resource_templates": [
    "port:auto",
    "db:localhost/dev/schema_${task}",
    "redis:localhost/0/${task}",
    "docker:local/${task}"
  ],
  "env": {
    "SERVER_PORT": "${port}",
    "APP_SCHEMA": "schema_${task}",
    "REDIS_KEY_PREFIX": "${task}:",
    "COMPOSE_PROJECT_NAME": "${task}"
  },
  "exclusive_paths": ["package-lock.json", "db/migrations", "src/contracts"],
  "port_range": [18000, 18999],
  "verification": {
    "commands": [],
    "timeout_seconds": 300
  }
}
~~~

这只提供分配与启动参数。项目必须实际使用 SERVER_PORT 等变量；数据库/schema 的建立、账号权限和迁移仍由项目流程负责，不能只登记一个名字却连接同一个库。

| 字段 | 行为 |
|---|---|
| copy_files | 具体相对文件白名单，独立字节复制；拒绝逃逸路径和符号链接 |
| resource_templates | 与命令行资源合并，检查同仓库已保留资源；port:auto 自动选择范围内端口 |
| env | 字符串环境变量模板，由 launch 注入子进程；图形产品需按实际启动方式显式加载 |
| exclusive_paths | 与当前 scope 相交的配置路径自动认领，其他路径不认领；交付检查他人认领 |
| port_range | 自动分配端口的闭区间，默认 18000..18999 |
| verification | 候选验证命令与单条超时；默认 commands 为空，沿用人工验证 |

模板支持 `${task}`、`${worktree}`、`${source}`；env 还可使用本任务已声明的 `${port}`、`${db}`、`${redis}`、`${docker}`、`${path}`，同类型多个资源时使用第一个。`$$` 表示字面量 `$`。模板不会执行命令。

## 文件、环境与外部资源

跟踪文件使用已知代码基线，不复制原目录未知改动。未跟踪文件仅在源和目标都被 Git 忽略时按白名单复制；缺失或目标已有不同内容会拒绝。不会共享链接、依赖目录或自动复制全部忽略文件。修改白名单文件不改变原目录文件。

环境文件保存在任务 workspace 的 `environment.json`，位于代码 worktree 之外。登记保存路径、变量名及哈希，普通输出和日志不展开环境值；launch 校验哈希，文件被修改时拒绝启动。文件可能包含敏感值，受本机权限保护，不能把运行登记上传 GitHub，也不应在公开项目配置中写密码。

配置不能覆盖 PATH、HOME、USERPROFILE、CODEX_HOME、GIT_* 或 PARALLEL_*。launch/verify 保留正常继承环境，移除 GIT_* 重定向变量及父进程残留的 PARALLEL_*，再注入本任务/候选的 PARALLEL_TASK_ID/WORKTREE/SOURCE、已声明资源的 PARALLEL_PORT/DB/REDIS/DOCKER 和业务 env。退出码真实透传，不自动 release。

port:auto 在共享登记锁内查询本仓库预留并短暂探测 127.0.0.1 可绑定状态。探测后不持续绑定：外部程序仍可能抢用端口，实际服务启动失败时需按错误处理，不能宣称全机互斥。

资源类型为 port、db、redis、docker、path。port 规范化数字；db/redis/docker 名称忽略大小写；path 使用绝对路径并拒绝相同及父子目录。用真实主机、库、schema、缓存前缀及 Docker 上下文命名，别名不同不会被识别为同一外部资源。

## 独占与普通并行

`enter --exclusive <相对文件或目录>` 可重复。路径必须与本任务 scope 相交；不能用 glob、.. 或 Git 元数据。配置认领和命令行认领合并，同路径及父子目录冲突会拒绝，在 core.ignorecase=true 的仓库忽略大小写。

其他没有认领的任务仍可以登记并修改普通文件；guard、ready、prepare、resolve、publish 检查实际修改不能触碰他人保留的独占路径。直接编辑或绕过脚本仍无法被文本规则阻止。

认领与资源一起保留到显式 release。resume 会重新检查占用。没有认领的普通同文件修改保持可并行，最后三方合并及业务审查。

## 候选验证配置

例如 Python 项目可按自身验收要求配置语法检查；其他项目替换为实际构建或检查程序：

~~~json
{
  "verification": {
    "commands": [["python", "-m", "compileall", "-q", "src"]],
    "timeout_seconds": 300
  }
}
~~~

这是合入现有配置的字段片段。commands 是 argv 数组列表，不接受 shell 命令字符串；按顺序执行，任一命令失败即停止。Windows 的 PowerShell 脚本显式使用 `["pwsh.exe", "-NoProfile", "-File", "scripts/check.ps1"]`。不进行命令参数模板替换，程序通过环境变量取得任务路径和资源。超时为每条命令的正整数秒数；交互 stdin 被关闭，命令不应启动脱离自身生命周期的后台服务。

- 配置在 enter 时冻结；原有配置缺少该字段时等同于空 commands，已有任务不会因为后来添加配置而改变规则。
- prepare 从**任务目录**复制白名单忽略文件的当前内容到候选；跟踪文件只使用合并结果。白名单路径必须在任务和候选都被忽略，符号链接被拒绝。没有共享可写依赖目录。
- 候选环境位于 `<workspace>/candidate-<ID>/environment.json`，`${worktree}` 与 `PARALLEL_WORKTREE` 指向候选。端口、数据库等复用任务登记，verify 前必须实际停止开发服务与旧验证进程。
- 验证输出写入 `<workspace>/candidate-<ID>/verification/<运行ID>/command-N.log`；stdout 返回 JSON，记录命令序号、程序名、参数摘要哈希、退出码和日志位置。命令非零退出会透传；超时返回非零并标记 timeout。
- 成功记录绑定候选 tree、受管环境文件、已复制白名单内容和冻结验证配置。publish 在回填前核对。修改白名单文件后可重新 verify；改动环境 JSON 需重新 prepare。源码变化按 reopen/prepare 或冲突 resolve 流程生成新候选树。
- 指纹不覆盖全部继承的系统环境、其他忽略文件或外部数据库内容，项目仍需验证这些依赖。日志可能包含业务程序输出，应由本机权限保护，不上传运行登记和日志。
- verify 持有本任务锁但不在运行命令时持全局锁；同任务的 prepare/publish/release 不能并发执行。超时和 Ctrl+C 只停止直接验证子进程，其他后代进程和服务需人工核查；硬中断遗留 running 记录时用 recover --workers-stopped 结束记录，再重验。
