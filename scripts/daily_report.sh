#!/bin/bash
# 懒人订阅每日汇报脚本
# 用法: bash daily_report.sh [星期几]
# 不传参数时自动读取当天星期（0=周日,1=周一...7=周日）
# 参数 1=周一, 2=周二, ..., 7=周日

ENV="/home/chenli/.openclaw/workspace-recruiter/skills/qas-lazy-cli/.env"

if [ -z "$1" ]; then
  DAY=$(date +%w)  # 0=周日,1=周一...
else
  DAY=$1
fi

echo "===== 懒人订阅每日汇报 ====="
echo ""

case $DAY in
  1)  echo "📅 周一" && qas_lazy_cli --env $ENV task status 百炼成神 ;;
  2)  echo "📅 周二" && qas_lazy_cli --env $ENV task status 仙剑 ;;
  3)  echo "📅 周三" && qas_lazy_cli --env $ENV task status 玄界之门 ;;
  4)  echo "📅 周四" && qas_lazy_cli --env $ENV task status 逆天邪神 && qas_lazy_cli --env $ENV task status 驭灵师 && qas_lazy_cli --env $ENV task status 将夜 ;;
  5)  echo "📅 周五" && qas_lazy_cli --env $ENV task status 大主宰 && qas_lazy_cli --env $ENV task status 沧元图 ;;
  6)  echo "📅 周六" && qas_lazy_cli --env $ENV task status 择天记 && qas_lazy_cli --env $ENV task status 仙武传 && qas_lazy_cli --env $ENV task status 光阴之外 && qas_lazy_cli --env $ENV task status 斗破苍穹 && qas_lazy_cli --env $ENV task status 永生 ;;
  7|0) echo "📅 周日" && qas_lazy_cli --env $ENV task status 成何体统 && qas_lazy_cli --env $ENV task status 牧神记 && qas_lazy_cli --env $ENV task status 仙逆 ;;
esac

echo ""
echo "===== 汇报完毕 ====="