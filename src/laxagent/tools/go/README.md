# 沙箱文件工具

当前阶段已迁移 `read_file`、`write_file`、`edit_file`，其他工具、系统提示词和部署流程尚未切换。

- Go 1.22 或更新版本；仅使用标准库。
- 从仓库根目录执行 `bash scripts/build_read_file.sh`，生成当前 Go 架构的 Linux 二进制到 `src/laxagent/tools/bin/read_file`。可通过 `GOARCH=amd64` 或 `GOARCH=arm64` 指定部署架构，产物不入库。
- `bash scripts/build_write_file.sh` 单独构建 `src/laxagent/tools/bin/write_file`，架构选择方式相同。
- `bash scripts/build_edit_file.sh` 单独构建 `src/laxagent/tools/bin/edit_file`，架构选择方式相同。
- 三个工具共用 Python 的 `sandbox/file_tools.py`，通过 bwrap 直接启动对应二进制，stdin 接收单个工具参数 JSON，stdout 输出 `{error_code, result}`。实际文件操作在沙箱内进行，不提供宿主执行回退。
- `path` 使用沙箱绝对路径；仅挂载用户的全部项目和 skills 到 `/user/project`、`/user/skills`，工作目录固定为 `/`，不依赖当前项目 workspace。附件、session 和诊断目录不挂载。
- 文件工具只操作普通 UTF-8 文件，不跟随符号链接。读取分页保留原始字节游标、2000 行和 50 KiB 上限。
- 写入创建缺失父目录，通过同目录临时文件原子替换并保留原文件权限；`delete` 幂等删除普通文件，包括技能入口，不删除目录。技能元数据不在写入时校验。
- 编辑仅面向已有文件，不创建父目录；精确匹配优先，整行回退匹配只忽略行首尾空格、Tab 和 LF/CRLF 差异。整批基于原文匹配，歧义、重叠或缺失时不写入；无变化编辑不替换文件。技能元数据不在编辑时校验。
- Go 写入与编辑共用 `internal/fileops` 的目录描述符、普通文件检查和原子替换；编辑要求目标存在，写入允许创建目标。
- 执行器不接收 workspace；写入和编辑成功后 Python 侧只将当前项目内的文件登记为预览候选，包括无变化编辑，技能、其他项目和删除操作不登记。

相关单元测试：在本目录运行 `go test ./internal/readfile ./internal/writefile ./internal/editfile ./internal/fileops`；在仓库根目录运行 `uv run pytest tests/test_read_file.py tests/test_write_file.py tests/test_edit_file.py`。Go 测试覆盖分页、匹配规则和文件操作，Linux 环境额外验证目录描述符和父目录创建；Python 测试隔离进程调用，验证工具适配、挂载参数和结果协议。真实 bwrap 与全仓测试留到整体迁移完成后执行。
