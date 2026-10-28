# Ontology workbench：已实现与启动方式

2026-10-25。此文及代码取代旧设计稿的独立 HTML 展示与下载方案。

## 已实现

- DocSensaAI 门户 `/ontology/`：数据库选择、真实 SQLite schema/主外键/数据分页预览。
- JSONL 上传、行号错误与重复 ID 校验、显式导入、分页 pairs 展示、两条示例快捷载入。
- `POST /ontology/api/sql-train` 默认启动真实 cortex CLI，首批处理前两条 case，展示真实阶段、case 计数、当前步骤和持久历史。
- 完整前端内的 Graph explorer；复用现有 `graph_html.py` 的 Canvas 算法和交互，主题配色适配，未嵌入 iframe 或跳转 HTML 文件。
- financial / thrombosis_prediction 归档结果的当前 schema 兼容副本，完整保留冲突，转换审计可展开。
- 当前视图 PNG / SVG 图片导出；全图 GraphState JSON 保存、重新导入和展示。PNG/SVG 是图片，不能还原交互式 ontology；导入使用 Graph JSON。

开始构建调用真实 `cortex sql-train` 子进程，使用 `.env` 中的模型及 embedding 配置。每次任务复制所选数据库为只读快照，独立保存 config、pairs、进度 JSONL、日志、CLI 结果与生成图谱。生成结果只加载该 run 的 `graph_state.json`，不会用归档替代失败结果。归档入口仍用于单独查看已有产物。

任务元数据与 Graph Merge 均使用 SQLite，后者使用官方 `SqliteSaver`；无需 PostgreSQL。前两条 case 无足够历史时不编造 ETA，训练 case 进度与 merge 阶段分别展示。服务关闭会终止自有子进程并将任务标记 interrupted；当前是单 worker 本地运行方式。

## 代码分布

后端位于当前 ontology 仓库：`src/cortex/web/app.py`、`training.py`、`graphs.py`。前端位于 `/path/to/user/CodeBase/DataMosaic/web/src/features/ontology/`，与门户导航、构建和主题直接集成。

后端数据库根目录默认 `data/web_databases`，运行数据默认 `workspace/web`，都被 gitignore 排除；预置图谱和示例 pairs 位于 `examples/ontology` 并可随源码分发。原 SQLite 和原 `results_final` 文件不修改。

## 本地运行

在 ontology 仓库先安装 web 依赖：

```bash
.venv/bin/python -m pip install -e '.[web]'
.venv/bin/python scripts/prepare_ontology_demo.py \
  --database-root /path/to/user/CodeBase/Text2SQL/datasets/bird/dev/dev_databases
scripts/start_ontology_web.sh /path/to/user/CodeBase/DataMosaic
```

打开 `http://127.0.0.1:8011/ontology/`。脚本构建真实门户后由 FastAPI 提供页面与 API，适合当前 独立调试本模块。停止终端服务即可结束预览。

该本地预览只运行 ontology 模块；已有 Analyze/Extract 链接仍存在，但不会代替未启动的其他引擎。完整系统仍通过原 `start.sh` 或 Compose 运行。

前后端热更新可分别启动：

```bash
.venv/bin/python -m uvicorn cortex.web.app:app --host 127.0.0.1 --port 8011 --reload
# 另一个终端，集成仓库中：
npm --prefix web run dev
```

Vite 已将 `/ontology/api/` 代理到 8011。Apple Silicon 上若默认 Node 为 x64，请在当前终端使用 `PATH=/opt/homebrew/bin:$PATH` 后运行 npm，避免 esbuild 架构不匹配。

环境变量：`CORTEX_WEB_DATABASE_ROOT`、`CORTEX_WEB_STORAGE`、`CORTEX_WEB_EXAMPLES`、`CORTEX_WEB_FRONTEND_DIST` 可覆盖默认路径。Docker 显式设置示例目录，不依赖 pip 安装后的源码位置。当前仅支持单个 API worker；运行元数据与 checkpoint 都需要持久目录。`CORTEX_WEB_CHECKPOINT_URL` 可指定 SQLite 文件，默认每次 run 独立保存到 `workspace/web/runs/<id>/merge-checkpoints.sqlite`。不接受 PostgreSQL 或内存 checkpoint URL。

集成门户使用现有 SiteHeader 导航，`/ontomosaic-text2sql` 与 `/ontology/` 均可进入。Vite、local-server 和 Nginx 通过同源 `/ontology/api/` 转发；完整系统的其他服务按门户原启动方式启动。

## 重新生成兼容图谱

```bash
.venv/bin/python scripts/prepare_ontology_demo.py \
  --database-root /path/to/dev_databases \
  --artifact-root /path/to/results_final/workspace/BIRD/autoonto
```

转换使用当前 `GraphManager` 严格校验，不放宽领域 schema。原有依赖边重新编译，因此边数与旧 HTML 中的统计可能不同；当前 financial 为 245 节点 / 339 边，thrombosis 为 284 节点 / 381 边，各 4 个冲突。`provenance.json` 记录原始 SHA256；旧字段、target ID、冲突和物理 JOIN 转换内容保存在 compatibility report。

## 验证

```bash
.venv/bin/python -m pytest tests/test_web_workbench.py -q
# 集成仓库
npm --prefix web run build
```

测试覆盖只读数据库、上传导入、重复请求、真实任务分发及 case 限制、生成结果隔离、官方 SQLite checkpoint 重开恢复、相对路径解析、图文件兼容与损坏拒绝。真实模型 smoke run 的结果应以运行目录和 API 状态为准。

## 门户完整启动

本地：设置 `ONTOLOGY_SOURCE=/path/to/autoonto-nl2sql-release` 后执行门户原 `start.sh`，会一并启动 ontology API。只调试本模块时使用上方 `start_ontology_web.sh`。

Docker：在门户仓库设置 `ONTOLOGY_SOURCE` 后执行 `docker compose -f docker-compose.yml -f docker-compose.ontology.yml up --build`。模型配置来自 ontology 的 `.env`，预置数据库只读挂载，任务及 SQLite checkpoint 使用 `ontology_runtime` 持久卷。本机没有 Docker，镜像与完整多引擎部署未实测。

2026-10-25 实测：网页上传并导入 financial source_id=139，点击真实构建按钮。run `ebc61c7038604840851e2b65166b5870` 完成，1/1 case stable_correct（1 次迭代），生成 68 节点 / 76 关系 / 0 冲突。实际约 372 秒，merge SQLite 文件存有 12 条 checkpoint。API 图谱与本次 CLI graph_state.json 一致。此前两条 case 的测试在第二条结果差异修复期间主动中断，不计通过。

进度面板位于数据库选择与 pairs 导入区域下方，展示五阶段流程、完成/运行/通过/失败 case 数、当前步骤与耗时。后续构建根据同库已完成的真实任务按 case 归一化耗时估算；无历史、超出历史估计或 merge 阶段明确显示无法可靠预测。顶部入口分别说明预置示例与已有 Graph JSON 文件的用途。

## 上传 SQLite 数据库

Database 区域支持上传 `.sqlite`、`.sqlite3`、`.db` 文件，单文件上限 200 MiB；SQL 文本和 JSON 不作为数据库文件接受。后端分块保存文件并检查 SQLite 文件头、quick_check 和用户表结构；损坏、空数据库或超限文件不注册，并清理失败文件。

上传文件保存在 `workspace/web/databases/<unique-id>/database.sqlite`，数据库注册信息持久化到同一运行目录的 metadata.sqlite，不写入预置数据库目录。同名文件具有不同 ID，不互相覆盖；Docker 运行目录持久卷同时保存这些文件。上传成功后页面自动选中新库并读取 schema/数据；随后可上传 pairs、导入并调用真实训练。新库没有预置示例 pairs/图谱，页面会说明相应入口不可用。训练仍针对只读 SQLite 快照执行。
