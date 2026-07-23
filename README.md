# Longbridge Quant Console

Longbridge Quant Console 是一个在本机运行的量化交易控制台。后端使用 FastAPI、DuckDB 和 Longbridge OpenAPI，前端使用 React、TypeScript、Vite 与 MUI。

> 本项目能够提交真实订单。AI 交易和自动仓位默认使用模拟模式；启用实盘前，请先确认账户、权限、标的、数量和风控参数。

## 当前功能

| 菜单 | 功能 |
| --- | --- |
| AI 交易 | AI 分析、策略参数、模拟或实盘执行、运行日志 |
| 智能仓位 | 仓位计算、持仓选择、自动仓位管理 |
| 智能选股 | 官方证券列表搜索、多空股票池、量化评分和 AI 分析 |
| 板块轮动 | 板块热力图、因子分析和持仓关联 |
| 策略盯盘 | 策略信号、实时 K 线和运行状态 |
| 持仓监控 | 持仓盈亏、止盈止损与监控设置 |
| 持仓 K 线 | 按持仓标的和周期查看历史 K 线 |
| 基础配置 | Longbridge/AI 凭据、股票列表和历史数据同步 |

## 环境要求

- Python 3.9+
- Node.js 20+
- npm
- macOS 或 Linux 可直接使用 Shell 启停脚本；Windows 可使用对应的 `.bat` 文件

## 快速启动

在仓库根目录运行：

```bash
./start.sh
```

脚本会创建 `backend/.venv`、安装缺失依赖，并启动：

- 前端：http://localhost:5173
- 后端：http://localhost:8000
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

停止服务：

```bash
./stop.sh
```

首次启动后，在“基础配置”页面保存并验证 Longbridge 凭据；AI 相关功能还需要配置相应 API Key。敏感配置会加密存入本地 DuckDB，不要提交数据库、环境文件或加密密钥。

## 手动启动

安装并启动后端：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e backend
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

在另一个终端安装并启动前端：

```bash
cd frontend
npm ci
VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

## 配置

应用凭据通过“基础配置”页面管理并加密保存。后端还支持在 `backend/.env` 中设置本地运行参数：

```dotenv
DATA_DIR=data
DUCKDB_PATH=data/quant.db
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
# ENCRYPTION_KEY=<Fernet key；不设置时首次运行自动生成>
```

`backend/longbridge.env.example` 提供 Longbridge 官方服务端点示例。需要覆盖 SDK 默认端点时，将其复制为 `backend/.longbridge.env` 后再启动；已有的 `.longport.env` 仍可兼容加载。前端 API 地址由 `VITE_API_BASE` 控制，默认是 `http://localhost:8000`。

## 实盘保护

- AI 交易和自动仓位配置中的 `enable_real_trading` 默认为 `false`。
- 从模拟模式切换为实盘模式时，后端要求显式确认值 `CONFIRM_REAL_TRADING`。
- `start.sh` 默认只监听 `127.0.0.1`，后端同时校验浏览器 Origin。
- 启用实盘不代表订单一定成功；券商权限、余额、交易时段和上游风控仍会生效。

## 验证

前端类型检查和生产构建：

```bash
cd frontend
npm run check
```

后端测试：

```bash
backend/.venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py' -v
```

CI 使用的完整命令是：

```bash
python -m unittest discover -s backend/tests -v
cd frontend && npm ci && npm run check
```

## 目录结构

```text
backend/app/             FastAPI 路由、服务、策略和数据访问
backend/tests/           后端单元与契约测试
frontend/src/pages/      当前 8 个菜单页面
frontend/src/api/        前端 HTTP API 客户端
frontend/src/components/ 共享 UI 与图表组件
config/strategies.json   策略模板
start.sh / stop.sh       本地服务启停脚本
```

运行时数据位于 `backend/data/`，日志位于 `logs/`；这些内容均不应进入版本控制。
