#!/usr/bin/env bash
# 使い方: bash tools/edit.sh <型の番号 1〜6> <動画のURL か ファイル> [ほかの動画...] [--keep-all]
# 終了コード 3 ＝ 計画を AI が書く番（作業/計画の頼み.md を読んで 計画.json を書き、同じコマンドをもう一度）
set -e
cd "$(dirname "$0")/.."
N="$1"; shift || true
K=$(ls -d kata/${N}_* 2>/dev/null | head -1)
[ -n "$K" ] || { echo "型 $N がありません（1〜6）"; ls kata; exit 1; }
[ -x .venv/bin/python ] || bash setup.sh
export KATA_WHISPER_DIR="$PWD/models/whisper-turbo"
VIDS=(); OPTS=()
for a in "$@"; do
  [ -n "$a" ] || continue
  case "$a" in
    --*) OPTS+=("$a");;
    http*://*)
      name=$(basename "${a%%\?*}"); name=$(.venv/bin/python -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.argv[1]))" "$name")
      dest="$K/1_入れる/$name"
      [ -s "$dest" ] || .venv/bin/python tools/up.py get "$a" "$dest"
      VIDS+=("$PWD/$dest");;
    *) VIDS+=("$(cd "$(dirname "$a")" && pwd)/$(basename "$a")");;
  esac
done
set +e
.venv/bin/python engine/kata_engine.py "$K" "${VIDS[@]}" --agent "${OPTS[@]}"
rc=$?
exit $rc
