#!/usr/bin/env bash
# CIA 凭据配置向导 — 交互式设置 API Key
# 用法: bash setup-credentials.sh
set -e

SETTINGS="$HOME/.claude/settings.json"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   CIA — API 凭据配置向导                         ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "需要配置 3 个 API Key（全部可免费/低成本获取）："
echo ""
echo "  1. APIFY_TOKEN       — TikTok/Reddit 社交数据（免费额度够用）"
echo "     获取地址: https://console.apify.com/account/integrations"
echo ""
echo "  2. YOUTUBE_API_KEY   — YouTube 视频数据（完全免费）"
echo "     获取地址: https://console.cloud.google.com → APIs & Services → YouTube Data API v3"
echo ""
echo "  3. DATAFORSEO_BASIC_AUTH — App Store ASO 关键词（最低充值 \$5）"
echo "     获取地址: https://dataforseo.com → 注册后进 Dashboard 复制 login:password"
echo "     格式: 将 login:password 做 base64 编码"
echo "     命令: echo -n 'your@email.com:yourpassword' | base64"
echo ""

# ── 读取现有值 ────────────────────────────────────────────
get_existing() {
  local key=$1
  if [[ -f "$SETTINGS" ]]; then
    python3 -c "import json; d=json.load(open('$SETTINGS')); print(d.get('env',{}).get('$key',''))" 2>/dev/null || echo ""
  fi
}

save_to_settings() {
  local key=$1
  local val=$2
  python3 - "$SETTINGS" "$key" "$val" << 'PYEOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text()) if p.exists() else {}
s.setdefault("env", {})
s["env"][sys.argv[2]] = sys.argv[3]
p.write_text(json.dumps(s, indent=2, ensure_ascii=False))
PYEOF
}

# ── APIFY_TOKEN ───────────────────────────────────────────
existing=$(get_existing "APIFY_TOKEN")
if [[ -n "$existing" ]]; then
  echo "APIFY_TOKEN 已设置（${existing:0:12}...），按 Enter 保留，或输入新值："
else
  echo "请输入 APIFY_TOKEN（格式: apify_api_xxxxxx）："
fi
read -r input_apify
if [[ -n "$input_apify" ]]; then
  save_to_settings "APIFY_TOKEN" "$input_apify"
  echo "  ✓ APIFY_TOKEN 已保存"
elif [[ -n "$existing" ]]; then
  echo "  ✓ APIFY_TOKEN 保留原值"
else
  echo "  ⚠ APIFY_TOKEN 未设置，TikTok/Reddit 数据采集将不可用"
fi

echo ""

# ── YOUTUBE_API_KEY ───────────────────────────────────────
existing=$(get_existing "YOUTUBE_API_KEY")
if [[ -n "$existing" ]]; then
  echo "YOUTUBE_API_KEY 已设置（${existing:0:12}...），按 Enter 保留，或输入新值："
else
  echo "请输入 YOUTUBE_API_KEY（格式: AIzaxxxxxxxx）："
fi
read -r input_yt
if [[ -n "$input_yt" ]]; then
  save_to_settings "YOUTUBE_API_KEY" "$input_yt"
  echo "  ✓ YOUTUBE_API_KEY 已保存"
elif [[ -n "$existing" ]]; then
  echo "  ✓ YOUTUBE_API_KEY 保留原值"
else
  echo "  ⚠ YOUTUBE_API_KEY 未设置，YouTube 数据采集将不可用"
fi

echo ""

# ── DATAFORSEO_BASIC_AUTH ─────────────────────────────────
existing=$(get_existing "DATAFORSEO_BASIC_AUTH")
echo "DataForSEO 设置（App Store ASO 关键词，每个竞品约 \$0.01）："
echo "先把 login:password 做 base64："
echo "  命令: echo -n 'your@email.com:yourpassword' | base64"
echo ""
if [[ -n "$existing" ]]; then
  echo "DATAFORSEO_BASIC_AUTH 已设置，按 Enter 保留，或输入新的 base64 值："
else
  echo "请输入 DATAFORSEO_BASIC_AUTH（base64 编码后的值，或直接输入 login:password）："
fi
read -r input_dfs
if [[ -n "$input_dfs" ]]; then
  # 如果包含 : 说明是原始 login:password，需要 base64
  if echo "$input_dfs" | grep -q ":"; then
    input_dfs=$(echo -n "$input_dfs" | base64)
    echo "  → 已自动 base64 编码"
  fi
  save_to_settings "DATAFORSEO_BASIC_AUTH" "$input_dfs"
  echo "  ✓ DATAFORSEO_BASIC_AUTH 已保存"
elif [[ -n "$existing" ]]; then
  echo "  ✓ DATAFORSEO_BASIC_AUTH 保留原值"
else
  echo "  ⚠ DATAFORSEO_BASIC_AUTH 未设置，App Store ASO 功能将不可用"
fi

echo ""

# ── 验证 ──────────────────────────────────────────────────
echo "→ 验证凭据配置..."
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SKILL_DIR/scripts"

check() {
  local key=$1
  val=$(python3 -c "
import json, pathlib, os
p = pathlib.Path.home() / '.claude/settings.json'
s = json.loads(p.read_text()) if p.exists() else {}
v = os.environ.get('$key') or s.get('env',{}).get('$key','')
print(v[:8] + '...' if len(v) > 8 else v)
" 2>/dev/null)
  if [[ -n "$val" && "$val" != "..." ]]; then
    echo "  ✓ $key: $val"
  else
    echo "  ✗ $key: 未设置"
  fi
}

check "APIFY_TOKEN"
check "YOUTUBE_API_KEY"
check "DATAFORSEO_BASIC_AUTH"

echo ""
echo "✓ 凭据配置完成。"
echo ""
echo "快速测试（需要 Apify Token）："
echo "  cd $SKILL_DIR/scripts"
echo "  python3 cli.py init \"test topic\" --country us"
echo "  python3 cli.py fetch-tiktok --topic \"test topic\" --queries \"ai tools\" --max-items 5"
echo ""
