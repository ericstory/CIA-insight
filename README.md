# CIA — 首席情报官（市场机会分析）

> **反确认偏误**的市场情报分析工具。输入你想做的方向，输出 7 条按数据排序的赛道——结论可能和你想的完全不一样。

---

## 这是什么

CIA 是一个 Claude Code skill，帮你回答：**"这个市场值不值得做？有没有更大的机会？"**

- 自动拉取 TikTok / YouTube / App Store / Google 关键词 / 竞品流量等多源数据
- Python 处理数据，LLM 只做判断，结果可审计
- 输出 HTML 报告 + Excel 原始数据，可直接发给团队

**核心承诺**：你的假设只是 7 条赛道之一，报告排序完全由数据决定，不迎合你的预期。

---

## 安装（5 分钟）

### 第一步：克隆到 Claude skills 目录

```bash
git clone git@github.com:ericstory/CIA-insight.git ~/.claude/skills/cia
```

### 第二步：安装依赖

```bash
bash ~/.claude/skills/cia/install.sh
```

### 第三步：配置访问凭据

CIA 通过一个统一数据网关运行，**你不需要自己申请任何 API key**。

只需在 `~/.claude/settings.json` 的 `env` 字段加两行：

```json
{
  "env": {
    "CIA_HUB_URL": "https://cia.ericstory.me",
    "CIA_HUB_TOKEN": "你的专属token"
  }
}
```

**Token 获取方式**：联系 @andrew 单独索取，每人一个独立 token。

---

## 使用

安装完成后，在 Claude Code 中直接说：

```
帮我分析"AI 语音客服"市场，有哪些赛道值得做
我想做 LLM API 中转站，你觉得怎么样
分析一下 Duolingo 的竞品格局
```

CIA skill 会自动触发，拉数据、跑分析、生成报告。

报告输出到：`~/workspace/analytics/reports/<日期>-cia-<话题>/`
- `report.html` — 浏览器打开，可搜索排序
- `data.xlsx` — 原始数据，16 个 sheet
- `synthesis.md` — 战略分析文本

---

## 手动 CLI（可选）

也可以直接跑 CLI 精细控制每一步：

```bash
cd ~/.claude/skills/cia/scripts

# 建话题
python3 cli.py init "your topic" --country us

# 拉社交数据（走 Hub，无需本地 key）
python3 cli.py fetch-tiktok  --topic "your topic" --queries "keyword1,keyword2" --max-items 40
python3 cli.py fetch-youtube --topic "your topic" --queries "keyword1" --per-query 10
python3 cli.py fetch-itunes-serp --topic "your topic" --keywords "keyword1,keyword2" --limit 20

# 拉 Ahrefs 数据（走 Hub，无需 MCP）
python3 cli.py fetch-ahrefs-kw   --topic "your topic" --keywords "keyword1,keyword2"
python3 cli.py fetch-ahrefs-site --topic "your topic" --targets "competitor.com"

# 查看数据量
python3 cli.py status --topic "your topic"

# 导出报告
python3 cli.py export --topic "your topic" --synthesis-file synthesis.md
```

---

## 数据源（全部通过 Hub 代理，无需自行配置）

| 数据源 | 用途 |
|--------|------|
| TikTok (Apify) | 传播侧 leading indicator |
| YouTube API | 内容趋势 + 钩子分析 |
| App Store (iTunes) | 竞品发现 + ASO |
| DataForSEO | App 关键词 + 评论痛点 |
| Ahrefs | Google KW + 竞品 Web 流量 |

---

## 常见问题

**Q: 不需要任何 API key 吗？**
是的。所有数据源都通过 Hub 代理，你只需要一个 CIA_HUB_TOKEN。

**Q: 没有 token 能用吗？**
不能。联系 @andrew 获取。

**Q: 报告在哪里？**
`~/workspace/analytics/reports/<日期>-cia-<话题>/report.html`，浏览器直接打开。

**Q: 数据会保存在哪里？**
本地 SQLite，在你自己机器上，不上传到任何地方。
