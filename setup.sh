#!/usr/bin/env bash
# すぐ来る 動画編集キット：最初に1回だけ（Claude Code のクラウド / Linux / Mac）
# 入れる物：ffmpeg（字幕を描く libass 入り）・Python の仮想環境（faster-whisper・Pillow・numpy・scipy）・文字起こしモデル
set -e
cd "$(dirname "$0")"
say(){ echo "▶ $*"; }

if ! command -v ffmpeg >/dev/null 2>&1 || ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass "; then
  say "ffmpeg を入れます"
  if command -v apt-get >/dev/null 2>&1; then
    SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo"
    $SUDO apt-get update -qq && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg >/dev/null
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "ffmpeg を入れられませんでした。手で入れてください。"; exit 1
  fi
fi
ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass " || { echo "この ffmpeg には字幕（libass）が入っていません"; exit 1; }
say "ffmpeg: $(ffmpeg -version | head -1)"

if [ ! -x .venv/bin/python ]; then
  say "Python の仮想環境を作ります"
  python3 -m venv .venv 2>/dev/null || { command -v apt-get >/dev/null && { SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo"; $SUDO apt-get install -y -qq python3-venv >/dev/null; python3 -m venv .venv; }; }
fi
.venv/bin/pip install -q --upgrade pip >/dev/null
say "faster-whisper などを入れます（数分）"
.venv/bin/pip install -q faster-whisper pillow numpy scipy

M=models/whisper-turbo
if [ ! -s "$M/model.bin" ]; then
  say "文字起こしモデル（約1.6GB）を取ってきます"
  .venv/bin/python tools/up.py model "$M" || {
    say "up.yoseca.com から取れなかったので Hugging Face から取ります"
    .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8', download_root='models/hf')" ; }
fi
say "できました。動画を編集する時は： bash tools/edit.sh <型の番号> <動画のURLかファイル>"
