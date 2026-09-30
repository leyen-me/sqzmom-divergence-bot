#!/usr/bin/env bash
# 运行全部测试。若环境里没有 OKX_API_KEY, API 集成测试会自动跳过。
set -e
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v "$@"
