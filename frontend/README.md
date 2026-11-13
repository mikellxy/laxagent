# LaxCode Web

React + TypeScript 前端。生产环境由现有 FastAPI API 端口提供静态文件和 `/api/v1`，不增加 listener 或 Node 服务。

## 开发与构建

使用 Node.js 22.12+（或较新的受支持版本）和 npm，在本目录运行：

```sh
npm ci
npm run dev
npm test
npm run build
```

开发服务器将 `/api` 代理到 `../config/config.json` 中的 API 端口；可用 `LAXCODE_CONFIG` 指定配置文件路径，未找到文件时使用 8000。生产构建输出 `dist/`，不会包含服务端配置或凭据。

在后端配置顶层启用：

```json
"frontend": { "dist_dir": "../frontend/dist" }
```

路径相对于该配置文件所在目录。省略 frontend 则保持纯后端运行；配置了 frontend 却未构建时启动失败。

从仓库根目录启动：

```sh
uv run python -m laxagent.main --config config/config.json
```

浏览器访问 API 端口的 `/`。`main.py` 保持原样，只启动 Router 和 API。

## 浏览器测试

```sh
npx playwright install chromium
npm run test:browser
```

测试使用独立浏览器上下文和模拟 API，不操作真实用户数据。macOS 已安装 Microsoft Edge 时自动使用它，也可通过 `PLAYWRIGHT_CHANNEL` 指定浏览器 channel。

## Lima 验收

从仓库根目录运行 `bash scripts/deploy_lima.sh`。脚本在宿主机安装锁定依赖并构建，将源码与 dist 同步到 Lima，排除 node_modules；虚拟机只需 Python 依赖。

在 Lima 的 `~/code/laxcode` 启动服务，通过现有 API 端口转发访问页面。真实聊天冒烟继续在虚拟机内运行：

```sh
uv run python scripts/smoke_chat.py --config config/config.json
```

用户目录和测试记录保留供检查。真实 provider 配置只保存在 Git 忽略的服务端配置中。

服务启动后，也可在宿主机的 `frontend/` 运行 `node tests/smoke-lima.mjs`，以独立浏览器验证游客初始化、项目与会话操作、真实聊天与工具读取、运行中刷新、分页和滚动。该脚本会调用真实模型，并在 Lima 中保留测试数据；截图写入 `test-results/`。可用 `VM_NAME` 和 `LAXCODE_BASE_URL` 覆盖默认虚拟机与访问地址。验收结束后停止本次启动的服务。
