# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/offline-records`：批量并入离线检验/维保记录，请求体为`{"records":[...]}`。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 离线并入规则

地下机房巡检员在离线端记录检验和维保结果，网络恢复后通过`POST /api/offline-records`逐条并入：

- 每条记录必须带`source_id`、`record_id`、`kind`（`inspection`/`maintenance`）、`target_id`、`action`、`base_version`和`payload`；同一`(source_id, record_id)`全局只处理一次，重复提交返回首次处理结果，不重复执行。
- 中心版本晚于`base_version`时判定为`stale`：保留中心内容、不覆盖他人改动，并在记录的`diffs`中逐项列出状态和字段差异（中心值/离线值）。并入写入瞬间发生乐观锁冲突也按`stale`处理。
- 设备存在未关闭报警（未关闭且未判误）时，检验或维保结果记为`held`，不改变目标记录；报警关闭或判误后自动重算并应用（审计动作为`retry_offline`）。重算时中心又被改过的，转为`stale`。
- 处理结果状态：`applied`（已生效）、`stale`（版本旧、保留中心并列差异）、`held`（等报警关闭）、`rejected`（目标不存在或未通过规则校验）。
- 在线接口同样受未关闭报警限制：报警未关闭时检验`pass/fail`和维保`complete`会被规则引擎拒绝。

## 恢复许可重算

设备状态一旦变化，恢复许可立即重算：新困人报警、设备停用/脱出可用状态、新增整改、检验结果变化等都会重新评估已`granted`的许可。条件不再满足时，许可退回`pending_review`，并在记录中写明原因、触发来源和阻断项（`review_reason`/`review_trigger`/`review_history`），必须人工重新`grant`，报警关闭不会自动恢复许可。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制；未关闭报警同时阻断结果生效和许可授予。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
