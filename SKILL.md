---
name: cia
description: |
  首席情报官 — 反确认偏误市场机会分析。
  
  触发场景：
  - 用户想进入一个新市场/产品方向，需要客观评估（不是证明想法对）
  - 用户提供竞品列表或 App 名称，想了解市场格局
  - 用户想找 PLG 增长机会（不依赖销售，下载即用）
  - 用户问"这个赛道值不值得做"、"有没有更大的机会"
  
  核心承诺：输出 7 条赛道，按数据排序，用户假设放倒数第 2 章，结论可能推翻用户方向。
  
  当用户提到：市场分析、竞品分析、机会发现、App 市场、关键词研究、ASO、
  进入某个赛道、分析某个产品方向、找增长机会 时使用。

metadata: {"openclaw":{"emoji":"🕵️","requires":{"env":["DATAFORSEO_BASIC_AUTH","APIFY_TOKEN","YOUTUBE_API_KEY"]}}}
---

# CIA — 市场情报分析（v4 反确认偏误版）

## 设计原则

> **Python 干固定的事，LLM 干判断的事。**
>
> 数据拉取、结构化存储、表格导出 → Python（可重复、可审计、低成本、任何人可重跑）
> 意图扩容、解读合成、战略判断 → LLM（在两端用，不在中间用）

> **CIA 找机会，不证明想法。** 用户输入仅用作"启动种子"，不作为分析框架。
>
> **反确认偏误 5 铁律：**
> 1. **用户输入是 query，不是 frame。** 竞品池 ≥50 后，从种子集**移除**用户原始词，重做聚类。
> 2. **赛道按 `total_reviews × plg_score` 排序，禁止按相关性排序。** 用户假设和其他赛道完全平权。
> 3. **必须保留"远赛道"。** 任何 cluster `total_reviews > 用户假设赛道 × 3` 必须独立成卡，禁止折叠。
> 4. **每条赛道强制 PLG 体检。** TTV（秒）/ setup_cost（0/低/中/高）/ viral_loop（内/外/无）/ sales_dependency 四项缺一不可。
> 5. **用户原始假设放在报告倒数第二章。** 章节顺序固定：赛道全景→7条卡片→PLG矩阵→传播洞察→组合策略→**用户假设批判（倒数第2）**→行动建议。

## 三层分析框架（v3 核心）

```
传播侧（leading）→ 需求侧（显性化）→ 供给侧（承接）→ 反哺传播侧
  TikTok/Reddit       Google KW              App/Web
  内容创造需求         搜索意图可测            产品竞争格局
```

- **传播侧**：用户因为什么内容/素材才知道并选择这个产品？(share_rate, viral hook)
- **需求侧**：用户主动在搜什么？划定边界，用 `intent_weight` 加权（交易×1.0，信息×0.1）
- **供给侧**：谁在满足需求？Python 聚类分赛道，LLM 只负责命名

**竞品发现是循环，不是线性**：
```
种子词 → App SERP → 竞品 ASO 词 → 新种子 → 新竞品 → ... (max 3 轮)
```
终止条件：新词 < 50 OR Jaccard > 0.70 OR 已跑 3 轮

---

## 一、数据架构

```
~/workspace/analytics/reports/<YYYY-MM-DD>-cia-<topic-slug>/
  cia.db                      # ★ canonical SQLite — 所有数据真相源
  data.xlsx                   # 多 sheet Excel，投放/ASO 团队直接打开
  report.html                 # 可交互 HTML（DataTables 搜索/排序/导出 CSV）
  report.md                   # LLM 解读摘要
  raw/*.json                  # Ahrefs MCP 等响应的原始缓存
```

**SQLite 15 张表**：seeds / keywords_google / appstore_serp / appstore_keywords / competitors_app / competitor_clusters / app_reviews / competitors_web / competitor_organic_kw / ai_visibility / social_tiktok / social_reddit / social_youtube / topic_meta / fetch_log

---

## 二、API 分工矩阵（v2 锁定）

| 数据 | 渠道 | 工具 | 成本 |
|---|---|---|---|
| Google 关键词 vol/KD/CPC/intent | Ahrefs MCP | `keywords-explorer-*` | 含订阅 |
| Google SERP / 竞品 organic 词 | Ahrefs MCP | `site-explorer-organic-keywords` | 含订阅 |
| 竞品 Web 流量 / DR / backlinks | Ahrefs MCP | `site-explorer-metrics` | 含订阅 |
| **AI 引用份额（ChatGPT/Gemini）** | Ahrefs MCP | `brand-radar-sov-overview` | 含订阅 |
| **App Store SERP（keyword → 排名 App）** | iTunes Search API | `itunes.keyword_to_apps` | 免费 |
| **App ASO 关键词（app → 排名词 + SV）** | DataForSEO Labs | `keywords_for_app/live` | $0.01 + $0.0001/条 |
| **App 评论（痛点提炼）** | DataForSEO | `app_reviews/task_post` | $0.0015/50 |
| App 元数据 | iTunes Lookup | `itunes.lookup_apps` | 免费 |
| TikTok 内容 | Apify | `clockworks/free-tiktok-scraper` | ~$0.05/run |
| Reddit 讨论 | Apify | `trudax/reddit-scraper-lite` | ~$0.05/run |
| YouTube 视频 + Hook | YouTube Data API v3 | `search`+`videos` | 10K units/day 免费 |

---

## 三、标准工作流（Python CLI 主跑）

> 所有命令从 `~/.claude/skills/cia/scripts/` 目录运行。

### Step 0：建 topic
```bash
cd ~/.claude/skills/cia/scripts
python3 cli.py init "ai phone receptionist" --country us
```

### Step 1A：传播侧先行——拉社交信号（v3 新增，先于关键词扩容）
**传播侧是 leading indicator，搜索是 lagging indicator。**
先拉 TikTok/Reddit，了解用户真实语言，再用这些词扩充种子。
```bash
python3 cli.py fetch-tiktok  --topic "ai phone receptionist" \
  --queries "ai receptionist,ai answering service,ai phone answering" --max-items 80
python3 cli.py fetch-reddit  --topic "ai phone receptionist" \
  --queries "ai receptionist,answering service small business" \
  --subreddits "smallbusiness,Entrepreneur,startups" --max-items 60

# 提取高互动内容中的词汇 → 保存为 demand|social_signal 种子
python3 cli.py social-to-seeds --topic "ai phone receptionist" \
  --min-share-rate 0.005 --min-reddit-score 10 --min-count 3
```

### Step 1B：意图扩容（LLM 出力，基于真实社交词）
读取 social-to-seeds 输出，结合用户输入，按 20 维扩容（详见 §五）。
把种子写到 `seeds.txt`：
```
demand|core|ai receptionist
demand|core|ai answering service
demand|social_signal|ai phone answering app
demand|audience|ai receptionist for dentist
supply|competitor|openphone
```
然后：
```bash
python3 cli.py seed-save --topic "ai phone receptionist" --file seeds.txt
```

### Step 2：拉 Ahrefs Google 关键词（MCP 调用 + ingest）
对每组种子词调 MCP，把 JSON 落盘后 ingest：
```bash
# 1. Claude 在对话中调 MCP（用 ToolSearch 加载 schema）
mcp__claude_ai_Ahrefs__keywords-explorer-overview \
  keywords="ai receptionist,ai answering service,ai phone answering" \
  country=us select=keyword,volume,difficulty,cpc,traffic_potential,intents
# 2. 把响应 JSON 保存到 raw/ahrefs-overview-1.json
# 3. ingest
python3 cli.py ingest-ahrefs --topic "ai phone receptionist" \
  --kind keywords --file raw/ahrefs-overview-1.json --source-seed "ai receptionist"
```
**关键操作**：每组核心词跑 `matching-terms`（limit 100）+ `related-terms`（limit 50），全部 ingest。**不要**在 LLM 阶段砍数据 — 全部进 SQLite，最后让 Excel/HTML 渲染。

### Step 3：拉 Brand Radar（AI 引用份额）
```bash
mcp__claude_ai_Ahrefs__brand-radar-sov-overview keywords="ai receptionist" country=us
# 保存到 raw/brand-radar-sov.json
python3 cli.py ingest-ahrefs --topic "..." --kind brand-radar \
  --file raw/brand-radar-sov.json
```

### Step 4：拉 App Store SERP（iTunes 免费）
用 Step 2 的 Google 词 + Step 1A 的社交词去搜 App Store：
```bash
python3 cli.py fetch-itunes-serp --topic "ai phone receptionist" \
  --keywords "ai receptionist,ai answering service,ai phone answering,ai virtual receptionist,ai call answering" \
  --limit 20
```

### Step 5：竞品 App 元数据
```bash
python3 cli.py fetch-competitors-meta --topic "ai phone receptionist"
# 自动从 appstore_serp 提取所有 app_id，调 iTunes lookup 拿全量元数据
```

### Step 6：★ 循环发现（v3 核心新增）
竞品 ASO 词 → 新词 → 新竞品 → 循环，max 3 轮，Jaccard > 0.70 或新词 < 50 自动停：
```bash
python3 cli.py discover-loop --topic "ai phone receptionist" \
  --max-rounds 3 --min-new-kw 50 --max-jaccard 0.70 \
  --aso-limit 200 --max-apps 20
# dry-run 先预览成本：
python3 cli.py discover-loop --topic "..." --dry-run
```
loop 完成后重跑 fetch-competitors-meta 补全新竞品元数据：
```bash
python3 cli.py fetch-competitors-meta --topic "ai phone receptionist"
```

### Step 7：App 评论（痛点）
```bash
python3 cli.py fetch-app-reviews --topic "..." --top 5 --depth 200
# Top 5 竞品（按 review_count）× 200 review = 约 $0.03
```

### Step 8：竞品 Web 流量（Ahrefs MCP）
对每个竞品域名：
```bash
mcp__claude_ai_Ahrefs__site-explorer-metrics target=quo.com country=us
python3 cli.py ingest-ahrefs --topic "..." --kind site-metrics \
  --file raw/ahrefs-quo-metrics.json --domain quo.com

mcp__claude_ai_Ahrefs__site-explorer-organic-keywords \
  target=quo.com country=us limit=100 order_by=volume:desc
python3 cli.py ingest-ahrefs --topic "..." --kind organic-kw \
  --file raw/ahrefs-quo-orgkw.json --domain quo.com
```

### Step 9：补充 YouTube 信号
（TikTok/Reddit 已在 Step 1A 提前拉取，此处补 YouTube）
```bash
python3 cli.py fetch-youtube --topic "..." --queries "ai receptionist demo,ai answering service review" --per-query 15
```

### Step 10：检查数据
```bash
python3 cli.py status --topic "ai phone receptionist"
# 显示每张表行数 + 总成本
```

### Step 10.5：★ 竞品聚类（v3 新增，LLM 合成前先做）
Python 用 KMeans + TF-IDF 自动把竞品分群，LLM 只给每群命名：
```bash
python3 cli.py cluster-competitors --topic "ai phone receptionist"
# 输出每个 cluster 的 app 列表 + keyword hints

# 然后在 DB 里给每个 cluster 命名（LLM 看 hints 后填写）：
sqlite3 ~/workspace/analytics/reports/<date>-cia-.../cia.db \
  "UPDATE competitor_clusters SET cluster_label='AI Phone Receptionist' WHERE cluster_id=0"
```

### Step 11：LLM 写解读 → 导出
1. 用 sqlite3 读关键聚合（详见 §六）
2. LLM 写 `synthesis.md`（只写解读段落，**不写表**）
3. 导出：
```bash
python3 cli.py export --topic "ai phone receptionist" --synthesis-file synthesis.md
# 生成：data.xlsx + report.html（含 LLM 摘要 + 14 张可搜索表）
```

---

## 四、Mode B（竞品列表驱动）

输入是竞品列表（含 App 名/域名）：
1. 把竞品名作为种子，调 iTunes Search 找 app_id → 直接 `fetch-competitors-meta`
2. 跑 Step 6-8 补 ASO + Web 数据
3. 反向：从竞品 organic 词反推 Google 关键词种子，再跑 Step 2
4. Step 9 社交信号
5. Step 11 合成

---

## 五、意图扩容 20 维（LLM 头部出力）

**需求侧 1-7**：核心问题词 / 受众细分词 / 场景触发词 / 痛点量化词 / 用户自描述词 / 相邻扩展词 / 平台偏重
**供给侧 8-12**：现有解决方案词 / 付费决策词 / 竞品获客渠道 / 传统替代品 / 竞品投诉词
**Gap 侧 13-17**：被忽视受众 / 场景空白 / 地域空白 / 功能空白 / 定价空白
**行动侧 18-20**：渠道判断 / 变现模式 / 时机判断

> 关键：扩容产物是**种子词列表**，不是分析。分析在 Step 11 用 SQLite 数据写。

---

## 六、合成阶段的 SQL 速查（LLM 用）

```sql
-- ★ 赛道概览（cluster-competitors 跑完后，先看这个）
SELECT cc.cluster_id, cc.cluster_label,
       COUNT(*) AS n_apps, SUM(ca.review_count) AS total_reviews,
       AVG(ca.rating) AS avg_rating, cc.top_keywords
FROM competitor_clusters cc
JOIN competitors_app ca ON ca.app_id = cc.app_id
GROUP BY cc.cluster_id ORDER BY total_reviews DESC;

-- Golden 关键词（高 vol + 低 KD + 高 CPC，按 intent 加权）
SELECT keyword, volume, kd, cpc_usd, intent,
       CASE intent
         WHEN 'commercial' THEN volume * 1.0
         WHEN 'transactional' THEN volume * 1.0
         WHEN 'informational' THEN volume * 0.1
         ELSE volume * 0.5 END AS weighted_vol
FROM keywords_google
WHERE volume>=200 AND kd<=35 AND cpc_usd>=1
ORDER BY weighted_vol DESC LIMIT 30;

-- App Store 蓝海词（搜索量大但只有 ≤3 个 App 占据）
SELECT keyword, search_volume, COUNT(DISTINCT app_id) AS n
FROM appstore_keywords WHERE search_volume>=500
GROUP BY keyword HAVING n<=3 ORDER BY search_volume DESC LIMIT 30;

-- TikTok 高分享率内容（传播侧 leading indicator）
SELECT plays, likes, shares,
       ROUND(CAST(shares AS REAL)/plays, 5) AS share_rate, text, url
FROM social_tiktok WHERE plays>100
ORDER BY share_rate DESC LIMIT 15;

-- Reddit 高分痛点帖（需求侧语言）
SELECT score, num_comments, subreddit, title, body FROM social_reddit
ORDER BY score DESC LIMIT 20;

-- App 评论高频痛点（供给侧弱点）
SELECT body FROM app_reviews WHERE rating<=3 AND length(body)>50
ORDER BY posted_at DESC LIMIT 50;

-- AI 引用份额（传播侧 AI 渠道）
SELECT brand, platform, sov_pct, mentions FROM ai_visibility
ORDER BY sov_pct DESC LIMIT 20;
```

---

## 七、报告固定结构（synthesis.md）—— **v4 反确认偏误模板**

> **核心原则**：**用户假设不置顶。赛道排序键 = `total_reviews × plg_score`，与用户输入无关。**
>
> LLM 写 synthesis 的 3 步：
> 1. `SELECT cluster_id, SUM(review_count), COUNT(*) FROM competitor_clusters JOIN competitors_app GROUP BY cluster_id ORDER BY SUM(review_count) DESC` → 按体量排全部 cluster
> 2. 给每个 cluster 命名（来自 subtitle TF-IDF 高频词，不用 LLM 猜）
> 3. 对每个 cluster 跑 PLG 体检（TTV/setup_cost/viral_loop/sales_dep）→ 算综合得分

```
# 一、赛道全景（按 total_reviews 降序——用户方向不置顶）
| # | Cluster | 赛道名 | Apps | 总 Reviews | 估算 TAM | PLG 得分 | 与用户方向关系 |
（用户假设赛道在此表中按数据排名，不加任何特殊标记）

# 二、Top 7 赛道详细卡片（每条必须包含三层 + PLG 体检）

## Track N: <赛道名>（TAM 估算）
### 传播侧（Leading Indicator）
| 平台 | 高效内容类型 | share_rate/score | 代表钩子 |
（来自实际 TikTok/Reddit 数据，禁止 LLM 编造）

### 需求侧（Lagging Indicator）
| 关键词 | vol/SV | KD | CPC | intent |
（金 KW：vol≥200, KD≤35, CPC≥1 / 蓝海 ASO：SV≥500 且占位 App≤3）

### 供给侧（竞争格局）
| 竞品 | reviews | 定价模式 | 主要获客渠道 |
（Top 3 竞品，来自 SQL 数据，不要 LLM 补充）

### PLG 体检
| TTV | setup_cost | viral_loop | sales_dep | 综合 PLG 得分 |
| （秒） | 0/低/中/高 | 产品内/外/无 | 0/低/中/高 | 0-10 |

### 切入角度（≤2条，必须基于评论痛点或 ASO 空白）

# 三、PLG 全赛道对照矩阵
| 赛道 | TTV(s) | setup_cost | viral_loop | sales_dep | PLG得分 |

# 四、传播侧跨赛道洞察
（TikTok/Reddit 高传播内容的共同规律——不分赛道的通用 hook 结构）

# 五、跨赛道组合策略
## 5.1 单赛道（按资源约束排序）
## 5.2 双赛道最高 ROI 组合
## 5.3 3/6/12 个月分阶段路线

# 六、对用户原始假设的批判性评估 ★ 倒数第二章
| 用户假设 | 数据排名 | 与最大赛道 TAM 差距 | 反向证据 | 建议 |
（必须明确写出"你假设的赛道在全体中排第几"）

# 七、数据出处 + 自检结果
```

### TAM 估算公式（v4）

```
PLG 赛道 TAM = cluster_total_reviews × 100 × 付费转化率 × ARPU/月 × 12
KW 侧 TAM  = Σ(volume × intent_weight) × 转化率 × ARPU
  intent_weight: transactional=1.0 / commercial=0.8 / informational=0.1

PLG 得分 = (10 - TTV/60) × 0.3 + (1 - setup_cost_index) × 0.3
           + viral_loop_score × 0.25 + (1 - sales_dep_index) × 0.15
```

> **synthesis.md 只放**：赛道总览、卡片、PLG矩阵、传播洞察、组合策略、用户假设批判、行动建议。
> **原始数据（词单/评论/社交帖子）** → 全在 Excel + HTML，禁止搬入 synthesis。

---

## 八、成本预算

单次完整 CIA 报告 ~$1-3：
- DataForSEO Keywords-for-App × 20 个竞品 ~ $0.30
- DataForSEO App Reviews × 5 个竞品 × 200 review ~ $0.03
- Apify TikTok + Reddit ~ $0.30

DataForSEO 余额查询：
```bash
curl -s -X GET "https://api.dataforseo.com/v3/appendix/user_data" \
  -H "Authorization: Basic $(python3 -c 'import json,pathlib; print(json.loads(pathlib.Path.home().joinpath(".claude/settings.json").read_text())["env"]["DATAFORSEO_BASIC_AUTH"])')" \
  | python3 -c "import sys,json; print('$'+str(json.load(sys.stdin)['tasks'][0]['result'][0]['money']['balance']))"
```

---

## 九、关键 don'ts

- ❌ **不要让 LLM 手抄数据进 markdown** — 全走 Python → SQLite → Excel/HTML
- ❌ **不要在 LLM 阶段砍数据** — Ahrefs 给多少 ingest 多少，导出层负责筛选展示
- ❌ **不要写"展示型"报告** — 投放团队要原始词单 (xlsx)，不是 15 行精选
- ❌ **不要单方向证明用户假设** — 必须给 7 条赛道按 TAM×PLG 排序，用户假设不置顶
- ❌ **不要重复调 API** — 每次 run 前先 `cli.py status` 看 DB 已有什么；重跑要明确删表
- ❌ **不要跳过 cluster-competitors 直接写 synthesis** — v4 赛道必须从 Python 聚类结果出发
- ❌ **不要把 TikTok 当"验证工具"** — TikTok/Reddit 是 leading indicator，Step 1 就运行
- ❌ **不要把用户假设赛道置顶** — 报告排序键是 `total_reviews × plg_score`，不是 relevance
- ❌ **不要因"看起来不相关"过滤大赛道** — 任何 cluster `total_reviews > 用户假设 × 3` 必须独立成卡
- ❌ **不要跳过 PLG 体检** — TTV/setup_cost/viral_loop/sales_dep 四项缺一不发报告

---

## 十、反偏误自检清单（synthesis.md 提交前必跑）

> 以下 6 项全部 ✅ 才能提交报告，任何一项 ❌ 必须重写对应章节。

- [ ] 报告第一屏赛道排序是 `total_reviews × plg_score`，**不是**按用户输入相关性？
- [ ] 用户原始假设赛道**没有**出现在前 3 张卡片里（除非它真的按数据排名前 3）？
- [ ] 是否有任何 cluster `total_reviews > 用户假设赛道 × 3` 被折叠/忽略？若有，**重写**。
- [ ] 每条赛道都有 TTV / setup_cost / viral_loop / sales_dep 四项 PLG 数值？
- [ ] "批判性评估"章节明确写出了"用户假设的赛道排第几"和"与最大赛道的 TAM 差距倍数"？
- [ ] 行动建议敢于推翻用户原始方向？（数据支持推翻时必须推翻，不允许 hedge）

---

## 十一、实战经验录（有数据前 vs 有数据后的结论对比）

> 这些经验来自一次完整的 AI Phone 市场分析实战（2026-05-11），记录了**没有数据时 LLM 的系统性偏误**，以及**拿到数据后结论如何 180° 反转**。

### 经验 1：没有数据时，LLM 必然迎合用户假设

**发生了什么：**
用户输入"AI phone receptionist"，初始分析把 AI 接待员放在报告第一位，把实时翻译（AI Phone 70K reviews）当噪声过滤掉。

**有数据之后：**
- 翻译赛道 TikTok 总播放 **100.8M** vs AI 接待员 **37.2M**（2.7 倍差距）
- App Store "translate" SV = 1,889,820 vs "ai receptionist" SV **< 50**（37,796 倍差距）
- 结论完全反转：建议先做翻译 PLG，AI 接待员是 12 个月后的衍生

**教训：** 在拿到社交数据和 ASO 数据之前，**不要写任何赛道排序结论**。顺序必须由数据决定。

---

### 经验 2：社交数据是最重要的 leading indicator，不是"验证工具"

**发生了什么：**
TikTok 原来被放在 Step 9（流程末尾），用来"验证"已有结论。

**有数据之后：**
- TikTok 爆款内容（sr > 1%）100% 是 B2B 服务商视角，0 条是消费者"下载即用"视角
- 这个发现直接否定了 PLG App Store 路线
- 同时，翻译赛道 TikTok 出现 iOS 26 / Samsung / T-Mobile 平台内置翻译的信号——这是**只有拉了数据才能发现的平台风险**

**教训：** TikTok/Reddit **必须在 Step 1 就跑**，用来决定种子词和方向，而不是在最后验证。传播侧是 leading indicator，搜索侧是 lagging indicator。

---

### 经验 3：App Store SV 和 Google KW 回答完全不同的问题

**发生了什么：**
分析一直说"'ai receptionist' 是高价值词"，依据是 Google KW vol=4,600 CPC=$7。

**有数据之后：**
- App Store "ai receptionist" SV **< 50**（DataForSEO 连这个词都不收录）
- Beside 实际靠 "voicemail"(SV 28,300) 拿 App Store 量
- Google vol=4,600 代表 B2B 采购意图，**不是 App Store 发现路径**

**教训：**
- `Google KW CPC 高` = B2B 采购意图，走 Web/SEO/SEM 渠道
- `App Store SV 高` = 消费者主动搜索，走 ASO 渠道
- 两者必须分开看，混用会导致渠道策略完全错误

---

### 经验 4：竞品 reviews 数量揭示市场真实渗透率，不是 TAM

**发生了什么：**
早期分析认为"AI 接待员市场空间大"，依据是 Google CPC 高。

**有数据之后：**
- AI 接待员头部 Beside：8,546 reviews（去除噪声后全赛道 **13,750** reviews）
- 相比虚拟号码赛道 **1,540,357** reviews，差距 **112 倍**
- 高 CPC 只说明"有人愿意买"，但 reviews 少说明"还没有人大规模在用"

**教训：** `reviews 数量 ≈ 市场渗透率`，`CPC ≈ 购买意愿`。两者都要看：高 CPC + 低 reviews = 早期市场，机会和风险并存。不能只看 CPC 就断定是大市场。

---

### 经验 5：聚类结果被种子词污染——竞品池决定分析边界

**发生了什么：**
用"ai receptionist"作为种子词跑 iTunes SERP，发现了 163 个竞品，自以为覆盖了市场。但 cluster 分析时发现翻译类 App（Translate Now 345K, AI Phone 70K）被错误分进噪声 cluster，差点被过滤掉。

**有数据之后：**
- 手动把翻译类 App 移入 C7，发现这是整个分析中 PLG 得分最高的赛道
- 如果没有这步修正，报告会完全遗漏最大机会

**教训：** 种子词决定了竞品池，竞品池决定了 cluster，cluster 决定了赛道。用户的输入词会把分析"锁定"在一个领域。**竞品池建好后，必须主动检查有没有"意外出现的大体量竞品"被分进了噪声 cluster。**

---

### 经验 6：平台风险只有真实数据才能发现

**发生了什么：**
分析早期把翻译赛道标注为"PLG 最优"，建议用户进入。

**有数据之后（YouTube 补采后）：**
- WWDC 2025 视频 **9.5M views** 确认 iOS 26 内置 Live Call Translation
- Samsung Galaxy Live Translate：3.3M TikTok plays
- T-Mobile 网络层翻译：TikTok 视频
- 结论修正为：通用翻译 App 窗口期 12-18 个月，必须走**专业场景**（医疗/法律/商务）规避平台风险

**教训：** 平台动向（Apple/Google/Samsung 新功能）会出现在 YouTube/TikTok 的热门内容里。**社交数据是发现平台风险的最快渠道**——比看新闻快，比看产品路线图准。

---

### 经验 7：Reddit actor 的局限性

**实测结论：**
`trudax/reddit-scraper-lite` 这个 Apify actor 有严重局限：
- 不能精准过滤查询词（`searches` 字段经常被忽略）
- 返回的帖子 `query` 字段为 NULL，无法归因
- 高分帖子基本都是 r/mildlyinfuriating 等娱乐社区的无关内容
- 每次运行只返回 4-6 条有效帖子

**当前有效 Reddit 信号的获取方式：** 只有两条路——人工搜索 Reddit，或用更好的 Apify actor（如 `apidojo/reddit-scraper`，付费）。免费 actor 的 Reddit 数据**不可用于传播侧分析**，只能作为参考。

---

### 经验 8：地理市场信号——印度/拉美是 voice AI 蓝海

**有数据之后（YouTube 补采后发现）：**
- Hindi "ai voice kaise banaye"（如何制作 AI 视频/声音）系列：每条 **300万-800万** 播放
- "FREE Ai Voices — Realistic TTS Hindi Natural Emotion"：**3M** 播放
- 西语"He Probado 200 Herramientas De IA"：550K 播放
- 拉美第二号码内容：745K TikTok plays（最高单条）

**教训：** 英语市场分析往往低估印度/拉美用户规模。YouTube 的标题语言可以直接反映受众地域。当看到 Hindi/Spanish 内容占据高播放位置时，说明这些市场存在**未被充分服务的需求**。

---

## 十二、扩展指南

新增数据源：
1. 加 fetcher 到 `scripts/fetchers/<name>.py`
2. 加 schema 到 `scripts/sql/schema.sql`
3. 加 CLI 命令到 `cli.py`
4. 加 sheet 到 `scripts/export/excel.py :: SHEETS`
5. 跑 `python3 cli.py status --topic <test>` 验证表可见

完整 schema：`scripts/sql/schema.sql`
原始 v1 skill（备份）：`SKILL.md.old`
