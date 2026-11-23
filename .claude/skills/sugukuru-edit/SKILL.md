---
name: sugukuru-edit
description: スマホで撮った動画を、型（1 縦で答える／2 対談／3 時刻ごと／4 年表・自己紹介／5 黒い札で手順／6 画面を見せる）に合わせて、ジャンプカット・ズーム・字幕・話題の札・見出し・白い箱・黒い札まで自動で編集し、up.yoseca.com へ返す。「すぐ来る編集キットで」「この動画を型Nで編集して」「up.yoseca.com の動画」と言われたら使う。
---

# すぐ来る 動画編集キット

手順の正本はリポジトリ直下の `CLAUDE.md`。それを読んで、その順（準備 → 名前の札 → 編集を走らせる → 計画を書く → 返す）で進める。

要点：
- `bash setup.sh`（最初だけ）→ `bash tools/edit.sh <番号> "<動画URL>" > edit.log 2>&1` をバックグラウンドで。
- 終了コード 3 ＝ あなたが `作業/計画の頼み.md` を読んで `計画.json` を書く番。書いたら同じコマンドをもう一度。
- できたら `.venv/bin/python tools/up.py put ... --id <id> --token <token>` で返し、`https://up.yoseca.com/r/<id>` を渡す。
- `up.yoseca.com` に届かない時は「環境のネットワーク設定で許可するドメインに up.yoseca.com を足す」と案内して止まる。
