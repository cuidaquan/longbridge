# Longbridge Quant Console

Longbridge Quant Console 是一个在本机运行的量化交易控制台。后端使用 FastAPI、DuckDB 和 Longbridge OpenAPI，前端使用 React、TypeScript、Vite 与 MUI。

> 本项目能够提交真实订单。AI 交易和自动仓位默认使用模拟模式；启用实盘前，请先确认账户、权限、标的、数量和风控参数。

## 当前功能

| 菜单 | 功能 |
| --- | --- |
| AI 交易 | AI 分析、策略参数、模拟或实盘执行、运行日志 |
| 智能仓位 | 仓位计算、持仓选择、自动仓位管理 |
| 智能选股 | 官方 Screener、多空候选过滤、量化评分和 AI 分析 |
| 板块轮动 | 板块热力图、因子分析和持仓关联 |
| 策略盯盘 | 策略信号、实时 K 线和运行状态 |
| 持仓监控 | 持仓盈亏、止盈止损与监控设置 |
| 持仓 K 线 | 按持仓标的和周期查看历史 K 线 |
| 基础配置 | Longbridge/AI 凭据、股票列表和历史数据同步 |

美股做空候选可按需查询当前 Longbridge 账户的点时预估可卖空数量，
也可设置最低数量进行硬过滤。该估算是账户风险控制结果，不是实时券源清单，
不提供融券费率或召回风险。查询失败会按异常元数据归类为配置、SDK、鉴权、
限流、超时、网络、本地繁忙/熔断、上游拒绝、无数据或未知技术错误，并汇总到
候选结果和点时覆盖；这些分类不会被解释为账户交易权限、实时券源或风控结论。
启用默认关闭的收盘后因子快照后，US SHORT 还会保存该数量及其缺失状态，
用于后续覆盖审计；旧快照不会被事后补值。

Screener 候选过滤默认只处理当前页。需要扩大候选范围时，可以显式开启
跨页扫描，一次连续处理 2、3 或 5 页，并汇总过滤原因、来源页和下一页位置。
单次最多处理 100 个上游候选；结果保持 Longbridge 原始顺序并按股票代码去重。
跨页扫描不会自动启用财务、保证金或账户容量等重型补充，也不代表已经遍历全市场。
跨页行业 RS 在去重后的本次扫描范围内统一计算，避免分页边界产生不同中位数；
它仍是策略候选样本的近似值，不是完整行业指数或全市场行业基准。

Screener 搜索可显式开启点时扫描快照，默认关闭。开启后，本地 DuckDB 会保存
策略与过滤配置、实际扫描页、候选出现顺序和来源页、跨页去重关系、过滤前候选及
当时的行业/RS 口径、逐股排除原因和最终入选顺序；页面可查询最近快照及详情。
覆盖接口会按市场本地时区将同日重复采集归并，并按市场、方向、策略和完整扫描
配置哈希隔离 cohort；达到 20 个采集日、28 天跨度、30 只候选、10 只入选、
200 条候选观测、60 条入选观测且快照完整性为 100% 后，才标记该 cohort 具备
后续评估的输入覆盖。覆盖达标不等于交易日与后验标签就绪或收益验证通过；当前
快照也尚未保存精确基准原始收益，不能进行市场环境分层。
该能力只从启用后的搜索开始积累未来样本，不会补造历史时点股票宇宙，也不代表
任何因子已经取得样本外增量收益。

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
DEPLOYMENT_MODE=single_instance
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
# ENCRYPTION_KEY=<Fernet key；不设置时首次运行自动生成>
```

`backend/longbridge.env.example` 提供 Longbridge 官方服务端点示例。需要覆盖 SDK 默认端点时，将其复制为 `backend/.longbridge.env` 后再启动；已有的 `.longport.env` 仍可兼容加载。前端 API 地址由 `VITE_API_BASE` 控制，默认是 `http://localhost:8000`。

## 部署模式

当前只支持本地单实例部署：同一 DuckDB 文件同时只能由一个后端进程使用。后端启动时会按 DuckDB 绝对路径获取系统级独占锁；如果已有后端使用同一数据库，第二个进程会在启动阶段明确失败。进程正常退出或异常终止后，操作系统会自动释放锁。

- 不要使用 Uvicorn 的多 worker 模式，也不要让多个后端进程共享同一 `DUCKDB_PATH`。
- 不同 DuckDB 路径可以分别启动独立实例。
- `GET /health` 返回 `deployment_mode`、`instance_lock_acquired`、不直接暴露路径的 SHA-256 `database_id` 指纹，以及每次进程启动都会变化的 `runtime_id`、启动时间和瞬时状态重置清单。
- 分析任务、缓存、熔断和限流状态均为进程内状态；这与当前单实例部署一致，服务重启后不会恢复这些瞬时状态。缓存和外部服务保护会以冷状态重新建立。
- 智能选股页面会在当前浏览器会话中记录进行中的分析任务。页面刷新或 SSE 断开后会查询任务快照并重连；若后端暂时不可达则持续等待，若 `runtime_id` 已变化则明确终止旧任务、重新加载持久化结果并提示重新分析。

只有确定需要多实例部署时，才需要迁移到支持并发写入的共享数据库、任务队列和共享缓存，并重新设计分布式协调。

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
