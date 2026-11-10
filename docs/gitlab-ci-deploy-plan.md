# 本地 GitLab CI 构建 ZIP 并存入 PostgreSQL

## 目标与范围

在当前 Mac 的 Docker 中部署 GitLab CE 和 Runner，将代码托管到 GitLab。主分支推送或手动发布时，流水线完成测试、前端编译和 ZIP 打包，再将完整产物写入本机 PostgreSQL。

**发布 ZIP 内的 `config/config.json` 必须是通过 GitLab CI File 变量注入的真实配置。** 真实配置继续被 Git 忽略，不提交到 GitLab 或 GitHub 的代码仓库。GitLab 变量负责向构建提供配置，PG 中的 ZIP 保存该次发布的配置快照。

本次完成构建到入库的闭环，不设计从 PG 下载 ZIP 的接口或工具，不自动部署、重启应用或清理历史产物。目标 Linux 联网重建 uv 环境；ZIP 不携带构建机的 `.venv`。

```text
主分支提交或手动运行
    → 自动测试
    → 编译前端
    → 注入 GitLab File 变量中的真实配置
    → 打包并校验 ZIP
    → 事务写入 PostgreSQL deploy 表
```

## 本地基础设施

### GitLab 和 Runner

使用 Docker Compose 管理 GitLab CE 和 GitLab Runner，均采用官方 ARM64 镜像。调研时本机为 ARM64、32 GB 内存，Docker 分配约 4 GB，已有 PostgreSQL 18 和 NATS 容器。计划将 Docker 内存调整为 8 GiB，Runner 同时只执行一个任务；验收时记录实际资源峰值。

- GitLab 镜像以已核实提供 ARM64 的 `gitlab/gitlab-ce:19.5.3-ce.0` 为部署基线，固定版本和 digest；Runner 同样固定兼容版本和 digest。
- 持久化 GitLab 配置、日志、数据及 Runner 注册配置，采用 Docker named volumes。部署说明包含 GitLab 数据和 secrets 的备份方法。
- GitLab 保留自身内置 PostgreSQL 和 Redis；产物使用现有业务 PG，两者独立。
- 使用单 Puma 进程、Sidekiq 并发 10，关闭 Prometheus、exporters、KAS，不启用 Registry、Pages。
- 浏览器入口为 `http://gitlab.test:8929`，Mac hosts 指向 `127.0.0.1`；SSH 使用本机 `2222` 端口，仅监听本机。
- GitLab、Runner 和作业使用专用 Docker 网络，设置 `gitlab.test` 别名，使容器能够访问相同的 GitLab URL。
- Runner 使用 Docker executor；只有 Runner 管理容器挂载 Docker socket，构建作业不挂载 socket、不启用 privileged。
- 创建私有 `laxcode_v2` 项目、保护 `main`，注册项目专属 Runner。新增 `gitlab` remote 验证推送，保留已有 remote。

资源裁剪参考 [GitLab 低内存部署](https://docs.gitlab.com/omnibus/settings/memory_constrained_envs/)，容器部署参考 [GitLab Docker 安装文档](https://docs.gitlab.com/install/docker/installation/)。8 GiB 是本项目的部署预算，不是官方最低要求。

### PostgreSQL 连接

本机作业通过 `host.docker.internal:5432` 连接现有应用数据库。先从实际作业容器验证连通性，再执行首次发布。

发布连接使用独立的 `DEPLOY_PG*` 环境变量，不从包内真实配置读取。这样切换产物存储位置时，只需调整 GitLab 变量，不必修改应用配置或代码。未来生产连接使用证书校验。

## 构建与配置注入

### 流水线阶段和触发规则

采用 `test → build → verify → publish` 四阶段：

1. **test**：执行 Python 回归、前端单元测试。PG/NATS 使用临时 CI services，测试不连接产物库或真实应用数据库。
2. **build**：执行 `npm ci`、`npm run build`；发布流水线随后注入真实配置并创建 ZIP。
3. **verify**：检查 ZIP 的文件清单、CRC、配置一致性和依赖可安装性。解压到新目录，按锁文件重建运行环境并验证 Python 导入，不使用包内配置连接真实外部服务。
4. **publish**：读取已验证的同一个 ZIP，以单事务写入 `deploy`。输出记录 ID、版本、字节数和整个 ZIP 的 SHA-256，不输出配置正文或凭据。

`main` 推送及 GitLab 页面手动运行 `main` 流水线均发布。其他分支和 MR 运行测试、前端构建及使用测试配置的打包校验，不注入真实配置、不生成可发布产物、不写产物库。发布不接受来自这些流水线的 ZIP。

固定 Node、Python、uv 构建工具版本；Python 基线为 3.12，并在 ZIP 中提供 `.python-version`。CI 使用 `uv sync --locked`，运行依赖验证使用 `--no-dev`，锁文件不匹配时失败，不自动更新。参见 [uv 锁定与同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)。

### 真实配置的来源

在 GitLab 项目的 Settings → CI/CD → Variables 中配置：

| 项目 | 约定 |
| --- | --- |
| Key | `LAXCODE_CONFIG_JSON` |
| Type | File |
| Value | 完整的真实 `config.json` 内容 |
| Protected | 开启，仅向受保护分支发布提供 |
| Expand variable reference | 关闭，避免配置中的 `$` 被当作变量展开 |

GitLab 将内容写入作业临时文件，环境变量 `LAXCODE_CONFIG_JSON` 的值是该文件路径，不是 JSON 正文。发布脚本必须读取文件，不能将变量值直接当作 JSON。

初次配置时，将本机真实配置录入该变量；以后本机文件变化不会自动同步，需要更新 GitLab 变量。一次发布以当次作业读取到的配置为快照，后续变量更新不修改已入库 ZIP。多行 JSON 不依赖 GitLab masked 功能兜底，作业不得回显变量内容、开启 shell trace 或将配置放入调试日志。

配置中的数据库、NATS、模型服务地址必须适用于最终运行环境。产物入库地址与应用运行地址是两个独立配置，不因本次写入本机 PG 而自动改写真实配置。保留现有配置目录结构，`frontend.dist_dir` 使用相对配置目录的 `../frontend/dist`，确保解压后前端路径可用。

### 注入和校验流程

构建脚本在独立临时打包目录中操作，不修改 Git checkout 下的 `config/config.json`，不回退读取 Runner 宿主机的本地文件。

1. 检查 `LAXCODE_CONFIG_JSON` 已设置，指向可读且非空的普通文件；缺失则终止发布构建。
2. 用受限文件权限将文件原样复制到打包目录的 `config/config.json`，例如 `install -m 600`。构建期间所有检查均针对这个已复制的快照。
3. 校验 UTF-8 JSON，并调用项目配置加载逻辑校验 `AppConfig`；保留相对路径解析语义，验证前端 dist 实际存在。校验不建立业务数据库、NATS 或 LLM 连接。
4. 记录配置文件 SHA-256 到内部校验结果；打包后读取 ZIP 中的 `config/config.json`，核对字节数和 SHA-256，保证没有打入示例配置或替换内容。配置校验信息不回显到普通日志。
5. 缺少真实配置、JSON 非法、字段校验失败、前端路径错误或包内内容不一致，均终止流水线，不执行发布；错误只报告字段位置和问题，不包含配置值。

JSON 合法和结构校验不能证明远端凭据有效；实际连通性通过指定目标环境冒烟验证。构建不尝试将无效值自动修复或替换为示例值。

### ZIP 内容与访问

文件名为 `laxcode-<version>.zip`，解压后只有一个同名顶层目录：

```text
src/laxcode/
frontend/dist/
config/config.json          # GitLab CI File 变量注入的真实配置
config/config.example.json
migrations/
alembic.ini
pyproject.toml
uv.lock
.python-version
scripts/prepare_bash_sandbox.sh
docs/bash-sandbox.md
manifest.json
DEPLOY.md
```

从当前提交按文件白名单收集源码及构建后的前端，再单独加入注入配置。排除 `.git`、`.venv`、缓存、`node_modules`、测试输出、其他 `.env` 和宿主机文件。源码与前端必须来自同一提交。

`manifest.json` 记录 UUID、UTC 构建时间、Git commit、流水线编号、Python/uv 版本及包内文件校验信息；不保存配置正文或凭据的独立副本。

ZIP 含真实凭据，因此 GitLab 发布产物访问限定为 Maintainer，临时产物保留 7 天，并关闭该项目“始终保留最近成功流水线产物”的默认保留行为。配置和含配置的打包目录不进入 CI cache。PG 中产物全部保留，访问权限限于发布及运维账号。

目标机安装 uv、准备好配置引用的 PG/NATS 并完成业务数据库迁移后，预期运行命令为：

```bash
uv run --locked --no-dev python -m laxcode.main --config config/config.json
```

首次运行联网下载 Python 和依赖，不分发 `.venv`。源码包不绑定 CPU 架构，但依赖必须支持目标平台；完整 Bash 工具仍依赖 bubblewrap、内核及系统工具。首期完整运行验收沿用 Ubuntu 24.04 ARM64。参见 [Python 虚拟环境可迁移性说明](https://docs.python.org/3/library/venv.html)。

## 接口文档

HTTP 接口无新增或修改，本次不设计产物下载接口。

新增内部构建脚本接口：

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `uv run python scripts/build_release.py --output-dir release` | 当前提交源码、已编译前端、`LAXCODE_CONFIG_JSON` File 变量 | 包含真实配置的 ZIP 及构建摘要 |
| `uv run python scripts/publish_release.py --artifact <zip>` | 已验证 ZIP、PG 连接环境变量 | 记录 ID、版本、构建时间、产物大小及 ZIP SHA-256 |

构建脚本在发布模式下必须有 File 变量，不接受使用示例配置替代。非发布分支的打包测试通过测试代码提供独立临时配置，不发布测试包。

发布环境变量为 `DEPLOY_PGHOST`、`DEPLOY_PGPORT`、`DEPLOY_PGDATABASE`、`DEPLOY_PGUSER`、`DEPLOY_PGPASSWORD`、`DEPLOY_PGSSLMODE` 和可选的 `DEPLOY_PGSSLROOTCERT`。密码保存在受保护、masked/hidden 的 GitLab 变量中，不进入代码、ZIP 或日志。这里不影响应用真实配置本身可能包含的业务数据库凭据。

数据库管理员负责建表和授权。发布账号只授予 `deploy` 的 INSERT、SELECT 以及 identity 序列所需权限，不授予 DDL、UPDATE、DELETE 权限。变量行为参考 [GitLab CI/CD 变量文档](https://docs.gitlab.com/ci/variables/)。

## 数据库建模

在现有应用数据库中新增 `public.deploy`，保持四个字段：

```sql
CREATE TABLE public.deploy (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version uuid NOT NULL UNIQUE,
    built_at timestamptz NOT NULL,
    artifact bytea NOT NULL
);
```

| 字段 | 约定 |
| --- | --- |
| `id` | 自增整数，只表示数据库记录标识 |
| `version` | 每次打包生成 UUIDv4，与包内清单一致 |
| `built_at` | 打包阶段产生的 UTC 时间，重试上传时不改变 |
| `artifact` | 包含真实配置的完整 ZIP 原始二进制，通过参数化 SQL 写入，不使用 Base64 |

源码及静态资源包体积较小，`bytea` 可以与元数据一起事务提交，无需引入 Large Object 生命周期管理。发布脚本限制 ZIP 最大 100 MiB，超过时直接失败，不截断、不自动改存其他位置。这是项目的内存和存储控制值，低于 PostgreSQL 单字段 1 GB 的上限。参见 [二进制类型](https://www.postgresql.org/docs/current/datatype-binary.html)和 [PostgreSQL 限制](https://www.postgresql.org/docs/current/limits.html)。

相同 ZIP 重试发布沿用包内 UUID。UUID、构建时间和内容相同则视为成功；UUID 相同而内容或构建时间不同则失败，禁止覆盖。通过唯一约束和事务处理并发重试；失败回滚，不保留半成品。表内每条记录均表示成功存储的完整产物，无需状态字段或业务表外键。

沿现有 Alembic 链新增迁移，版本号根据实施时实际 head 顺延，同时更新应用 schema revision 校验和迁移测试。首次发布前先完成本机数据库迁移；未来切换生产产物库前，同样先准备表和账号。历史产物全部保留，人工按需要清理。

## 实施顺序与验收

1. 部署 GitLab 和 Runner，验证登录、SSH 推拉、作业拉代码及重启后持久化；记录空闲和构建峰值资源。
2. 新增数据库迁移和发布账号，验证新库与已有库升级及现有业务回归；临时 CI services 与产物库分离。
3. 配置受保护的 File 变量和发布数据库变量，执行一次真实 `main` 流水线。
4. 检查 ZIP 的源码、前端、manifest 提交号一致，真实配置与 File 变量快照逐字节一致，其他本地文件、凭据文件及虚拟环境未被打入。
5. 验证 File 变量缺失、空文件、JSON 非法、配置校验失败、配置内容不一致均失败且不入库；非受保护分支和 MR 无真实配置发布产物，日志不包含测试用敏感标记。
6. 验证损坏 ZIP、超限、数据库凭据错误、重复上传及 UUID 内容冲突，失败不留下半成品记录。真实入库后比较数据库产物长度和 SHA-256 与本地 ZIP 一致；测试只在内部核对配置内容，不输出它。
7. 按项目约定通过 `scripts/deploy_lima.sh` 准备 Lima 冒烟环境，在独立目录验证 ZIP 联网安装、前端访问和服务启动。注入配置为目标环境配置；若验收环境不同，使用单独的验收配置显式传给 `--config`，不改写已封装 ZIP。完整 Bash 能力按现有 sandbox probe 验证。

交付 Compose 配置、Runner 模板、CI 定义、构建与发布脚本、数据库迁移和部署说明。结束条件是一次真实主分支流水线将包含注入真实配置的完整 ZIP 写入本机 PG，并通过一致性校验。本文件为实施计划，不表示上述基础设施和流水线已经部署。
