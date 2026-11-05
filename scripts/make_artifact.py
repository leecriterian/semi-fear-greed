"""Claude 아티팩트용 페이지 만들기.

GitHub Actions가 만든 site/data.json을 template.html에 넣어 artifact.html을 만든다.
아티팩트는 문서 껍데기(doctype·head·body)를 스스로 붙이므로 본문만 쓴다.
"""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data = json.load(open(os.path.join(ROOT, 'site', 'data.json'), encoding='utf-8'))
tpl = open(os.path.join(ROOT, 'template.html'), encoding='utf-8').read()
payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
out = os.path.join(ROOT, 'artifact.html')
open(out, 'w', encoding='utf-8').write(tpl.replace('__DATA__', payload))
print(f"OK {out} · 미국 {data['asof']['us']} / 한국 {data['asof']['kr']} 종가 · GitHub 갱신 {data['updated']}")
