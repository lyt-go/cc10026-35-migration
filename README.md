# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 调用审计的按月分片存储

审计明细已从「每条一个 JSON」迁移到按月分片；迁移期间两套来源并存，
查询和归档都以**旧目录 ∪ 月分片**为数据源。

```text
data/audit/
  000123.json                 迁移前存量逐条明细（只读兼容）
  _archive_2026-11-01.json    归档产物（保持原路径）
  monthly/
    manifest.json             每月迁移状态（active / partial / complete）
    2026-10/000123.json       月分片明细
    2026-11/000456.json
  archive/
    2026-11-01/2026-10.json   按原日期切出的历史明细，按月分文件
    2026-11-01/index.json
```

约定：

- 月份边界统一按东八区（`+08:00`）。记录里的原始时间戳连同原始偏移原样保留，
  只在判断归属月份时换算到东八区；因此 `2026-10-31T16:20:00+00:00`
  归属 `2026-11`。
- 同一笔调用可能同时出现在旧目录和月分片。查询按「原始编号 / 原始 `audit_id`」
  归并，只算一笔，并在 `_source` 标注 `legacy`、`shard` 或 `legacy+shard`。
  没有编号的历史事件只在两套来源之间按内容指纹一对一配对，同源重复调用仍分开计。
- 迁移没跑完（月清单为 `partial`）不会让旧目录里已存在的历史行消失：
  查询始终合并两套来源，分片只负责去重和补全字段。
- 归档按原日期（东八区自然日零点）切历史明细，结果保留原编号、
  原时间戳、归属月份和**新数据实际写入的月份**（`_written_month`）。
  归档状态会同时写回两套来源，避免分片优先读到旧状态。
- 每次查询/归档都返回对账摘要：两套来源行数、去重量、双来源笔数、
  按月归属、迁移中的月份、旧目录待迁行数，以及 `balanced` / `complete` 结论。

相关接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/audit/records` | 区间查询（`start_time`/`end_time`/`api_name`/`caller`/`end_exclusive`） |
| GET | `/api/audit/reconcile` | 只返回对账摘要 |
| GET | `/api/audit/shards` | 月分片清单与迁移状态 |
| GET | `/api/audit/archive/preview` | 归档预览（`before_date`） |
| POST | `/api/audit/archive` | 执行归档（`before_date`） |
| POST | `/api/audit/migrate` | 存量迁移（`months`/`per_month_limit`/`remove_legacy`，可重复执行） |
| POST | `/api/audit/samples` | 写入并核对三个边界样本 |

前端在「调用审计」页提供区间查看、归档预览/执行、分片清单和边界样本核对；
列表与导出会带出原始编号、归属月份、写入月份和数据来源。

三个固定边界样本（`POST /api/audit/samples` 幂等写入）：

| 原始编号 | 场景 | 原始时间戳 | 归属 | 来源 | 11-01 前归档 |
| --- | --- | --- | --- | --- | --- |
| CALL-SAMPLE-0001 | 迁移前月末 | `2026-10-31T23:30:00+08:00` | 2026-10 | 旧目录 | 是 |
| CALL-SAMPLE-0002 | 刚跨月 | `2026-10-31T16:20:00+00:00` | 2026-11 | 旧目录+分片（去重为一笔） | 否 |
| CALL-SAMPLE-0003 | 切换过程中新增 | `2026-11-01T09:05:00+08:00` | 2026-11 | 月分片 | 否 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
