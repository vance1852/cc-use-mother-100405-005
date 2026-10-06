# 治理科研成果中试转化协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

`src/science_strategy_foundation/pilot/` 子包在基础边界上实现了一套完整的**中试转化治理**：把知识产权权属、许可范围、工艺版本、原料谱系、设备能力、试生产批次、质量指标、风险责任与阶段付款关联起来，支撑高校涂层工艺从实验室进入连续产线的放大协作。

## 中试转化治理规则

- **协议与授权**：协议绑定技术方组织与生产方组织，登记知识产权权属与许可范围（领域、地域、独占性、产能上限）。
- **放大前冻结**：工艺版本（配方哈希、放大参数与偏差、允许物料规格、设备能力）只能在前置证据齐备时冻结；进入 `pilot`/`continuous` 前必须已有上一阶段冻结版本。
- **职责分离的签收门**：进入中试/连续阶段必须开启签收门并绑定一套已冻结工艺；技术方与生产方各自确认，双方齐备后门才通过；同阶段同时最多一个未决门（并发签收只一个版本生效），门通过后才能放行该阶段批次。
- **原料谱系**：登记供应商与批次；替代供应商/原料替换时旧料标记停产、保留历史，并返回波及的全部在制批次；被替代原料不能再进入新批次。
- **检测回调**：按 `callback_key` 与请求回执双重幂等，相同回调绝不二次推进批次状态；全部达标/部分达标/失败三态。
- **偏差与责任**：按类别确定性定责（工艺设计→技术方，执行/来料→生产方），由责任相对方确认结清，可派生返工或补偿义务；已结清结论不可改写。
- **召回**：以新记录召回已放行批次，保留批次已交付状态并生成返还义务。
- **只追加事实**：付款流水、批次、回调、偏差、义务均不可改写或删除；结清义务一律新增结算引用。
- **协议终止**：未支付阶段款随终止核减，返工/返还/补偿/退款义务继续有效，已交付批次与已支付付款保持不变；终止后只能履行遗留义务，不能产生新交付。
- **查询 API**：`product-lineage`（产品依据哪套工艺、哪次双方授权、哪些原料批次生产）、`dispositions`（偏差由谁处置）、`financial-position`（尚有哪些付款/返还义务与付款流水）、`change-impact`（原料或工艺变更波及的全部在制批次，含被取代版本谱系）。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/science_strategy_foundation/pilot/`：中试转化治理的表结构、领域常量、治理服务与离线验收；
- `tests/`：基础规则、事务边界、接口路由、中试治理规则和端到端验收测试。

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
PYTHONPATH=src python3 -m science_strategy_foundation.pilot.acceptance
```

验收命令在临时 SQLite 数据库中走完整业务链。基础验收核对幂等回执与审计链；中试验收覆盖：实验室冻结 → 证据齐备 → 双方职责分离签收 → 原料停产替代 → 在制批次波及 → 部分达标 → 偏差定责技术方 → 阶段付款 → 召回保留事实 → 并发签收只一个版本生效 → 协议终止核减未付款、保留补偿义务并继续履行 → 产品血缘/偏差处置/财务头寸/变更波及查询。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

### 中试治理接口

写入均为 `POST`（操作者取自 `X-Actor-Id`，请求体带 `request_id` 实现幂等），查询为 `GET`：

- `POST /pilot/agreements`、`/pilot/ip-terms`、`/pilot/agreements/terminate`
- `POST /pilot/process-versions`、`/pilot/evidence`、`/pilot/process-versions/freeze`
- `POST /pilot/scale-gates`、`/pilot/scale-gates/confirm`
- `POST /pilot/materials`、`/pilot/materials/substitute`
- `POST /pilot/batches`、`/pilot/batches/start`、`/pilot/batches/recall`、`/pilot/quality-callbacks`
- `POST /pilot/deviations`、`/pilot/deviations/resolve`
- `POST /pilot/payments/schedule`、`/pilot/payments`、`/pilot/obligations/settle`
- `GET /pilot/agreements/{id}/product-lineage?batch_id=…`
- `GET /pilot/agreements/{id}/dispositions?batch_id=…`
- `GET /pilot/agreements/{id}/financial-position`
- `GET /pilot/agreements/{id}/change-impact?material_id=…&version_id=…`
