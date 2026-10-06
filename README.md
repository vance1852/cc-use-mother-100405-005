# 治理科研成果中试转化协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

## 中试转化治理领域（`pilot_governance`）

`src/pilot_governance/` 在基础服务之上实现涂层（或类似工艺）从实验室到连续产线的完整中试转化治理：

- **权属与授权**：项目绑定分属不同组织的技术方、生产方、成果转化中心；技术转让协议登记知识产权权属、许可范围、失败批次责任条款与阶段付款金额。
- **工艺版本与冻结**：工艺版本含配方步骤、放大参数偏差窗口和质量指标；放大前必须冻结，冻结后只能新建后继版本，且阶段必须逐级递进。
- **原料谱系与供应商替换**：登记供应商与原料批次、停产事实；供应商替换由成果转化中心裁决并只追加新记录，仅对生效时点之后开批的批次生效，历史批次绑定不变。
- **设备能力与阶段闸门**：每级放大前校验设备能力与物料有效性，冻结工艺与前置证据；闸门必须由技术方、生产方两个不同组织分别签收才生效（部分唯一索引保证每阶段最多一个生效版本）。
- **试生产批次与检测回调**：批次依据生效闸门与工艺开批并快照物料绑定；检测回调按 `callback_id` 幂等，相同回调不重复推进状态，按质量窗口判定通过 / 部分达标 / 不达标并自动开偏差单。
- **偏差与责任**：偏差由成果转化中心裁决技术方 / 生产方承担或分担，技术方份额生成返还义务；裁决结论与检测事实不可改写。
- **交付、召回、终止**：交付仅允许全达标批次且事实不可改写；召回插入新记录并登记返还义务；协议终止以新结算记录轧差全部未完成义务，已支付与已交付事实保持不变。
- **治理查询 API**：`/pilot/products/provenance`（依据哪套工艺与授权生产）、`/pilot/deviations`（偏差由谁处置）、`/pilot/obligations`（未了付款与返还）、`/pilot/change-impact`（变更波及的全部在制批次）。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/pilot_governance/`：中试转化治理领域的模型、状态规则、服务、HTTP 路由和端到端离线验收；
- `tests/`：基础规则、事务边界、接口路由、并发签收和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
PYTHONPATH=src python3 -m pilot_governance.acceptance
```

验收命令会在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。中试治理验收完整演练原料停产、供应商替换、双签放大、部分达标归责、回调幂等、召回和终止轧差剧情。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m pilot_governance.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8090
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。中试治理服务同时挂载基础建档路由（`/organizations`、`/actors`、`/sites`），可独立部署。
