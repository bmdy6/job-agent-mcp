# 没有 Ego Lite 时怎么用

安装 Python 3.10+ 和 uv 后，`uv sync --project mcp` 即可使用本地岗位库、JD 提取、评分预览、漏斗和离线 HTML 看板。采集真实网站、投递、读取原站回复需要 Ego Lite；付费评分只需要配置好的模型 API 和用户授权。

## 用临时数据试一遍

以下命令从仓库根目录执行，`JOB_AGENT_HOME` 指向新建临时目录。演示配置和岗位都由示例创建。

```sh
export JOB_AGENT_HOME="$(mktemp -d)"
cat > "$JOB_AGENT_HOME/config.yaml" <<'YAML'
collect:
  keywords: [Agent]
  cities: [杭州]
YAML
uv run --project mcp job-agent collect --once --dry-run --platform boss
```

采集 dry-run 使用 FakeBrowser / FakeSource，只写 `collect-dry-run.sqlite3`。为了试用读取工具，下面在临时目录的 `jobs.sqlite3` 中创建一个独立的合成岗位；它不是从招聘网站采集的，也没有经过模型评分。

```sh
uv run --project mcp python - <<'PY'
import json
from job_agent.store import Store
from job_agent.service.read_tools import search_jobs, funnel_stats
from job_agent.service.scoring import preview_scoring
with Store() as store:
    ident = str(store.upsert_job(
        platform='boss', platform_job_id='offline-demo',
        title='Agent 开发（合成示例）', company='演示公司', city='杭州',
        salary='10-20K', jd='任职要求：\n熟悉 Python 和 SQL。',
        url='https://example.invalid/job'))
print(json.dumps(search_jobs(query='Agent'), ensure_ascii=False))
print(json.dumps(preview_scoring([ident]), ensure_ascii=False))
print(json.dumps(funnel_stats(), ensure_ascii=False))
PY
uv run --project mcp job-agent dashboard --out "$JOB_AGENT_HOME/dashboard.html"
```

看板是一份本地 HTML，可直接打开。生成时只读数据库，不启动浏览器、不调用模型、不发通知。演示没有投递和回复，漏斗为零是正常结果。

## 用自己的岗位库

把 `JOB_AGENT_HOME` 指向已有且已初始化的本地数据目录，即可通过 MCP 的 `search_jobs`、`get_job`、`jd_requirements`、`funnel_stats`、`greeting_stats` 和 `dashboard` 查看保存的数据，注册命令见 [使用说明](usage.md)。只读工具不会替你创建或迁移缺失的岗位库。

`preview_scoring` 只估算请求和检查配置。需要真实评分时，先按 [评分配置](usage.md) 设置模型、简历和凭证引用，再由 Agent 展示预览，获得用户授权后调用 `start_scoring(confirm=true)`；可能产生 API 费用。评分走 service/store，不依赖 Ego Lite。没有模型凭证时仍可搜索已有岗位、读历史评分、提取 JD 原句和查看统计。

`setup_check` 包含浏览器登录预检，没有 Ego Lite 时相应项会失败；这不妨碍上述本地功能。投递 dry-run 也有独立数据库和 FakeAdapter，具体预览及确认流程见 [投递说明](usage.md)；真实投递仍受默认关闭、批次确认与回执护栏约束。
