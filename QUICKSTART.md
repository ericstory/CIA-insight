# CIA — 首席情报官 快速上手

> **一句话**：输入一个市场方向，CIA 返回 7 条按数据排序的赛道分析——结论可能和你想的完全不一样。

---

## 安装（5 分钟）

```bash
# 1. 安装技能文件 + Python 依赖
bash ~/.claude/skills/cia/install.sh

# 2. 配置 API Key（按向导操作）
bash ~/.claude/skills/cia/setup-credentials.sh
```

### 需要的 API Key

| Key | 用途 | 获取方式 | 费用 |
|---|---|---|---|
| `APIFY_TOKEN` | TikTok / Reddit 社交数据 | [apify.com](https://console.apify.com/account/integrations) | 免费额度够用 |
| `YOUTUBE_API_KEY` | YouTube 视频数据 | [Google Cloud Console](https://console.cloud.google.com) → YouTube Data API v3 | 完全免费 |
| `DATAFORSEO_BASIC_AUTH` | App Store ASO 关键词 | [dataforseo.com](https://dataforseo.com) → 注册 → 复制 login:password | 最低充值 $5 |

> **Ahrefs MCP**（可选）：提供 Google 关键词 + 竞品 Web 流量数据，需要 Ahrefs 订阅。没有也能运行，跳过 Step 2/3/8 即可。

---

## 使用方式

### 方式一：直接对话（推荐）

在 Claude Code 中说：

```
帮我分析"AI 语音客服"市场，有哪些赛道
我想做 LLM API 中转站，值不值得？
分析一下 Duolingo 的竞品格局
```

CIA 会自动触发并执行完整分析流程。

### 方式二：手动执行 CLI

```bash
cd ~/.claude/skills/cia/scripts

# 0. 建 topic（每个分析方向建一个）
python3 cli.py init "your market topic" --country us

# 1. 先拉社交数据（传播侧 leading indicator）
python3 cli.py fetch-tiktok --topic "your topic" \
  --queries "query1,query2,query3" --max-items 40

python3 cli.py fetch-youtube --topic "your topic" \
  --queries "query1,query2" --per-query 10

# 2. App Store 竞品（如果是 App 市场）
python3 cli.py fetch-itunes-serp --topic "your topic" \
  --keywords "keyword1,keyword2" --limit 20

python3 cli.py fetch-competitors-meta --topic "your topic"

# 3. 循环发现更多竞品（可选，花费约 $0.30）
python3 cli.py discover-loop --topic "your topic" --dry-run  # 先预览成本
python3 cli.py discover-loop --topic "your topic"

# 4. 竞品聚类
python3 cli.py cluster-competitors --topic "your topic"

# 5. 检查数据量
python3 cli.py status --topic "your topic"

# 6. 导出报告
python3 cli.py export --topic "your topic" --synthesis-file synthesis.md
```

报告输出到：`~/workspace/analytics/reports/<date>-cia-<topic>/`

---

## 报告结构（固定 7 章）

```
一、赛道全景（按 TAM×PLG 排序，用户假设不置顶）
二、Top 7 赛道详细卡片（每条：传播侧/需求侧/供给侧/PLG体检）
三、PLG 全赛道对照矩阵
四、传播侧跨赛道洞察
五、跨赛道组合策略
六、对用户原始假设的批判性评估 ← 用户自己提出的方向放这里
七、行动建议
```

**核心承诺**：你的假设只是 7 条赛道之一，排名完全由数据决定。

---

## 一次完整分析的费用

| 数据源 | 费用 | 说明 |
|---|---|---|
| TikTok (Apify) | ~$0.10 | 200 条视频 |
| Reddit (Apify) | ~$0.05 | 60 条帖子 |
| YouTube API | 免费 | 10K units/day |
| DataForSEO ASO | ~$0.30 | 20 个竞品 × 200 词 |
| DataForSEO 评论 | ~$0.03 | 5 个竞品 × 200 条 |
| iTunes SERP | 免费 | — |
| **合计** | **约 $0.50-1.50** | — |

---

## 已验证的市场分析案例

| 话题 | 关键发现 |
|---|---|
| AI Phone Receptionist | 用户假设排第 6；实时翻译赛道 TikTok 播放 2.7x，ASO 词量差 37,796 倍 |
| LLM API 中转站 | 用户假设排第 5-7；最大机会在消费中转站的客户端工具层（OpenClaw 189B tokens/week）|

---

## CLI 完整命令参考

```bash
cd ~/.claude/skills/cia/scripts

# 数据采集
python3 cli.py init <topic> [--country us]
python3 cli.py seed-save --topic <t> --file seeds.txt
python3 cli.py fetch-tiktok --topic <t> --queries "q1,q2" [--max-items 40]
python3 cli.py fetch-reddit --topic <t> --queries "q1,q2" [--max-items 60]
python3 cli.py fetch-youtube --topic <t> --queries "q1,q2" [--per-query 10]
python3 cli.py fetch-itunes-serp --topic <t> --keywords "kw1,kw2" [--limit 20]
python3 cli.py fetch-competitors-meta --topic <t>
python3 cli.py fetch-aso-keywords --topic <t> [--top 20] [--limit 200]
python3 cli.py fetch-app-reviews --topic <t> [--top 5] [--depth 200]

# v3 新功能
python3 cli.py social-to-seeds --topic <t> [--min-share-rate 0.005]
python3 cli.py discover-loop --topic <t> [--max-rounds 3] [--dry-run]
python3 cli.py cluster-competitors --topic <t> [--n-clusters 0]

# Ahrefs ingest（需要 Ahrefs MCP）
python3 cli.py ingest-ahrefs --topic <t> --kind keywords --file raw/xxx.json
python3 cli.py ingest-ahrefs --topic <t> --kind site-metrics --file raw/xxx.json --domain example.com
python3 cli.py ingest-ahrefs --topic <t> --kind organic-kw --file raw/xxx.json --domain example.com
python3 cli.py ingest-ahrefs --topic <t> --kind brand-radar --file raw/xxx.json

# 导出
python3 cli.py status --topic <t>
python3 cli.py export --topic <t> [--synthesis-file synthesis.md]
```

---

## 常见问题

**Q: 没有 DataForSEO 可以用吗？**
可以。跳过 `fetch-aso-keywords` 和 `fetch-app-reviews`，仍然能拿到 TikTok/YouTube/iTunes 数据做分析，只是没有 App Store 关键词数据。

**Q: 分析 Web 产品（不是 App）怎么用？**
跳过所有 `fetch-itunes-*` 和 `fetch-aso-*` 步骤，重点走社交数据 + Ahrefs Web 数据。

**Q: Reddit 数据很少怎么办？**
已知问题，免费 Apify actor 效果差。手动在 Reddit 搜索补充，或升级到 `apidojo/reddit-scraper` (付费)。

**Q: 报告放在哪里？**
`~/workspace/analytics/reports/<YYYY-MM-DD>-cia-<topic>/`
- `report.html` — 浏览器打开，可搜索 / 排序 / 导出 CSV
- `data.xlsx` — 原始数据，16 个 sheet
- `synthesis.md` — 战略分析文本

---

*CIA v4 — 反确认偏误市场情报分析技能*
*设计原则：Python 干固定的事，LLM 干判断的事。结论由数据决定，不由假设决定。*
