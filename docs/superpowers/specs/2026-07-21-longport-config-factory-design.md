# Longbridge 4.x Config 构造修复设计

## 背景与根因

当前环境使用 `longport 4.3.3`。该版本禁止直接实例化 `Config`，旧式
`Config(app_key=..., app_secret=..., access_token=...)` 会抛出
`TypeError: cannot create 'builtins.Config' instances`。4.x 的公开 API 是
`Config.from_apikey(app_key, app_secret, access_token)`。

仓库在凭证验证、持仓查询、交易上下文和实时行情订阅中共有四类旧式构造点，
因此只修复保存页面触发的验证路径会留下同类运行时错误。

## 方案选择

采用直接迁移到 Longbridge 4.x 工厂 API：将所有旧式 `Config(...)` 调用替换为
`Config.from_apikey(...)`。不保留旧 SDK 兼容分支，也不通过降级或固定旧版 SDK
规避问题。

备选方案未采用：

- 增加新适配器模块：可集中兼容多个 SDK 版本，但当前已确认只支持 4.x，额外抽象没有收益。
- 降级 Longbridge SDK：会依赖旧行为，并继续掩盖项目代码与当前 API 不一致的问题。

## 修改范围

- `backend/app/services.py`：行情验证上下文与持仓配置构造。
- `backend/app/trading_api.py`：交易上下文配置构造。
- `backend/app/streaming.py`：实时行情订阅配置构造。

凭证存储格式、DuckDB 数据、接口请求/响应和前端交互保持不变。

## 错误处理与安全

工厂调用继续使用已有的三个凭证字段；不记录或回显凭证内容。SDK 创建或连接失败
仍由各调用链现有异常处理负责，不改变用户可见错误分类。

## 测试与验收

1. 先增加一个针对 Longbridge 4.x 的失败测试，证明旧式构造路径会触发当前异常。
2. 修改全部构造点后，测试应证明调用的是 `Config.from_apikey`，且参数顺序正确。
3. 搜索后端源码，确认不存在遗留的 `Config(` 旧式调用。
4. 重启后端，验证 `/health`，再调用凭证验证链路；实际凭证只用于本地验证，不输出到日志或回复。

