#!/usr/bin/env python3
"""動画の受け渡し（up.yoseca.com）。標準ライブラリだけで動く。
  python3 tools/up.py get <動画のURL> <保存先>                      … スマホから上げた動画を取ってくる（途中から再開できる）
  python3 tools/up.py put <ファイル>... --id <id> --token <token>    … できあがりを返す（分割して上げる）
  python3 tools/up.py model <保存先フォルダ>                         … 文字起こしモデルを取ってくる（Hugging Face に届かない時）
"""
import json, os, sys, time, urllib.request, urllib.parse

BASE = os.environ.get('SUGUKURU_UP', 'https://up.yoseca.com')
UA = {'user-agent': 'sugukuru-edit/1.0'}


def req(method, url, data=None, headers=None, timeout=600):
    h = dict(UA, **(headers or {}))
    for k in range(5):
        try:
            r = urllib.request.Request(url, data=data, method=method, headers=h)
            with urllib.request.urlopen(r, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')
            if e.code < 500: raise SystemExit(f'失敗 {e.code}: {body[:300]}')
            err = body
        except Exception as e:
            err = str(e)
        time.sleep(3 * (k + 1))
    raise SystemExit(f'失敗（5回やり直した）: {err[:300]}')


def get(url, dest):
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or '.', exist_ok=True)
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    h = dict(UA)
    if have: h['range'] = f'bytes={have}-'
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=600)
    if have and r.status != 206: have = 0
    total = have + int(r.headers.get('content-length') or 0)
    with open(dest, 'ab' if have else 'wb') as f:
        got, t0 = have, time.time()
        while True:
            b = r.read(4 << 20)
            if not b: break
            f.write(b); got += len(b)
            if time.time() - t0 > 5: print(f'  取得 {got >> 20}/{total >> 20}MB', flush=True); t0 = time.time()
    print(f'取れました: {dest}（{os.path.getsize(dest) >> 20}MB）')


def put_one(path, ident, token, kind='out', extra=None):
    size = os.path.getsize(path)
    ctype = 'video/mp4' if path.lower().endswith('.mp4') else ('image/jpeg' if path.lower().endswith('.jpg') else 'text/plain; charset=utf-8')
    body = dict(kind=kind, id=ident, token=token, name=os.path.basename(path), size=size, type=ctype, **(extra or {}))
    hdr = {'content-type': 'application/json'}
    if kind == 'model': hdr['x-admin'] = os.environ['ADMIN_KEY']
    n = json.loads(req('POST', f'{BASE}/api/new', json.dumps(body).encode(), hdr))
    P, parts = n['partSize'], []
    with open(path, 'rb') as f:
        i = 0
        while True:
            chunk = f.read(P)
            if not chunk: break
            i += 1
            q = urllib.parse.urlencode({'key': n['key'], 'uploadId': n['uploadId'], 'n': i})
            r = json.loads(req('PUT', f'{BASE}/api/part?{q}', chunk, {'content-type': 'application/octet-stream'}))
            parts.append({'partNumber': r['partNumber'], 'etag': r['etag']})
            print(f'  {os.path.basename(path)} {min(size, i * P) >> 20}/{size >> 20}MB', flush=True)
    d = json.loads(req('POST', f'{BASE}/api/done', json.dumps({'key': n['key'], 'uploadId': n['uploadId'], 'parts': parts}).encode(), {'content-type': 'application/json'}))
    print('上げました:', d['url'])
    return d['url']


def main():
    a = sys.argv[1:]
    if not a: print(__doc__); return
    if a[0] == 'get' and len(a) >= 3: get(a[1], a[2]); return
    if a[0] == 'put':
        files, kv, i = [], {}, 1
        while i < len(a):
            if a[i].startswith('--'): kv[a[i][2:]] = a[i + 1]; i += 2
            else: files.append(a[i]); i += 1
        if not files or 'id' not in kv or 'token' not in kv: raise SystemExit('使い方: put <ファイル>... --id <id> --token <token>')
        for f in files: put_one(f, kv['id'], kv['token'])
        print(f'\nできあがりのページ: {BASE}/r/{kv["id"]}')
        return
    if a[0] == 'model' and len(a) >= 2:
        dest = a[1]; os.makedirs(dest, exist_ok=True)
        idx = json.loads(req('GET', f'{BASE}/f/m/whisper-turbo/files.json'))
        for name in idx:
            p = os.path.join(dest, name)
            if os.path.exists(p) and os.path.getsize(p) == idx[name]: continue
            get(f'{BASE}/f/m/whisper-turbo/{name}', p)
        print('モデルを取れました:', dest); return
    if a[0] == 'model-put':      # 管理者だけ（ADMIN_KEY が要る）
        src = a[1]; idx = {}
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p) and not name.startswith('.'):
                idx[name] = os.path.getsize(p); put_one(p, '', '', 'model', {'path': f'whisper-turbo/{name}'})
        open(os.path.join(os.environ.get('TMPDIR', '/tmp'), 'files.json'), 'w').write(json.dumps(idx))
        put_one(os.path.join(os.environ.get('TMPDIR', '/tmp'), 'files.json'), '', '', 'model', {'path': 'whisper-turbo/files.json'})
        return
    print(__doc__)


if __name__ == '__main__':
    main()
