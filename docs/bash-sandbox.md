# Bash 工具与 bubblewrap 部署

## 使用约定

`bash` 每次调用在新的 Linux bubblewrap 沙箱中执行一次非交互命令。bubblewrap 不需要 Docker 镜像或守护进程，它通过 Linux namespace 和挂载参数构造文件系统视图。

```json
{"command":"python3 --version && go version","timeout_seconds":null}
```

两个字段均必填，禁止额外字段。`command` 非空且不能含 NUL；`timeout_seconds` 为 1～300 的整数，`null` 使用 120 秒。命令初始目录就是项目 workspace；需要子目录时在命令中 `cd`，不会改变后续调用的初始目录。

- workspace 是唯一持久可写的宿主目录；系统工具目录只读。
- `/tmp` 与最小 `/dev`（包括 `/dev/shm`）每次重新创建，命令结束后销毁。**中间文件可以放 `/tmp`，最终代码、报告和其他交付文件必须放 workspace。**
- 网络与宿主共享，可访问互联网及宿主网络服务；本版不提供网络隔离。
- shell 环境、工作目录变化和后台进程不跨调用保留。`sleep &`、`nohup`、`setsid` 都不能用于保留后台服务。
- 命令失败、超时、取消或后续消息/事件失败，不回滚已写入 workspace 的文件，不自动重试副作用。
- 系统没有 Linux/bwrap 或不能建立沙箱时，只有 Bash 工具报错；不会回退为宿主 Bash，业务服务仍可启动。
- 本版不配置 GOPATH、GOCACHE、语言依赖下载、虚拟环境或持久缓存，也不提供 CPU、内存、磁盘配额；不是完整的多租户资源隔离方案。

命令启动后的结果字段为 `exit_code`、`stdout`、`stderr`、`timed_out`、`stdout_truncated`、`stderr_truncated`。普通非零退出码仍属于正常工具结果，交给模型判断下一步。超时使用 `BASH_TIMEOUT` 并保留已捕获输出；没有正常退出状态时 `exit_code=null`。

stdout、stderr 分别保留最多 32 KiB 原始字节（前后各 16 KiB），超限后仍持续读取和丢弃中间内容，避免死锁。展示时额外插入截断标记；UTF-8 无效字节用替代字符显示，不保证展示字符串编码后的字节数仍小于 32 KiB。不自动保存完整日志；确需保留时，命令应自行重定向到 workspace 文件。

## 启动命令

以下命令用于 Ubuntu 24.04 人工调试。请把 `WORKSPACE` 改成已经存在的项目绝对路径，用普通服务账号运行。真实实现位于 `src/laxcode/sandbox/bubblewrap.py`，通过参数数组启动，不经过宿主 shell 拼接。

```bash
WORKSPACE="/absolute/path/to/project/workspace"
COMMAND='pwd; python3 --version; go version'

runtime_mounts=(
  --ro-bind /usr /usr
  --ro-bind /bin /bin
  --ro-bind /sbin /sbin
  --ro-bind /lib /lib
)
if [[ -e /lib64 ]]; then
  runtime_mounts+=(--ro-bind /lib64 /lib64)
fi
dns_source="$(readlink -f /etc/resolv.conf)"

/usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/bwrap \
  --unshare-user --unshare-pid --unshare-ipc --unshare-uts \
  --share-net --disable-userns --cap-drop ALL \
  --die-with-parent --new-session \
  "${runtime_mounts[@]}" \
  --ro-bind /etc/ld.so.cache /etc/ld.so.cache \
  --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/hosts /etc/hosts \
  --ro-bind "$dns_source" /etc/resolv.conf \
  --ro-bind /etc/ssl/certs /etc/ssl/certs \
  --proc /proc --dev /dev --perms 1777 --tmpfs /tmp \
  --bind "$WORKSPACE" "$WORKSPACE" --chdir "$WORKSPACE" \
  --clearenv \
  --setenv PATH /usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  --setenv HOME /tmp --setenv TMPDIR /tmp --setenv LANG C.UTF-8 \
  --remount-ro / \
  -- /bin/bash --noprofile --norc -c "$COMMAND"
```

workspace 在沙箱内外保持同一绝对路径。为挂载点创建的空父目录不代表开放宿主 home。`/bin`、`/lib` 在 Ubuntu 上通常指向 `/usr`，只读挂载允许系统程序及动态链接器正常工作；ARM64 机器没有 `/lib64` 时不添加它。

| 参数 | 作用 |
| --- | --- |
| `--ro-bind SRC DEST` | 只读挂载系统程序、解释器、库和指定配置。 |
| `--bind workspace workspace` | 读写挂载项目目录，保留工作成果。 |
| `--perms 1777 --tmpfs /tmp` | 创建独立临时文件系统，权限与常规 `/tmp` 一致。 |
| `--dev /dev` | 创建最小设备目录，包括 null、随机设备等；不绑定宿主设备树。 |
| `--unshare-pid --proc /proc` | 建立独立进程编号空间和对应 procfs，不暴露宿主业务进程。 |
| `--unshare-user` | 普通用户创建沙箱，无需以 root 执行用户命令。 |
| `--unshare-ipc --unshare-uts` | 隔离 IPC 与主机名空间。 |
| `--share-net` | 保留宿主网络，包括宿主回环服务。 |
| `--disable-userns --cap-drop ALL` | 禁止后续创建用户命名空间并移除 Linux capabilities。 |
| `--die-with-parent` | bwrap 或调用它的进程退出时结束沙箱进程。 |
| `--new-session` | 分离终端会话，不继承可交互终端。 |
| `env -i`、`--clearenv`、`--setenv` | 启动 bwrap 前和沙箱内均使用最小环境，避免继承凭据或 BASH_ENV 等钩子。 |
| `--remount-ro /` | 将空根挂载改为只读，不递归更改 workspace、tmpfs 等子挂载。 |
| `--noprofile --norc -c` | 一次性非交互 Bash，不读取 shell 初始化文件。 |

正式执行额外通过独立管道传入 `--json-status-fd FD`，解析 bwrap 的多份 JSON 状态，区分设置失败和用户命令退出。该状态不从 stdout/stderr 猜测，也不向命令开放服务文件描述符。人工命令没有 Python 层的限时和输出收集；应用工具具有这些能力。

不会挂载宿主根目录、整个 `/etc`、`/run`、用户 home 或服务目录。DNS 绑定 `resolv.conf` 指向的实际文件，避免因为 `/run` 不可见而断开解析。TLS 使用只读系统证书。安装在 `/usr`、`/usr/local` 的工具可用，home 中的 uv、服务 `.venv` 或未挂载的 `/opt` 工具不会自动提供。

## 安装依赖与 AppArmor

支持并验收 Ubuntu 24.04、Linux 6.8，使用发行版安全更新的 bubblewrap（至少具备上述 0.9 参数）。无需编译 bwrap，也不需要 Docker 或新守护进程。

一次性准备：

```bash
sudo bash scripts/prepare_bash_sandbox.sh
# 下条命令用普通服务账号执行，不能用 sudo：
python3 src/laxcode/sandbox/bubblewrap.py --probe
```

准备脚本安装 `bubblewrap apparmor bash coreutils procps util-linux ca-certificates python3 python3-venv golang-go build-essential git curl`。Ubuntu bubblewrap 的 `libc6`、`libcap2`、`libselinux1` 依赖由 APT 自动安装。Python、Go、GCC 等是提供给沙箱的系统工具，不是项目新增的 Python 依赖。

内核必须支持并允许非特权 user namespace 以及 mount/PID/IPC/UTS namespace，`user.max_user_namespaces` 应大于零。服务使用普通账号，只在安装和配置时使用管理员权限。

Ubuntu 24.04 通常开启 `kernel.apparmor_restrict_unprivileged_userns=1`。准备脚本保留已有 bwrap 相关策略；无相关策略时安装 `/etc/apparmor.d/laxcode-bwrap`：

```text
abi <abi/4.0>,
include <tunables/global>

profile laxcode-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
```

然后运行 `apparmor_parser -r /etc/apparmor.d/laxcode-bwrap`。这仅允许 bwrap 建立 namespace；固定挂载、capabilities 移除与禁止嵌套 user namespace 由运行参数实施。不会全局关闭 AppArmor，不改变 bwrap 的 setuid 位。

脚本保守检测已有 bwrap 相关文件并保留，拒绝覆盖内容不同的同名规则。已有规则是否满足要求，由普通账号探测决定；失败时由管理员修复原有规则，不安装第二条冲突绑定。规则编译缓存可能出现在缓存目录，但不作为项目配置分发。

服务源码、真实配置及秘密不应位于公开给沙箱的 `/usr` 树内。workspace 是服务可信计算的路径，不允许用 `/` 或系统挂载目录冒充 workspace。

## 项目构建和部署变化

| 项目 | 变化 |
| --- | --- |
| Python 构建、uv sync | 无新增第三方依赖、无原生扩展构建。 |
| 前端 | 仍使用 npm ci 与 npm run build。 |
| 数据库和 HTTP | 无迁移、无新接口或 listener。 |
| Lima YAML | 新 VM 初始化时安装 bwrap/Go 等依赖，并配置专用 AppArmor 规则。 |
| 已有 Lima | 必须运行一次性准备脚本，不能只修改 YAML。 |
| deploy_lima.sh | 在任何源码复制和前端构建之前，以 SSH 服务账号执行当前 checkout 的真实沙箱探测；失败立即停止，无自动提权和系统安装。 |
| 服务启动 | 不强制依赖 bwrap；不可用时 Bash 工具报错，其余功能可用。 |

探测源码只有标准库依赖，通过 stdin 传入目标机器，不依赖远端是否已有新代码。探测使用相同 `build_argv`、进程执行和清理路径，在临时 workspace 中执行 `pwd && python3 --version && go version`。

现有 Lima 实例的命令：

```bash
limactl shell laxcode -- sudo bash -s < scripts/prepare_bash_sandbox.sh
limactl shell laxcode -- python3 - --probe < src/laxcode/sandbox/bubblewrap.py
scripts/deploy_lima.sh
```

部署不自动重启正在运行的服务；新进程启动后才使用新工具。系统服务的额外 namespace/AppArmor 限制也应以实际服务账号环境探测。

## 执行、取消与测试

executor 对异步 Bash 直接 await，对原有文件工具继续使用线程。子进程 stdin 关闭，stdout/stderr 并发排空；不产生实时命令输出 SSE，只使用原有 tool_start/tool_end。

超时和取消先终止 bwrap 进程组，一秒内未退出则强制杀死。独立 PID namespace 与 die-with-parent 同时收掉脱离进程组的后代。取消发生在创建进程期间时，仍先取得句柄再清理；重复取消不能打断收尾。服务关闭等待任务清理结束。

```bash
# 本机普通回归；明确排除 Linux 验收，不把它当成沙箱验收结果。
uv run pytest

# Linux/Lima 真正的沙箱验收，缺依赖会失败，不做 skip：
uv run pytest -m sandbox_linux tests/test_bash_linux.py tests/test_tool_loop.py

# Lima 中连接 macOS 的独立测试库（数据库名和测试凭据固定，不读取真实配置）：
LAXCODE_TEST_DB_HOST=host.lima.internal uv run pytest -m sandbox_linux tests/test_bash_linux.py tests/test_tool_loop.py
```

Linux 单独的 `test_bash_linux.py` 不需要 PostgreSQL、NATS 或真实 LLM；`test_tool_loop.py` 的集成验收使用独立 `laxcode_test` 数据库和模拟模型/事件。HTTPS 验收访问 example.com，需要外网可达。

覆盖文件持久化、临时文件销毁、系统目录只读、home 与其他工作区不可见、进程隔离、环境清理、DNS/HTTPS、非零退出、海量输出、后台进程、超时、重复取消、创建进程期间取消、调用进程崩溃，以及缺 bwrap/挂载失败时拒绝执行。

排障顺序：确认 `bwrap --version`，用服务账号运行 probe，再查看 `sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns user.max_user_namespaces`。Ubuntu 上通过 `sudo journalctl -k` 查 AppArmor DENIED，并检查已有规则。工具结果隐藏 bwrap 设置阶段的宿主路径；需要完整错误时，用本文人工命令在部署终端复现。不要通过关闭全局安全机制或宿主执行来绕过失败。

参考：[bubblewrap 官方说明](https://github.com/containers/bubblewrap)、[0.9 手册](https://raw.githubusercontent.com/containers/bubblewrap/v0.9.0/bwrap.xml)、[AppArmor 4 bwrap 规则](https://gitlab.com/apparmor/apparmor/-/blob/apparmor-4.0/profiles/apparmor/profiles/extras/bwrap-userns-restrict)。
