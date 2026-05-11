#!/usr/bin/env bash
# CIA — 市场情报分析技能 安装脚本
# 用法: bash install.sh
set -e

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.claude/skills/cia"
REPORTS_DIR="$HOME/workspace/analytics/reports"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   CIA — 首席情报官 市场分析技能 安装程序         ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. 检查 Python ─────────────────────────────────────
echo "→ 检查 Python 版本..."
if ! command -v python3 &>/dev/null; then
  echo "✗ 未找到 python3，请先安装 Python 3.10+"
  exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
  echo "✗ 需要 Python 3.10+，当前版本：$PY_VER"
  exit 1
fi
echo "  ✓ Python $PY_VER"

# ── 2. 复制技能文件 ────────────────────────────────────
echo "→ 安装技能文件到 $TARGET_DIR ..."
mkdir -p "$TARGET_DIR"
if [[ "$SKILL_DIR" != "$TARGET_DIR" ]]; then
  cp -r "$SKILL_DIR/." "$TARGET_DIR/"
  echo "  ✓ 文件已复制"
else
  echo "  ✓ 已在目标目录，跳过复制"
fi

# ── 3. 安装 Python 依赖 ────────────────────────────────
echo "→ 安装 Python 依赖..."
VENV_DIR="$TARGET_DIR/.venv"

# 优先使用 venv（更干净），fallback 到 --break-system-packages
if python3 -m venv "$VENV_DIR" &>/dev/null 2>&1; then
  "$VENV_DIR/bin/pip" install -q -r "$TARGET_DIR/scripts/requirements.txt"
  echo "  ✓ 依赖安装到 venv: $VENV_DIR"
  # 生成 wrapper 脚本，让 cli.py 自动用 venv
  cat > "$TARGET_DIR/scripts/cia" << WRAPPER
#!/usr/bin/env bash
"$VENV_DIR/bin/python3" "\$(dirname "\$0")/cli.py" "\$@"
WRAPPER
  chmod +x "$TARGET_DIR/scripts/cia"
  echo "  ✓ wrapper 脚本: $TARGET_DIR/scripts/cia"
elif pip3 install -q --user -r "$TARGET_DIR/scripts/requirements.txt" 2>/dev/null; then
  echo "  ✓ 依赖安装到用户目录 (--user)"
elif pip3 install -q --break-system-packages -r "$TARGET_DIR/scripts/requirements.txt" 2>/dev/null; then
  echo "  ✓ 依赖安装完成 (system)"
else
  echo "  ⚠ 自动安装失败，请手动安装："
  echo "     pip3 install -r $TARGET_DIR/scripts/requirements.txt"
fi

# ── 4. 创建报告目录 ────────────────────────────────────
echo "→ 创建报告目录 $REPORTS_DIR ..."
mkdir -p "$REPORTS_DIR"
echo "  ✓ $REPORTS_DIR"

# ── 5. 验证 CLI 可用 ───────────────────────────────────
echo "→ 验证 CLI..."
cd "$TARGET_DIR/scripts"
if python3 cli.py --help &>/dev/null; then
  echo "  ✓ CLI 可用"
else
  echo "  ✗ CLI 验证失败，请检查依赖"
  exit 1
fi

# ── 6. 检查凭据 ────────────────────────────────────────
echo ""
echo "→ 检查 API 凭据..."
SETTINGS="$HOME/.claude/settings.json"

check_cred() {
  local key=$1
  # 先查 settings.json
  if [[ -f "$SETTINGS" ]]; then
    val=$(python3 -c "import json; d=json.load(open('$SETTINGS')); print(d.get('env',{}).get('$key',''))" 2>/dev/null)
    if [[ -n "$val" ]]; then
      echo "  ✓ $key"
      return 0
    fi
  fi
  # 再查 shell env
  if [[ -n "${!key}" ]]; then
    echo "  ✓ $key (from shell env)"
    return 0
  fi
  echo "  ✗ $key  ← 未设置"
  return 1
}

CREDS_OK=true
check_cred "APIFY_TOKEN"     || CREDS_OK=false
check_cred "YOUTUBE_API_KEY" || CREDS_OK=false
check_cred "DATAFORSEO_BASIC_AUTH" || CREDS_OK=false

if [[ "$CREDS_OK" == "false" ]]; then
  echo ""
  echo "  ⚠ 部分凭据未设置，运行凭据配置向导："
  echo "     bash $TARGET_DIR/setup-credentials.sh"
fi

# ── 完成 ───────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✓ 安装完成！                                   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "快速开始:"
echo "  1. 设置凭据（如未完成）:"
echo "     bash $TARGET_DIR/setup-credentials.sh"
echo ""
echo "  2. 在 Claude Code 中直接说:"
echo "     '帮我分析 [市场方向] 的市场机会'"
echo "     '我想进入 [产品方向]，有哪些赛道'"
echo ""
echo "  3. 或手动运行 CLI:"
echo "     cd $TARGET_DIR/scripts"
echo "     python3 cli.py init \"your topic\" --country us"
echo ""
echo "  详细说明: $TARGET_DIR/QUICKSTART.md"
echo ""
