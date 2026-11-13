# Compose 部署说明

需要 Docker Engine 和 Compose v2+；`docker-compose` 与 `docker compose` 均可。此目录独立于 `scripts/deploy_lima.sh`，应用仍使用原有 JSON 配置格式。

三个服务及容器名为 `laxagent`、`laxagent-pg`、`laxagent-nats`，只发布应用的 `8000` 端口。`config.json` 是唯一用户配置，按文件只读挂载，不进入镜像；PG 从其中读取初始化库名、用户名和密码。容器内部地址与端口已经配好。

应用以普通账号运行，移除全部 capabilities，不使用 privileged 模式，不挂载 Docker socket。为支持现有 bubblewrap，应用容器的 seccomp、AppArmor 和 systempaths 设置为 unconfined；这会放宽 Docker 的外层限制，工具文件访问继续由现有 bubblewrap 挂载规则约束。PG 与 NATS 使用默认 Docker 安全策略。

## Linux 宿主的 user namespace

内核必须允许非特权 user namespace。Ubuntu 24.04 开启 `kernel.apparmor_restrict_unprivileged_userns=1` 时，还需要允许 `/usr/bin/bwrap` 创建 user namespace；仅设置容器 `apparmor:unconfined` 不够。应用启动会先执行现有 sandbox probe，失败时退出，不降级到无沙箱执行。

若宿主尚无 bwrap AppArmor 策略，可安装本目录提供的专用策略：

```bash
sudo install -m 0644 deploy/compose/laxagent-bwrap.apparmor /etc/apparmor.d/laxagent-bwrap
sudo apparmor_parser -r /etc/apparmor.d/laxagent-bwrap
```

已有 bwrap 策略时保留并检查原策略，不安装第二份同路径策略。无需关闭宿主 AppArmor 或修改全局 user namespace 限制。Docker Desktop 的 Linux 内核仍需支持相同的 namespace 能力。

## 数据保留

PG、JetStream、用户文件分别使用独立命名卷，普通 `down` 或重建容器会保留数据。应用使用现有 `~/laxcode` 数据路径，容器中挂载至 `/home/laxagent/laxcode`。

PG 初始化参数仅在空数据卷上生效。已有数据时保留数据库并通过 SQL 修改密码，再同步 `config.json`、重建应用容器；需要重新初始化时，删除 PG 容器和 PG 数据卷会清空数据库。不要用 `down -v` 代替只删除 PG 卷，它还会删除事件和用户文件。

`config.json` 随仓库提供的 JWT secret 和 PG 密码是公开默认值；填写个人模型 Key 后，不要提交该文件的本地修改。
