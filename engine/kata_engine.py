#!/usr/bin/env python3
# すぐ来る 動画編集キット 共通エンジン（CC BY-NC 4.0）
# 使い方: python3 engine/kata_engine.py <型フォルダ> [動画ファイル...] [--agent] [--model large-v3-turbo|small] [--no-plan] [--keep-all]
#   --agent : 見出し・札の計画を、このエンジンでは作らず「作業/計画の頼み.md」に書いて止まる（終了コード 3）。
#             AI（Claude Code / Codex）がそれを読んで 計画.json を書き、もう一度同じコマンドを走らせると書き出しまで進む。
#   動画を省くと <型フォルダ>/1_入れる/ の動画（名前順）を全部つなげて1本にする。
# 流れ: 1920x1080に揃える → 無音を切る(ジャンプカット) → 文字起こし(whisper) → 見出し・札・強調の計画(claude -p)
#       → 札・見出し・白箱・黒い札・画面分割をPNG/ASSで重ねて書き出し → 2_出来上がり/<名前>/完成.mp4
# 参考動画の分解（Gemini 3.8 flash・数値）は _共通/参考分析/kata_*.json。型ごとの数値は各フォルダの 型.json。
import os, sys, json, re, glob, subprocess, shutil, math, random, time
from concurrent.futures import ThreadPoolExecutor

FF = os.environ.get('KATA_FFMPEG') or ('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg' if os.path.exists('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg') else (shutil.which('ffmpeg') or 'ffmpeg'))   # libass 入りが要る
FP = os.environ.get('KATA_FFPROBE') or shutil.which('ffprobe') or 'ffprobe'
HERE = os.path.dirname(os.path.abspath(__file__))
FONT8 = f'{HERE}/fonts/NotoSansCJKjp-Black.otf'; FONT6 = f'{HERE}/fonts/NotoSansCJKjp-Bold.otf'   # SIL OFL 1.1（fonts/OFL.txt）
W, H, FPS = 1920, 1080, 30
VID_EXT = ('.mov', '.mp4', '.m4v', '.mkv', '.avi', '.webm')
IMG_EXT = ('.jpg', '.jpeg', '.png', '.heic', '.webp')


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f'失敗: {" ".join(str(c) for c in cmd[:6])}...\n{r.stderr[-2000:]}')
    return r


def probe(path):
    j = json.loads(run([FP, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path]).stdout)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    rot = 0
    for sd in v.get('side_data_list', []):
        if 'rotation' in sd: rot = int(sd['rotation'])
    w, h = int(v['width']), int(v['height'])
    if rot % 180: w, h = h, w
    return w, h, float(j['format']['duration'])


def dur(path):
    return float(run([FP, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path]).stdout.strip())


# ---------- 1. 1920x1080 30fps に揃えてつなぐ ----------
def normalize(src, out):
    w, h, _ = probe(src)
    other = (h > w) != (H > W)   # 画面と向きが違う（横の素材を縦の画面に等）→ ぼかし背景の上に中央
    if other:
        fit = f'scale=-2:{H}' if H > W and h > w else (f'scale={W}:-2' if H > W else f'scale=-2:{H}')
        vf = (f'split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5[bg];'
              f'[b]{fit}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p')
    else:
        vf = f'scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p'
    run([FF, '-v', 'error', '-y', '-i', src, '-filter_complex' if other else '-vf', vf,
         '-c:v', 'libx264', '-preset', 'fast', '-crf', '17', '-ac', '2', '-ar', '48000', '-c:a', 'aac', '-b:a', '192k', out])


def concat(files, out):
    if len(files) == 1:
        shutil.copy(files[0], out); return
    lst = out + '.txt'
    open(lst, 'w').write(''.join(f"file '{f}'\n" for f in files))
    run([FF, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', out])


# ---------- 2. 無音を切る ----------
def silence_segments(path, db, min_sil, pad):
    r = subprocess.run([FF, '-v', 'info', '-i', path, '-af', f'silencedetect=noise={db}dB:d={min_sil}', '-f', 'null', '-'],
                       capture_output=True, text=True)
    total = dur(path)
    sil = []
    st = None
    for ln in r.stderr.splitlines():
        m = re.search(r'silence_start: ([\d.]+)', ln)
        if m: st = float(m.group(1))
        m = re.search(r'silence_end: ([\d.]+)', ln)
        if m and st is not None: sil.append((st, float(m.group(1)))); st = None
    if st is not None: sil.append((st, total))
    keep, cur = [], 0.0
    for a, b in sil:
        a2, b2 = a + pad, b - pad
        if b2 - a2 <= 0: continue
        if a2 > cur: keep.append([cur, a2])
        cur = b2
    if total > cur: keep.append([cur, total])
    out = []
    for a, b in keep:   # 近すぎる切れ目はつなぐ・短すぎるカケラは捨てる
        if out and a - out[-1][1] < 0.15: out[-1][1] = b
        else: out.append([a, b])
    return [(a, b) for a, b in out if b - a >= 0.3]


def plan_zoom(K, plan, total, seams):
    """115% に寄る区間。白箱・強調の所で寄って 10〜15 秒で戻す。それ以外は interval 秒ごとに交互。始まり・終わりはカットの切れ目に合わせる"""
    zoom = K.get('zoom')
    if not zoom or len(zoom['levels']) < 2: return []
    lo, hi = zoom['interval']
    def snap(t):
        if not seams: return t
        return min(seams, key=lambda s: abs(s - t))
    marks = sorted(set(c['t'] for c in plan.get('keycaps', []) + plan.get('bigs', [])))
    win, t = [], snap(random.uniform(lo, hi))
    while t < total - 3:
        d = random.uniform(10, 15) if any(abs(m - t) < 2 for m in marks) else random.uniform(lo, hi)
        a, b = t, snap(min(total, t + d))
        if b - a >= 3: win.append((a, b))
        t = snap(b + random.uniform(lo, hi))
        if t <= b: t = b + lo
    for m in marks:   # 強調の所は必ず寄る
        a = snap(m)
        if not any(x <= a < y for x, y in win):
            b = snap(min(total, a + random.uniform(10, 15)))
            if b - a >= 3: win.append((a, b))
    win.sort()
    out = []
    for a, b in win:
        if out and a <= out[-1][1]: out[-1] = (out[-1][0], max(out[-1][1], b))
        else: out.append((a, b))
    return out


def cut_render(src, segs, work):
    def one(i):
        a, b = segs[i]
        o = f'{work}/seg_{i:04d}.mp4'
        run([FF, '-v', 'error', '-y', '-ss', f'{a:.3f}', '-to', f'{b:.3f}', '-i', src, '-vf', 'format=yuv420p',
             '-c:v', 'libx264', '-preset', 'fast', '-crf', '17', '-af', 'aresample=async=1:first_pts=0', '-c:a', 'aac', '-b:a', '192k', o])
        return o
    with ThreadPoolExecutor(4) as ex:
        files = list(ex.map(one, range(len(segs))))
    seams, acc = [], 0.0
    for f in files:
        acc += dur(f); seams.append(round(acc, 3))
    concat(files, f'{work}/cut.mp4')
    return f'{work}/cut.mp4', seams


# ---------- 画面録画の自動同期（OBS のマイク音と自撮りの音を波形で合わせる） ----------
def audio_env(path, sr=100, limit=600):
    """音の大きさの列（1秒 sr 個・先頭 limit 秒）"""
    import numpy as np
    r = subprocess.run([FF, '-v', 'error', '-t', str(limit), '-i', path, '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.int16).astype('float32')
    if len(a) < 8000: return None
    n = 8000 // sr; a = a[:len(a) // n * n].reshape(-1, n)
    e = np.sqrt((a * a).mean(axis=1)); e = e - e.mean()
    return e / (e.std() + 1e-6)

def sync_offset(cam, scr):
    """screen の何秒目が cam の 0 秒目か（= 画面録画の from に足す秒）。音が無ければ None"""
    import numpy as np
    from scipy.signal import correlate
    a, b = audio_env(cam), audio_env(scr)
    if a is None or b is None: return None
    c = correlate(b, a, mode='full', method='fft')          # 位置 k ↔ lag = k - (len(a) - 1)（lag>0: 画面録画が先に始まった）
    norm = np.array([min(len(a), len(b), len(a) + len(b) - 1 - k, k + 1) for k in range(len(c))], dtype='float32')
    c = c / np.maximum(norm, 100)                              # 短い重なりでの偶然の一致を抑える（1秒以上）
    k = int(np.argmax(c)); lag = k - (len(a) - 1)
    return lag / 100.0, float(c[k])


def cut_to_src(t, segs):
    """ジャンプカット後の秒 → 元の自撮りの秒"""
    acc = 0.0
    for a, b in segs:
        if t <= acc + (b - a): return a + (t - acc)
        acc += b - a
    return segs[-1][1] if segs else t


# ---------- 3. 文字起こし ----------
def transcribe(path, model_name, work):
    wav = f'{work}/cut16k.wav'
    run([FF, '-v', 'error', '-y', '-i', path, '-vn', '-ac', '1', '-ar', '16000', wav])
    try:
        from faster_whisper import WhisperModel      # CPU でも速い（クラウドの既定）
        local = os.environ.get('KATA_WHISPER_DIR')    # tools/up.py model で取ってきたモデルのフォルダ
        src = local if local and os.path.isdir(local) and model_name in ('large-v3-turbo', 'turbo') else model_name
        log(f'  faster-whisper（{src}・CPU int8）')
        m = WhisperModel(src, device='cpu', compute_type='int8', cpu_threads=os.cpu_count() or 4)
        import numpy as np, wave
        with wave.open(wav) as wf: pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0   # PyAV の版の違いに左右されないよう、音は自分で渡す
        segs, _ = m.transcribe(pcm, language='ja', word_timestamps=True, vad_filter=False, beam_size=5)
        words, text = [], ''
        for sg in segs:
            text += sg.text
            for w in (sg.words or []):
                words.append({'t': round(w.start, 2), 'e': round(w.end, 2), 'w': w.word})
        return words, text
    except ImportError:
        pass
    import whisper
    model = whisper.load_model(model_name)
    r = model.transcribe(wav, language='ja', word_timestamps=True, fp16=False, verbose=False)
    words = []
    for s in r['segments']:
        for w in s.get('words', []):
            words.append({'t': round(w['start'], 2), 'e': round(w['end'], 2), 'w': w['word']})
    return words, r['text']


def chunk_words(words, max_chars):
    """文節ごとの字幕にまとめる（参考: 1行 20〜28 字・1.5〜2.5 秒・ポン出し）"""
    chunks, cur = [], None
    END = '。！？!?'; PAUSE = '、,'
    for i, w in enumerate(words):
        txt = w['w'].strip()
        if not txt: continue
        if cur is None: cur = {'t': w['t'], 'e': w['e'], 'text': ''}
        cur['text'] += txt; cur['e'] = w['e']
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt['t'] - w['e']) if nxt else 9
        n = len(cur['text'])
        nn = len(nxt['w'].strip()) if nxt else 0
        PARTS = {'は', 'が', 'を', 'に', 'で', 'と', 'も', 'の', 'ね', 'よ', 'か', 'へ', 'から', 'まで', 'って', 'けど', 'ので', 'し', 'て', 'ば'}   # 助詞そのものの語の後ろで切る
        is_part = txt in PARTS or (len(txt) >= 2 and txt[-2:] in ('から', 'まで', 'って', 'けど', 'ので'))
        nxt_txt = nxt['w'].strip() if nxt else ''
        nxt_is_part = bool(nxt_txt) and (nxt_txt in PARTS or nxt_txt[-2:] in ('から', 'まで', 'って', 'けど', 'ので'))
        nxt_end0 = bool(nxt_txt) and re.fullmatch(r'(か|か[?？]|ね|よ|か[?？]?[。]?)', nxt_txt) is not None and (nxt['t'] - w['e']) <= 0.7
        sent_end = (txt[-1] in END or re.search(r'(ました|ます|です|でした|ません|でしょう|ください|ますね|ですね)$', txt) is not None) and not nxt_end0   # 文の終わり（次が「か」ならまだ）
        close = (nxt is None or gap > 0.7 or n + nn > max_chars or (sent_end and n >= 4) or (txt[-1] in PAUSE and n >= max_chars * 0.5)
                 or (is_part and n >= max_chars * 0.7) or (cur['e'] - cur['t'] > 3.0 and n >= 8 and (is_part or txt[-1] in END + PAUSE)))
        nxt_end = bool(nxt_txt) and re.fullmatch(r'(か|か[?？]|ね|よ|です|ます|でした|ました|か[?？]?[。]?)', nxt_txt) is not None
        if close and (nxt_is_part or nxt_end) and n + nn <= max_chars + 3 and not sent_end and gap <= 0.7: close = False   # 助詞・文末だけの字幕を作らない（「も避けて…」「か」）
        if close and nxt_txt and re.match(r'[\u4e00-\u9fff]$', txt) and re.match(r'^[\u3041-\u309f]', nxt_txt) and nn <= 3 and n + nn <= max_chars + 3 and not sent_end and gap <= 0.4: close = False   # 「同|じ」のような送り仮名の前で切らない
        if close:
            t = cur['text'].replace('、', ' ').replace(',', ' ')
            t = re.sub(r'[。！？!?]+$', '', t).strip()
            if t: chunks.append({'t': cur['t'], 'e': max(cur['e'], cur['t'] + 0.7), 'text': t})
            cur = None
    out = []   # 文の途中に「？。！」が残っていたら、そこで2つに割る（文字起こしが「か?地元」とくっつける事がある）
    for c in chunks:
        m = re.search(r'[。！？!?]', c['text'][:-1])
        if m and m.end() >= 2 and len(c['text']) - m.end() >= 2:
            r = m.end() / len(c['text']); mid = c['t'] + (c['e'] - c['t']) * r
            out.append({'t': c['t'], 'e': mid, 'text': re.sub(r'[。！？!?]+$', '', c['text'][:m.end()])}); out.append({'t': mid, 'e': c['e'], 'text': c['text'][m.end():]})
        else: out.append(c)
    for c in out: c['text'] = re.sub(r'^(次|つぎ|ツギ)(?![にのはをもへ])', '', c['text']).strip()   # 合図の「次」は字幕にしない
    chunks = [c for c in out if c['text']]
    for a, b in zip(chunks, chunks[1:]):   # 次の字幕まで隙間が小さければ出しっぱなし
        if b['t'] - a['e'] < 0.5: a['e'] = b['t']
        elif b['t'] < a['e']: a['e'] = b['t']
    return chunks


def srt_time(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{sec:02d},{ms:03d}'


def write_srt(chunks, path):
    open(path, 'w', encoding='utf-8').write(''.join(f'{i+1}\n{srt_time(c["t"])} --> {srt_time(c["e"])}\n{c["text"]}\n\n' for i, c in enumerate(chunks)))


def read_srt(path):
    txt = open(path, encoding='utf-8').read()
    out = []
    for blk in re.split(r'\n\s*\n', txt.strip()):
        ln = blk.strip().splitlines()
        if len(ln) < 3: continue
        m = re.match(r'(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)', ln[1])
        if not m: continue
        g = [int(x) for x in m.groups()]
        out.append({'t': g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, 'e': g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, 'text': ' '.join(ln[2:])})
    return out


# ---------- 4. 計画（見出し・札・白箱・強調・黒い札・画面分割） ----------
def plan_prompt(K, chunks, total, photos, screens):
    tr = '\n'.join(f'{int(c["t"])//60:02d}:{int(c["t"])%60:02d} {c["text"]}' for c in chunks)
    feats = []
    feats.append('"title": ["1行目","2行目"]  … 動画の結論を2行（各行14字以内）。冒頭に大きく出る')
    feats.append('"chips": [{"t":秒,"text":"…"}]  … 左上に出し続ける今の話題の札（16字以内）。45〜90秒に1つ。最初は t=' + str(K['title']['t'][1]) + ' 以降')
    if K.get('keycap'): feats.append('"keycaps": [{"t":秒,"dur":4,"lines":["1行目","2行目"]}]  … 結論・大事な一言を白い箱で2行（各行16字以内）。1〜2分に1つ')
    if K.get('big'): feats.append('"bigs": [{"t":秒,"text":"…"}]  … 感情が出た一言を画面中央に大きく（12字以内）。3〜5分に1つ')
    if K.get('slides'): feats.append('"slides": [{"t":秒,"dur":30,"heading":"見出し(14字以内)","items":["項目(18字以内)",…3〜4個],"item_t":[各項目を言い始める秒 …items と同じ数],"numbered":true/false}]  … 手順・箇条書き・理由を話している所に黒い札（左半分）。合計で動画の25%くらい（1〜2分に1つ・25〜40秒）')
    if K.get('split_screen') and screens: feats.append('"screens": [{"t":秒,"dur":秒,"file":"ファイル名","from":その録画の何秒目から見せるか}]  … 画面録画を右側に見せる区間。ファイル: ' + ', '.join(os.path.basename(s) for s in screens) + '。内容に合う所にだけ割り当てる（合わない物は使わない）')
    if photos: feats.append('"photos": [{"t":秒,"dur":4,"file":"ファイル名"}]  … 写真を全画面で4秒。ファイル: ' + ', '.join(os.path.basename(p) for p in photos))
    feats.append('"cut_out": [{"t0":秒,"t1":秒,"why":"NG/言い直し/撮影前の準備/撮影後の雑談/スタッフとの確認"}]  … 本編に要らない所を切る（その行の開始〜次の行の開始）。無ければ []。本編の中身は切らない')
    prompt = f"""あなたは日本のビジネス系YouTubeの編集者。次の文字起こし（分:秒 文）から、画面に出す文字の計画を JSON で作る。
動画の長さ {int(total)} 秒。型＝{K['name']}（{K.get('memo', '')[:200]}）。
決まり: 言っていない事は書かない。短く言い切る。敬語を外して体言止め。数字は半角。時刻は文字起こしの時刻を使う。
返すのは次の鍵を持つ JSON だけ（前後に文章を付けない）:
{chr(10).join('  ' + f for f in feats)}
  "summary": "この動画の一言（20字以内）"

文字起こし:
{tr}
"""
    return prompt


def make_plan(K, chunks, total, photos, screens, model):
    prompt = plan_prompt(K, chunks, total, photos, screens)
    try:
        r = subprocess.run(['claude', '-p', '--model', model, '--allowedTools', '', '--output-format', 'text'], input=prompt, capture_output=True, text=True, timeout=600)
        m = re.search(r'\{.*\}', r.stdout, re.S)
        plan = json.loads(m.group(0))
        plan['_by'] = model
    except Exception as e:
        log('計画を claude で作れなかったので簡易計画にします:', str(e)[:200])
        plan = {'_by': 'fallback', 'title': [chunks[0]['text'][:14]] if chunks else ['']}
        plan['chips'] = [{'t': c['t'], 'text': c['text'][:16]} for c in chunks if c['t'] >= K['title']['t'][1]][::max(1, len(chunks) // max(1, int(total // 60)))]
    return normalize_plan(K, plan, total, photos, screens)


def normalize_plan(K, plan, total, photos, screens):
    """AI が書いた計画を整える（長さ・時刻・ファイル名）"""
    def clampt(x): return max(0.0, min(float(x), total - 0.5))
    plan['title'] = [str(x)[:16] for x in plan.get('title', [])][:2]
    plan['chips'] = sorted([{'t': clampt(c['t']), 'text': str(c['text'])[:18]} for c in plan.get('chips', []) if c.get('text')], key=lambda c: c['t'])
    plan['keycaps'] = [{'t': clampt(c['t']), 'dur': float(c.get('dur', 4)), 'lines': [str(x)[:18] for x in c.get('lines', [])][:2]} for c in plan.get('keycaps', []) if c.get('lines')]
    plan['bigs'] = [{'t': clampt(c['t']), 'text': str(c['text'])[:14]} for c in plan.get('bigs', []) if c.get('text')]
    plan['slides'] = [{'t': clampt(c['t']), 'dur': float(c.get('dur', 25)), 'heading': str(c.get('heading', ''))[:16], 'items': [str(x)[:20] for x in c.get('items', [])][:5],
                       'item_t': [float(x) for x in c.get('item_t', []) if isinstance(x, (int, float))][:5], 'numbered': bool(c.get('numbered', True))} for c in plan.get('slides', []) if c.get('items')]
    byname = {os.path.basename(p): p for p in list(photos) + list(screens)}
    bn = lambda f: os.path.basename(str(f or ''))
    plan['screens'] = [dict({'t': clampt(c['t']), 'dur': float(c.get('dur', 30)), 'file': byname[bn(c['file'])], 'from': max(0.0, float(c.get('from', 0) or 0))}, **({'sync_done': True} if c.get('sync_done') else {})) for c in plan.get('screens', []) if bn(c.get('file')) in byname]
    plan['photos'] = [{'t': clampt(c['t']), 'dur': float(c.get('dur', 4)), 'file': byname[bn(c['file'])]} for c in plan.get('photos', []) if bn(c.get('file')) in byname]
    co = []
    if K.get('cut_out') is False: plan['cut_out'] = []      # 問い→答えの型は AI に切らせない（11/21 23〜56秒を「スタッフとの確認」と誤って切った）
    for c in plan.get('cut_out', []):
        try:
            a, b = clampt(c['t0']), min(float(c['t1']), total)
            if b - a >= 0.8: co.append({'t0': a, 't1': b, 'why': str(c.get('why', ''))[:30]})
        except Exception: pass
    plan['cut_out'] = sorted(co, key=lambda c: c['t0'])
    return plan


def apply_cutout(work, cut, plan, chunks, total):
    """計画の cut_out（NG・雑談）を実際に切り、字幕と計画の時刻をずらす。戻り値＝新しい長さ"""
    rem = []
    for c in plan.get('cut_out', []):
        a, b = c['t0'], c['t1']
        if rem and a <= rem[-1][1]: rem[-1][1] = max(rem[-1][1], b)
        else: rem.append([a, b])
    if not rem: return total
    keep, cur = [], 0.0
    for a, b in rem:
        if a - cur >= 0.5: keep.append((cur, a))
        cur = b
    if total - cur >= 0.5: keep.append((cur, total))
    log('要らない所を切る: ' + '・'.join(f'{a:.0f}〜{b:.0f}秒({c["why"]})' for (a, b), c in zip(rem, plan['cut_out'])))
    def remap(t):
        d = 0.0
        for a, b in rem:
            if t >= b: d += b - a
            elif t > a: return a - d
        return t - d
    def inside(t): return any(a < t < b for a, b in rem)
    tmp = f'{work}/cutout_src.mp4'; shutil.move(cut, tmp)
    _, seams = cut_render(tmp, keep, work)
    for f in glob.glob(f'{work}/seg_*.mp4'): os.remove(f)
    os.remove(tmp)
    new_total = dur(cut)
    chunks[:] = [{'t': remap(c['t']), 'e': remap(c['e']), 'text': c['text']} for c in chunks if not inside((c['t'] + c['e']) / 2)]
    chunks[:] = [c for c in chunks if c['e'] - c['t'] > 0.2]
    for k in ('chips', 'keycaps', 'bigs', 'slides', 'screens', 'photos'):
        for c in plan.get(k, []): c['t'] = round(min(remap(c['t']), max(0, new_total - 1)), 2)
    plan['_cut_out_done'] = plan['cut_out']; plan['cut_out'] = []
    cj = json.load(open(f'{work}/cuts.json')); cj['seams'] = seams; cj['cut_out'] = rem
    json.dump(cj, open(f'{work}/cuts.json', 'w'), ensure_ascii=False)
    return new_total


# ---------- 5. PNG の部品（PIL） ----------
def font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def rounded(draw, box, r, fill):
    draw.rounded_rectangle(box, radius=r, fill=fill)


def png_chip(text, K, path):
    """左上の話題の札。参考3本とも白い箱＋濃い文字（型1は少し斜めの箱・型2/3は角の小さい箱）。画像は (0,0) 起点で、重ねる時に x,y をずらす"""
    from PIL import Image, ImageDraw
    c = K['chip']; f = font(FONT8, c['size'])
    px, py = int(c['size'] * 0.5), int(c['size'] * 0.3)
    tw = int(f.getlength(text)); hgt = c['size'] + py * 2; wd = int(tw + px * 2)
    im = Image.new('RGBA', (wd + 40, hgt + 8), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    if c['style'] == 'dark':
        rounded(d, (0, 0, wd, hgt), 8, (0, 0, 0, 160)); d.text((px, py - 2), text, font=f, fill=(255, 255, 255, 255))
    elif c['style'] == 'skew':   # 型1: 平行四辺形の白い箱
        k = int(hgt * 0.18)
        d.polygon([(k, 0), (wd + k, 0), (wd, hgt), (0, hgt)], fill=(255, 255, 255, 235)); d.text((px + k // 2, py - 2), text, font=f, fill=(28, 34, 48, 255))
    else:
        rounded(d, (0, 0, wd, hgt), 6, (255, 255, 255, 240)); d.text((px, py - 2), text, font=f, fill=(20, 20, 20, 255))
    im.save(path)


def png_title(lines, K, path):
    from PIL import Image, ImageDraw, ImageFilter
    t = K['title']; f = font(FONT8, t['size'])
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    sh = Image.new('RGBA', (W, H), (0, 0, 0, 0)); ds = ImageDraw.Draw(sh); d = ImageDraw.Draw(im)
    lh = int(t['size'] * 1.18)
    for i, ln in enumerate(lines):
        if t['pos'] == 'topcenter':
            d.text((W / 2 + 0, t['y'] + i * lh), ln, font=f, fill=(255, 255, 255, 255), anchor='ma', stroke_width=6, stroke_fill=(0, 0, 0, 255))
        else:
            ds.text((t['x'] + 5, t['y'] + i * lh + 6), ln, font=f, fill=(0, 0, 0, 200))
            d.text((t['x'], t['y'] + i * lh), ln, font=f, fill=(255, 255, 255, 255))
    if t['pos'] != 'topcenter':
        sh = sh.filter(ImageFilter.GaussianBlur(6)); im = Image.alpha_composite(sh, im)
    im.save(path)


def png_nameplate(K, path):
    from PIL import Image, ImageDraw
    p = K['person']; np_ = K['nameplate']; ft = font(FONT6, np_.get('tag_size', 24)); fn = font(FONT8, np_.get('name_size', 44))
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    x, y = np_.get('x', 50), np_.get('y', 790)
    tw = d.textlength(p['tag'], font=ft)
    rounded(d, (x, y, x + tw + 28, y + 40), 6, (29, 112, 232, 255)); d.text((x + 14, y + 6), p['tag'], font=ft, fill=(255, 255, 255, 255))
    d.text((x, y + 48), p['name'], font=fn, fill=(255, 255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255))
    im.save(path)


def png_keycap(lines, K, path):
    from PIL import Image, ImageDraw
    f = font(FONT8, 42)
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    y0 = K['keycap_y'] - len(lines) * 62
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=f); x = W / 2 - tw / 2; y = y0 + i * 62
        rounded(d, (x - 18, y - 4, x + tw + 18, y + 56), 6, (255, 255, 255, 242)); d.text((x, y), ln, font=f, fill=(15, 15, 15, 255))
    im.save(path)


def png_big(text, path):
    from PIL import Image, ImageDraw
    f = font(FONT8, 76)
    tw = int(f.getlength(text)) + 160
    im = Image.new('RGBA', (tw, 160), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.text((80, 30), text, font=f, fill=(255, 255, 255, 255), stroke_width=8, stroke_fill=(0, 0, 0, 255))
    im = im.transform(im.size, Image.AFFINE, (1, 0.18, -14, 0, 1, 0), resample=Image.BICUBIC)   # 斜体
    out = Image.new('RGBA', (W, H), (0, 0, 0, 0)); out.paste(im, (int(W / 2 - tw / 2), int(H * 0.435)), im); out.save(path)


def png_slide(heading, items, numbered, path, n_all=None):
    """左半分 960x1080 の黒い札（参考: #1E1E1E・見出し44px・項目40px・番号は橙）。項目が増えても位置が動かないよう n_all で座標を決める"""
    from PIL import Image, ImageDraw
    im = Image.new('RGBA', (960, H), (30, 30, 30, 255)); d = ImageDraw.Draw(im)
    fh = font(FONT8, 46); fnum = font(FONT8, 40)
    def fit(txt, size, maxw):   # 右余白 70px を守る（本文幅 770px）。超えたら少しずつ小さく
        while size > 28 and font(FONT6, size).getlength(txt) > maxw: size -= 2
        return font(FONT6, size)
    n = n_all or len(items); top = H / 2 - (90 + n * 78) / 2
    d.text((70, top), heading, font=fit(heading, 46, 820) if fh.getlength(heading) > 820 else fh, fill=(255, 255, 255, 255))
    for i, it in enumerate(items):
        y = top + 100 + i * 78; fi = fit(it, 40, 770)
        if numbered: d.text((70, y - 2), str(i + 1), font=fnum, fill=(255, 170, 40, 255)); d.text((120, y), it, font=fi, fill=(240, 240, 240, 255))
        else: d.text((70, y), '✓', font=fnum, fill=(120, 200, 120, 255)); d.text((120, y), it, font=fi, fill=(240, 240, 240, 255))
    im.save(path)


# ---------- 5b. アプリ縦（トルトルの satsuei_build HEAD_SPEC["tate"] と同じ数値・CEO 11/21「完成はこんな感じに」） ----------
APP_TATE = {'icon': (126, 130, 202, 7), 'title': (79, 5, 567, 83, 630), 'pill': (166, 137, 720, 92, 45, 58, 252, 184), 'video_top': 285, 'video_h': 1350}
def png_app_head(K, dai, path):
    """左＝丸いアイコン（白い縁）・右上＝題（濃紺＋白縁・帯の中央）。帯（黄色の文字）は png_app_pill で別に重ねる"""
    from PIL import Image, ImageDraw
    sp = APP_TATE; cx, cy, dia, bw = sp['icon']; fs, bord, tx, ty, tw = sp['title']
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    ic = K.get('icon') or 'icon.png'; src = ic if os.path.isabs(ic) else next((c for c in (f'{K["_kdir"]}/{ic}', f'{HERE}/../my/{ic}') if os.path.exists(c)), '')   # 型フォルダか my/ の icon.png（自分の顔やロゴ・無ければ出さない）
    if os.path.exists(src):
        pic = Image.open(src).convert('RGBA'); w0, h0 = pic.size; m = min(w0, h0); pic = pic.crop(((w0 - m) // 2, (h0 - m) // 2, (w0 - m) // 2 + m, (h0 - m) // 2 + m)).resize((dia, dia), Image.LANCZOS)
        mask = Image.new('L', (dia, dia), 0); ImageDraw.Draw(mask).ellipse((0, 0, dia - 1, dia - 1), fill=255)
        d.ellipse((cx - dia / 2 - bw, cy - dia / 2 - bw, cx + dia / 2 + bw, cy + dia / 2 + bw), fill=(255, 255, 255, 255))
        im.paste(pic, (int(cx - dia / 2), int(cy - dia / 2)), mask)
    if dai:
        px, py, pw, ph = sp['pill'][:4]; band_cx = (sp['pill'][6] - 14 + px + pw) / 2.0
        size = fs
        while size > 30 and font(FONT8, size).getlength(dai) > min(tw, 2 * (W - band_cx - 24)): size -= 2
        f = font(FONT8, size); d.text((band_cx, ty), dai, font=f, fill=(25, 33, 76, 255), anchor='mm', stroke_width=bord, stroke_fill=(255, 255, 255, 255))
    im.save(path)
def png_app_pill(text, path):
    """黒い角丸の帯＋黄色の文字（役割・今の話題）。文字が長い時は帯を右へ広げる（画面幅の 0.6 まで）"""
    from PIL import Image, ImageDraw
    px, py, pw, ph, pr, pfs, ptx, pcy = APP_TATE['pill']
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    size = pfs; avail = px + pw - ptx - 16; maxw = int(W * 0.6 - ptx)
    while size > 30 and font(FONT8, size).getlength(text) > maxw: size -= 2
    f = font(FONT8, size); tw = f.getlength(text); pw2 = pw + max(0, int(tw) - avail)
    rounded(d, (px, py, px + pw2, py + ph), pr, (30, 30, 30, 235))
    d.text((ptx, pcy), text, font=f, fill=(216, 184, 48, 255), anchor='lm', stroke_width=2, stroke_fill=(10, 10, 48, 255))
    im.save(path)


# ---------- 6. ASS 字幕 ----------
def write_ass(chunks, K, path):
    s = K['sub']
    def col(hexrgb, a=0): return f'&H{a:02X}{hexrgb[5:7]}{hexrgb[3:5]}{hexrgb[1:3]}'
    def ts(x): h, r = divmod(x, 3600); m, sec = divmod(r, 60); return f'{int(h)}:{int(m):02d}:{sec:05.2f}'
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{s['font']},{s['size']},{col('#FFFFFF')},{col('#FFFFFF')},{col(s.get('outline_color', '#000000'), s.get('outline_alpha', 0))},{col('#000000', 0x60)},0,0,0,0,100,100,0,0,1,{s['outline']},{s.get('shadow', 0)},2,60,60,{s['bottom']},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pre = '{\\blur3}' if s.get('soft') else ''   # 型3: 縁を柔らかく（薄い影）
    ev = ''.join(f'Dialogue: 0,{ts(c["t"])},{ts(c["e"])},Sub,,0,0,0,,{pre}{c["text"]}\n' for c in chunks)
    open(path, 'w', encoding='utf-8').write(head + ev)


# ---------- 7. 最終書き出し ----------
def render(cut, K, plan, chunks, total, work, out_mp4):
    ov = []   # (png, x, y, t0, t1)
    ins = []
    def add(png, x, y, t0, t1): ov.append((png, x, y, max(0, t0), min(total, t1)))
    # 見出し（冒頭）
    app_tate = K.get('layout') == 'app_tate'
    if app_tate:
        dai = (K.get('head_title') or '').strip()
        if not dai:
            try: dai = (json.load(open(f'{K["_kdir"]}/原稿.json', encoding='utf-8')).get('dai') or '').strip()
            except Exception: dai = ''
        png_app_head(K, dai, f'{work}/app_head.png')      # 重ねるのは帯（chips）の後＝アイコンが帯の上に乗る（CEO 11/21）
    if plan.get('title') and any(plan['title']) and not app_tate:
        png_title(plan['title'], K, f'{work}/title.png'); add(f'{work}/title.png', 0, 0, K['title']['t'][0], K['title']['t'][1])
    if K.get('nameplate') and not app_tate:
        png_nameplate(K, f'{work}/name.png'); add(f'{work}/name.png', 0, 0, K['nameplate']['t'][0], K['nameplate']['t'][1])
    sfx_t = []
    # 黒い札（型2）：項目が1つずつ増える。札（左上）より先に重ねて、札が上に乗るようにする
    slide_windows = []
    for i, s in enumerate(plan.get('slides', [])):
        n = len(s['items']); step = s['dur'] / (n + 1)
        # 項目の出現時刻：計画に item_t（その項目を言う秒）があればそれに合わせ、無ければ等間隔
        it_t = s.get('item_t') or []
        starts = [s['t'] + (k - 1) * step for k in range(1, n + 1)]
        if len(it_t) == n and all(isinstance(x, (int, float)) for x in it_t):
            starts = [max(s['t'], min(float(x), s['t'] + s['dur'] - 1)) for x in it_t]; starts[0] = s['t']
            for k in range(1, n): starts[k] = max(starts[k], starts[k - 1] + 0.5)
        for k in range(1, n + 1):
            p = f'{work}/slide_{i:02d}_{k}.png'; png_slide(s['heading'], s['items'][:k], s['numbered'], p, n)
            t0 = starts[k - 1]; t1 = starts[k] if k < n else s['t'] + s['dur']
            add(p, 0, 0, t0, t1); sfx_t.append(t0)
        slide_windows.append((s['t'], s['t'] + s['dur']))
    chips = plan.get('chips', [])
    if app_tate:
        try: rows = [r for r in json.load(open(f'{K["_kdir"]}/原稿.json', encoding='utf-8')).get('rows', []) if (r[0] or '').strip()]
        except Exception: rows = []
        qc = []; drop = set()
        norm_ = lambda x: re.sub(r'[\s、。？?！!，,「」]', '', str(x)).replace('きょう', '今日')
        def grams(x): x = norm_(x); return {x[i:i + 3] for i in range(len(x) - 2)}
        pos = 0
        for r in rows:
            g = grams(r[0])
            if not g: continue
            need = max(2, (len(g) + 1) // 2)      # 問いの 3 文字組の半分以上が入っている所（順番どおりに前から探す）
            hits = [i for i, c in enumerate(chunks) if i >= pos and len(g & grams(c['text'])) >= need]
            if not hits:      # 2 つの字幕にまたがる時
                hits = [i for i in range(pos, len(chunks) - 1) if len(g & (grams(chunks[i]['text']) | grams(chunks[i + 1]['text']))) >= need]
            if not hits: continue
            pos = hits[0] + 1
            i0 = hits[0]
            while i0 + 1 in hits or (i0 + 1 < len(chunks) and chunks[i0 + 1]['t'] - chunks[i0]['e'] < 0.3 and len(g & grams(chunks[i0 + 1]['text'])) >= 2 and len(chunks[i0 + 1]['text']) <= 8 and i0 + 1 - hits[0] < 3): i0 += 1      # 続きの札（「ありましたか」）だけ。次の問いの字幕は食わない
            for i in range(hits[0], i0 + 1): drop.add(i)
            j = i0 + 1
            while j < len(chunks) and len(chunks[j]['text']) <= 2 and chunks[j]['t'] - chunks[j - 1]['e'] < 0.5: drop.add(j); j += 1      # 札の後ろの「か」
            pos = max(pos, j)
            q0 = re.sub(r'[？?！!。]+$', '', str(r[0]).strip())
            q0 = re.sub(r'^(きょうは|今日は|それで|では)[、，]?', '', q0)
            def pick(q):
                parts = [x for x in re.split(r'[、，]', q) if x]
                return parts[-1] if parts and 3 <= len(parts[-1]) <= 10 else (parts[0] if parts else q)
            def pick2(q):
                parts = [x for x in re.split(r'[、，]', q) if x]
                if len(parts) > 1 and re.match(r'^(あり|どう|どんな|なぜ|なに|何が)', parts[-1]) and len(parts[0]) >= 3: return parts[0]      # 「〜は、ありましたか」→ 前半
                return pick(q)
            lab = pick2(re.sub(r'(でしたか|ですか|ますか|でしょうか)$', '', q0))
            if len(lab) < 4: lab = pick2(re.sub(r'か$', '', q0))
            if len(lab) > 10: lab = lab[:10]
            qc.append({'t': chunks[hits[0]]['t'], 'text': lab or q[:10]})
        if qc:
            qc.sort(key=lambda c: c['t']); qc[0]['t'] = 0.0; chips = qc      # 帯は問いごとに変わる（アストラ 11/21）
            chunks = [c for i, c in enumerate(chunks) if i not in drop]      # 問いの札の間は字幕を出さない（札に文が出ている）
            log('問いの帯 ' + ' / '.join(f"{c['t']:.0f}s {c['text']}" for c in qc))
    for i, c in enumerate(chips):
        t1 = chips[i + 1]['t'] if i + 1 < len(chips) else total
        p = f'{work}/chip_{i:02d}.png'
        if app_tate: png_app_pill(c['text'], p); add(p, 0, 0, c['t'], t1)
        else: png_chip(c['text'], K, p); add(p, K['chip']['x'], K['chip']['y'], c['t'], t1)
        sfx_t.append(c['t'])
    if app_tate: add(f'{work}/app_head.png', 0, 0, 0, total)
    # ズーム（100→115%）：強調の所で寄り、カットの切れ目で戻す。寄りが無い所は interval 秒ごとに交互
    zoom_windows = plan_zoom(K, plan, total, json.load(open(f'{work}/cuts.json'))['seams'] if os.path.exists(f'{work}/cuts.json') else [])
    for i, c in enumerate([] if app_tate else plan.get('keycaps', [])):
        p = f'{work}/key_{i:02d}.png'; png_keycap(c['lines'], K, p); add(p, 0, 0, c['t'], c['t'] + c['dur']); sfx_t.append(c['t'])
    for i, c in enumerate([] if app_tate else plan.get('bigs', [])):
        p = f'{work}/big_{i:02d}.png'; png_big(c['text'], p); add(p, 0, 0, c['t'], c['t'] + 2.8)
    screens = plan.get('screens', [])
    photos = plan.get('photos', [])

    args = [FF, '-v', 'error', '-y', '-i', cut]
    fc = []; vi = 1
    cur = '[0:v]'
    g = K.get('grade')   # 色：参考は明るく均一（蛍光灯の沈みを少し持ち上げる）
    fc.append(f'{cur}' + (f'eq=contrast={g.get("contrast", 1.0)}:brightness={g.get("brightness", 0.0)}:saturation={g.get("saturation", 1.0)}:gamma={g.get("gamma", 1.0)},' if g else '') + 'format=yuv420p[v0]'); cur = '[v0]'
    if zoom_windows:
        z = K['zoom']['levels'][-1] / 100
        en = '+'.join(f'between(t,{a:.2f},{b:.2f})' for a, b in zoom_windows)
        fc.append(f'{cur}split=2[za][zb];[zb]scale=iw*{z}:ih*{z},crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.4[zz];[za][zz]overlay=0:0:enable=\'{en}\'[vz]'); cur = '[vz]'
    if app_tate:
        vt, vh = APP_TATE['video_top'], APP_TATE['video_h']
        fc.append(f'{cur}crop={W}:{vh}:0:(ih-{vh})*0.6,pad={W}:{H}:0:{vt}:black[vk]'); cur = '[vk]'      # 顔は上寄りなので 35% の所から 4:5 を切る
    # 2分割（型2：左に札・右に人物 / 型3：左に人物・右に画面録画）
    if slide_windows:
        en = '+'.join(f'between(t,{a:.2f},{b:.2f})' for a, b in slide_windows)
        fc.append(f'{cur}split=2[sa][sb];[sb]crop=960:1080:480:0,pad=1920:1080:960:0:color=#1E1E1E[lay];[sa][lay]overlay=0:0:enable=\'{en}\'[v1]'); cur = '[v1]'
    if screens:
        sp = K['split']; pw, sw = sp['person_w'], W - sp['person_w']
        fc.append(f'{cur}split=2[pa][pb];[pb]crop={pw}:1080:{(W - pw) // 2}:0,pad=1920:1080:0:0:color=#111111[base]')
        prev = '[base]'
        for i, s in enumerate(screens):
            args += ['-i', s['file']]
            fc.append(f'[{vi}:v]trim=start={s.get("from", 0):.2f},setpts=PTS-STARTPTS,scale={sw}:1080:force_original_aspect_ratio=decrease,pad={sw}:1080:(ow-iw)/2:(oh-ih)/2:color=#111111,setpts=PTS+{s["t"]:.3f}/TB[sc{i}]')
            fc.append(f'{prev}[sc{i}]overlay={pw}:0:eof_action=pass:enable=\'between(t,{s["t"]:.2f},{s["t"] + s["dur"]:.2f})\'[b{i}]'); prev = f'[b{i}]'; vi += 1
        en = '+'.join(f'between(t,{s["t"]:.2f},{s["t"] + s["dur"]:.2f})' for s in screens)
        fc.append(f'[pa]{prev}overlay=0:0:enable=\'{en}\'[v2]'); cur = '[v2]'
    # 写真（全画面）
    for i, p in enumerate(photos):
        args += ['-loop', '1', '-framerate', '30', '-t', f'{p["dur"]:.2f}', '-i', p['file']]
        fc.append(f'[{vi}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setpts=PTS-STARTPTS+{p["t"]:.3f}/TB[ph{i}]')
        fc.append(f'{cur}[ph{i}]overlay=0:0:eof_action=pass:enable=\'between(t,{p["t"]:.2f},{p["t"] + p["dur"]:.2f})\'[vp{i}]'); cur = f'[vp{i}]'; vi += 1
    # PNG を重ねる
    for i, (png, x, y, t0, t1) in enumerate(ov):
        args += ['-loop', '1', '-framerate', '30', '-t', f'{total:.2f}', '-i', png]
        fc.append(f'{cur}[{vi}:v]overlay={x}:{y}:eof_action=pass:enable=\'between(t,{t0:.2f},{t1:.2f})\'[o{i}]'); cur = f'[o{i}]'; vi += 1
    # 字幕（ASS）
    write_ass(chunks, K, f'{work}/sub.ass')
    fc.append(f"{cur}ass='{work}/sub.ass':fontsdir='{HERE}/fonts'[vout]")
    # 音：声を整える → BGM → 効果音
    fc.append('[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[voice]')
    amix = ['[voice]']; ai = vi
    bgm_file = None
    if K.get('bgm'):
        for cand in (f'{K["_kdir"]}/{K["bgm"]["file"]}', f'{HERE}/../my/{K["bgm"]["file"]}', f'{HERE}/bgm/{K["bgm"]["file"]}'):
            if os.path.exists(cand): bgm_file = cand; break
        if not bgm_file: log(f'BGM（{K["bgm"]["file"]}）が無いので、BGMなしで作ります。入れたい時は my/ に自分が権利を持つ mp3 を置いてください')
    if bgm_file:
        args += ['-stream_loop', '-1', '-i', bgm_file]
        fc.append(f'[{ai}:a]atrim=0:{total:.2f},loudnorm=I=-16:TP=-1.5,volume={K["bgm"]["db"]}dB,afade=t=out:st={max(0, total - 2):.2f}:d=2[bgm]'); amix.append('[bgm]'); ai += 1
    if K.get('sfx') and sfx_t:
        for t in sfx_t:
            args += ['-i', f'{HERE}/sfx/pop.wav']
            fc.append(f'[{ai}:a]adelay={int(t * 1000)}|{int(t * 1000)},volume=0.35[sf{ai}]'); amix.append(f'[sf{ai}]'); ai += 1
    if len(amix) > 1:
        fc.append(f'{"".join(amix)}amix=inputs={len(amix)}:duration=first:normalize=0[aout]')
    else:
        fc.append('[voice]anull[aout]')
    args += ['-filter_complex', ';'.join(fc), '-map', '[vout]', '-map', '[aout]', '-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-t', f'{total:.2f}', out_mp4]
    open(f'{work}/ffmpeg_cmd.txt', 'w').write(' '.join(f'"{a}"' if ' ' in str(a) else str(a) for a in args))
    run(args)


# ---------- 本体 ----------
def main():
    a, kv, i = [], {}, 1
    while i < len(sys.argv):
        x = sys.argv[i]
        if x.startswith('--'):
            if x[2:] in ('model',) and i + 1 < len(sys.argv): kv[x[2:]] = sys.argv[i + 1]; i += 2; continue
            kv[x[2:]] = '1'
        else: a.append(x)
        i += 1
    kdir = os.path.abspath(a[0]); K = json.load(open(f'{kdir}/型.json', encoding='utf-8'))
    global W, H
    K['_kdir'] = kdir
    if K.get('canvas') == 'tate': W, H = 1080, 1920   # 縦（9:16）だけで仕上げる型（撮るだけ1・CEO 11/21「縦で統一」）
    inbox = f'{kdir}/1_入れる'
    vids = [os.path.abspath(x) for x in a[1:]] or sorted(f for f in glob.glob(f'{inbox}/*') if f.lower().endswith(VID_EXT) and not os.path.basename(f).startswith('.'))
    if not vids: raise SystemExit(f'動画が見つかりません: {inbox}/ に入れてください')
    photos = sorted(f for f in glob.glob(f'{inbox}/写真/*') if f.lower().endswith(IMG_EXT) and not os.path.basename(f).startswith('.'))
    screens = sorted(f for f in glob.glob(f'{inbox}/画面録画/*') if f.lower().endswith(VID_EXT) and not os.path.basename(f).startswith('.'))
    name = os.path.splitext(os.path.basename(vids[0]))[0]
    outdir = f'{kdir}/2_出来上がり/{name}'; work = f'{outdir}/作業'; os.makedirs(work, exist_ok=True)
    log(f'型: {K["name"]} / 動画 {len(vids)} 本 / 写真 {len(photos)} / 画面録画 {len(screens)} → {outdir}')

    cut = f'{work}/cut.mp4'
    stamp = json.dumps({'v': 2, 'in': [os.path.basename(v) + str(os.path.getsize(v)) for v in vids], 'silence': K['silence']}, ensure_ascii=False)
    if os.path.exists(cut) and os.path.exists(f'{work}/cuts.json') and json.load(open(f'{work}/cuts.json')).get('stamp') != stamp:
        log('入れた動画か無音の設定が変わったので、カットから作り直します')
        for f in (cut, f'{work}/transcript.json', f'{work}/cuts.json'):
            if os.path.exists(f): os.remove(f)
    if not os.path.exists(cut):
        norm = []
        for i, v in enumerate(vids):
            log(f'揃える {i+1}/{len(vids)}: {os.path.basename(v)}'); o = f'{work}/src_{i:02d}.mp4'; normalize(v, o); norm.append(o)
        concat(norm, f'{work}/src.mp4')
        # 声はピンマイク（画面収録に入った音）を使う：型.json の split.audio が "screen" なら、画面録画の音を自撮りに合わせてずらして差し替える（CEO 11/21）
        if screens and K.get('split', {}).get('audio') == 'screen':
            sc = screens[0]; r = sync_offset(f'{work}/src.mp4', sc)
            if r is None: log('画面録画に音が無いので、自撮りの音のまま使います（⌘⇧5 のオプションでマイクを選んでください）')
            else:
                off = r[0]; log(f'声は画面録画（ピンマイク）の音にします：{os.path.basename(sc)} は自撮りより {off:+.2f} 秒ずれ')
                src_total0 = dur(f'{work}/src.mp4'); tmp = f'{work}/src_mic.mp4'
                af = f'atrim=start={off:.3f},asetpts=PTS-STARTPTS' if off >= 0 else f'adelay={int(-off*1000)}|{int(-off*1000)}'
                run([FF, '-v', 'error', '-y', '-i', f'{work}/src.mp4', '-i', sc, '-filter_complex', f'[1:a]{af},apad[a]', '-map', '0:v', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', f'{src_total0:.3f}', tmp])
                os.replace(tmp, f'{work}/src.mp4')
        s = K['silence']; src_total = dur(f'{work}/src.mp4')
        # 参考のカット間隔（平均 3.5〜5 秒）に近づける：細かすぎたら無音の長さの基準を上げる
        m = s['min']
        if 'keep-all' in kv: s = dict(s, db=-60, min=5.0, pad=1.0); m = 5.0      # --keep-all：無音をほぼ切らない（短い素材・CEO 11/21「4秒なら4秒で出して」）
        while True:
            segs = silence_segments(f'{work}/src.mp4', s['db'], m, s['pad'])
            kept = sum(b - a for a, b in segs)
            if kept / max(1, len(segs)) >= s.get('target_avg', 3.5) or m >= 1.0 or len(segs) <= 1: break
            m = round(m + 0.1, 2)
        log(f'無音を切る（{m}秒以上）: {src_total:.0f}秒 → {kept:.0f}秒（カット {len(segs)} 個・平均 {kept / max(1, len(segs)):.1f}秒）')
        _, seams = cut_render(f'{work}/src.mp4', segs, work)
        json.dump({'segments': segs, 'seams': seams, 'stamp': stamp}, open(f'{work}/cuts.json', 'w'), ensure_ascii=False)
        for f in glob.glob(f'{work}/seg_*.mp4'): os.remove(f)
    total = dur(cut)

    srt = f'{outdir}/字幕.srt'; trj = f'{work}/transcript.json'
    if os.path.exists(srt) and os.path.exists(trj) and os.path.getmtime(srt) > os.path.getmtime(trj) + 1:
        log('字幕.srt が直されているので、それを使います'); chunks = read_srt(srt)
    elif os.path.exists(trj):
        tj = json.load(open(trj, encoding='utf-8')); chunks = tj['chunks']
        if tj.get('words') and tj.get('sub_stamp') != json.dumps(K['sub'], sort_keys=True):   # 字幕の設定が変わったら words から切り直す
            log('字幕の設定が変わったので切り直します'); chunks = chunk_words(tj['words'], K['sub']['max_chars'])
            json.dump({'chunks': chunks, 'words': tj['words'], 'sub_stamp': json.dumps(K['sub'], sort_keys=True)}, open(trj, 'w', encoding='utf-8'), ensure_ascii=False)
            write_srt(chunks, srt)
    else:
        log(f'文字起こし（whisper {kv.get("model", K.get("whisper_model", "large-v3-turbo"))}）…')
        words, text = transcribe(cut, kv.get('model', K.get('whisper_model', 'large-v3-turbo')), work)
        chunks = chunk_words(words, K['sub']['max_chars'])
        json.dump({'chunks': chunks, 'words': words, 'sub_stamp': json.dumps(K['sub'], sort_keys=True)}, open(trj, 'w', encoding='utf-8'), ensure_ascii=False)
        open(f'{outdir}/文字起こし.txt', 'w', encoding='utf-8').write(text)
        write_srt(chunks, srt)
        log(f'字幕 {len(chunks)} 個（平均 {sum(len(c["text"]) for c in chunks) / max(1, len(chunks)):.1f} 字・{sum(c["e"] - c["t"] for c in chunks) / max(1, len(chunks)):.1f} 秒）')

    pj = f'{outdir}/計画.json'
    # 画面録画の自動同期：型.json の split.sync が true なら、画面録画は自撮りと同時に回した物とみなし、音で合わせて from を自動で決める
    if screens and K.get('split', {}).get('sync') and os.path.exists(f'{work}/src.mp4') and os.path.exists(f'{work}/cuts.json'):
        segs = json.load(open(f'{work}/cuts.json'))['segments']; syncs = {}
        for sc in screens:
            r = sync_offset(f'{work}/src.mp4', sc)
            if r: syncs[sc] = r[0]; log(f'同期: {os.path.basename(sc)} は自撮りより {r[0]:+.2f} 秒ずれ（音で合わせた）')
        json.dump({os.path.basename(k): v for k, v in syncs.items()}, open(f'{work}/sync.json', 'w'), ensure_ascii=False)
    if os.path.exists(pj):
        log('計画.json があるので、それを使います'); plan = json.load(open(pj, encoding='utf-8'))
        if not plan.get('_normalized'):      # AI が書いたばかりの計画：整えて、要らない所（cut_out）を切る
            by = plan.get('_by', 'agent'); plan = normalize_plan(K, plan, total, photos, screens); plan['_by'] = by; plan['_normalized'] = True
            if plan.get('cut_out'):
                total = apply_cutout(work, cut, plan, chunks, total)
                json.dump({'chunks': chunks, 'words': []}, open(trj, 'w', encoding='utf-8'), ensure_ascii=False)
                write_srt(chunks, srt)
                log(f'切った後: {total:.0f}秒・字幕 {len(chunks)} 個')
            json.dump(plan, open(pj, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        for k in ('screens', 'photos'):
            for c in plan.get(k, []):
                if not os.path.isabs(c['file']): c['file'] = f'{inbox}/{"画面録画" if k == "screens" else "写真"}/{c["file"]}'
    elif 'agent' in kv and 'no-plan' not in kv:
        ask = f'{work}/計画の頼み.md'
        open(ask, 'w', encoding='utf-8').write(plan_prompt(K, chunks, total, photos, screens)
            + f'\n\n---\n書いたら、上の JSON を {pj} に保存して、同じコマンドをもう一度走らせてください。\n')
        log(f'計画は AI が書きます：{ask} を読んで、JSON を {pj} に保存してから、同じコマンドをもう一度走らせてください')
        print(f'PLAN_NEEDED {ask} {pj}')
        sys.exit(3)
    else:
        log(f'計画（見出し・札・強調）を {K.get("plan_model", "claude-sonnet-5-5")} で…')
        plan = make_plan(K, chunks, total, photos, screens, K.get('plan_model', 'claude-sonnet-5-5')) if 'no-plan' not in kv else {'title': [], 'chips': []}
        if plan.get('cut_out'):
            total = apply_cutout(work, cut, plan, chunks, total)
            json.dump({'chunks': chunks, 'words': []}, open(trj, 'w', encoding='utf-8'), ensure_ascii=False)
            write_srt(chunks, srt)
            log(f'切った後: {total:.0f}秒・字幕 {len(chunks)} 個')
        plan['_normalized'] = True
        json.dump(plan, open(pj, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if screens and K.get('split', {}).get('sync') and os.path.exists(f'{work}/sync.json'):
        sy = json.load(open(f'{work}/sync.json')); segs = json.load(open(f'{work}/cuts.json'))['segments']
        for c in plan.get('screens', []):
            if c.get('sync_done'): continue
            b = os.path.basename(c['file'])
            if b in sy:
                c['from'] = max(0.0, cut_to_src(c['t'], segs) + sy[b]); c['sync_done'] = True
        json.dump(plan, open(pj, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if screens and K.get('split', {}).get('always') and not plan.get('screens'):      # 11/21 CEO：画面録画を入れたら必ず右に出す（AI が「合わない」として外しても）
        plan['screens'] = [{'t': 0.0, 'dur': float(total), 'file': screens[0], 'from': 0.0}]
        if os.path.exists(f'{work}/sync.json'):
            sy = json.load(open(f'{work}/sync.json')); b = os.path.basename(screens[0]); segs = json.load(open(f'{work}/cuts.json'))['segments']
            if b in sy: plan['screens'][0]['from'] = max(0.0, cut_to_src(0.0, segs) + sy[b]); plan['screens'][0]['sync_done'] = True
        json.dump(plan, open(pj, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log(f'見出し {plan.get("title")} / 札 {len(plan.get("chips", []))} / 白箱 {len(plan.get("keycaps", []))} / 強調 {len(plan.get("bigs", []))} / 黒い札 {len(plan.get("slides", []))} / 画面 {len(plan.get("screens", []))} / 写真 {len(plan.get("photos", []))}')

    out = f'{outdir}/完成.mp4'
    log('書き出し…'); render(cut, K, plan, chunks, total, work, out)
    run([FF, '-v', 'error', '-y', '-i', out, '-vf', ('scale=720:1280' if H > W else 'scale=1280:720'), '-c:v', 'libx264', '-crf', '26', '-c:a', 'aac', '-b:a', '128k', f'{outdir}/完成_スマホ用.mp4'])
    run([FF, '-v', 'error', '-y', '-ss', '3', '-i', out, '-frames:v', '1', f'{outdir}/サムネ候補.jpg'])
    log(f'できました: {out}（{total:.0f}秒）')
    print(out)


if __name__ == '__main__':
    main()
