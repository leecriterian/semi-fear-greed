"""반도체 공포탐욕지수 계산기 (GitHub Actions에서 매일 실행).

데이터를 받아 지표별 점수와 지수를 계산하고 site/index.html, site/data.json을 만든다.
- 가격·거래량: Yahoo Finance
- 미국 구성종목: iShares SOXX 보유종목(실패 시 아래 기본 목록)
- 외국인 순매수: Daum 금융(실패 시 네이버 금융)
점수 = 최근 252거래일 중 오늘 값의 백분위. 변동성은 100에서 뺀다. 지수 = 지표 점수 평균.
"""
import csv, datetime, io, json, math, os, re, sys, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'site')
KST = datetime.timezone(datetime.timedelta(hours=9))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
W = 252
US_TZ = ZoneInfo('America/New_York')
KR_SYMS = {'005930.KS', '000660.KS', '^KS11'}
# Yahoo가 분할을 과거 가격에 늦게 반영할 때를 대비한 목록(날짜, 비율). 이미 반영됐으면 건드리지 않는다.
KNOWN_SPLITS = {'SOXX': [('2026-11-05', 3.0)]}
DEFAULT_HOLDINGS = ['AMD', 'NVDA', 'AVGO', 'MU', 'INTC', 'TXN', 'ADI', 'QCOM', 'NXPI', 'KLAC', 'MPWR', 'AMAT', 'MRVL', 'LRCX', 'TSM',
                    'SKHY', 'TER', 'MCHP', 'ALAB', 'ON', 'CRDO', 'ASML', 'ENTG', 'TSEM', 'MTSI', 'ASX', 'CBRS', 'UMC', 'STM', 'ARM']


def log(*a):
    print(*a, flush=True)


def get(url, headers=None, tries=4):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    h.update(headers or {})
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read()
        except Exception as e:
            log(f'  재시도 {t + 1}/{tries}: {url[:90]} → {e}')
            time.sleep(3 * (t + 1))
    raise RuntimeError(f'받기 실패: {url}')


# ---------- 데이터 받기 ----------
def yahoo(sym):
    q = urllib.parse.quote(sym)
    last = None
    for host in ('query1', 'query2'):
        try:
            j = json.loads(get(f'https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=5y&interval=1d&events=split', tries=3))
            res = j['chart']['result'][0]
            off = res['meta']['gmtoffset']
            qt = res['indicators']['quote'][0]
            adj = res['indicators'].get('adjclose', [{}])[0].get('adjclose') or qt['close']
            m = {}
            for i, t in enumerate(res.get('timestamp') or []):
                if adj[i] is None or qt['close'][i] is None:
                    continue
                d = datetime.datetime.fromtimestamp(t + off, datetime.timezone.utc).date().isoformat()
                m[d] = {'c': adj[i], 'raw': qt['close'][i], 'v': qt['volume'][i] or 0}
            splits = list(KNOWN_SPLITS.get(sym, []))
            for ev in ((res.get('events') or {}).get('splits') or {}).values():
                d = datetime.datetime.fromtimestamp(ev['date'] + off, datetime.timezone.utc).date().isoformat()
                splits.append((d, ev['numerator'] / ev['denominator']))
            fix_splits(sym, m, splits)
            return m
        except Exception as e:
            last = e
    raise RuntimeError(f'{sym}: {last}')


def fix_splits(sym, m, splits):
    """분할 비율만큼 튀는 구간을 찾아 분할 전 단위로 남은 봉만 나눈다.

    Yahoo는 분할 직후 오래된 과거는 이미 나누고 최근 며칠만 그대로 두는 등
    반만 반영된 상태로 주기도 해서, 분할일에서 거꾸로 훑으며 봉마다 단위를 판정한다.
    """
    ds = sorted(m)
    for d, r in sorted(set(splits)):
        if r <= 0 or abs(r - 1) < 1e-9:
            continue
        idx = next((i for i, x in enumerate(ds) if x >= d), None)
        if not idx:
            continue
        lr = math.log(r)
        near = lambda j, target: j > 0 and abs(math.log(j) - target) < abs(lr) / 2
        pre, state = [], 0          # state 1 = 분할 전 단위로 남아 있음
        for k in range(idx, 0, -1):
            a, b = m[ds[k - 1]]['raw'], m[ds[k]]['raw']
            if idx - k < 60 and b > 0:
                if state == 0 and near(a / b, lr):
                    state = 1
                elif state == 1 and near(a / b, -lr):
                    state = 0
            if state:
                pre.append(ds[k - 1])
        for x in pre:
            m[x]['c'] /= r
            m[x]['raw'] /= r
            m[x]['v'] *= r
        if pre:
            log(f'  {sym}: {d} {r:g}대1 분할이 반영되지 않은 봉 {len(pre)}개를 직접 보정({min(pre)}~{max(pre)})')


def check_jumps(D):
    """핵심 종목의 하루 변화가 비정상이면 게시하지 않고 멈춘다(분할 미반영 등 자료 오류 방지)."""
    lim = {'SOXL': 1.0, 'SOXS': 1.0}
    for s in ['SOXX', 'SPY', 'SOXL', 'SOXS', '005930.KS', '000660.KS', '^KS11']:
        ds = sorted(D[s])[-300:]
        for a, b in zip(ds, ds[1:]):
            x, y = D[s][a]['c'], D[s][b]['c']
            if x > 0 and y > 0 and abs(math.log(y / x)) > lim.get(s, 0.45):
                sys.exit(f'자료 이상으로 중단: {s} {a}→{b} 하루 {y / x - 1:+.0%}. 분할이 반영되지 않았는지 확인 필요')


def soxx_holdings():
    try:
        txt = get('https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/latest-holdings.csv').decode('utf-8-sig')
        lines = txt.splitlines()
        asof = next((re.sub(r'.*?,"?([^"]+)"?$', r'\1', l) for l in lines if 'Holdings as of' in l), '')
        hi = next(i for i, l in enumerate(lines) if l.startswith('Ticker'))
        rows = list(csv.reader(lines[hi:]))
        head = rows[0]
        ti, ci = head.index('Ticker'), head.index('Asset Class')
        tick = [r[ti].strip() for r in rows[1:] if len(r) > ci and r[ci] == 'Equity' and r[ti].strip()]
        if len(tick) >= 20:
            return tick, asof
        log('  iShares 보유종목 수가 적어 기본 목록 사용')
    except Exception as e:
        log('  iShares 보유종목 받기 실패, 기본 목록 사용:', e)
    return DEFAULT_HOLDINGS, '2026년 11월 3일(기본 목록)'


def flows_daum(code):
    out = []
    for p in range(1, 13):
        j = json.loads(get(f'https://finance.daum.net/api/investor/days?symbolCode=A{code}&page={p}&perPage=100&pagination=true',
                           {'Referer': f'https://finance.daum.net/quotes/A{code}', 'Accept': 'application/json'}))
        data = j.get('data') or []
        out += data
        if len(data) < 100:
            break
    return {x['date'][:10]: (x['foreignStraightPurchaseVolume'], x['tradePrice'], x['accTradeVolume']) for x in out}


def flows_naver(code):
    out = {}
    for p in range(1, 60):
        html = get(f'https://finance.naver.com/item/frgn.naver?code={code}&page={p}',
                   {'Referer': 'https://finance.naver.com/'}).decode('euc-kr', 'replace')
        rows = re.findall(r'<tr[^>]*onMouseOver[^>]*>(.*?)</tr>', html, re.S)
        got = 0
        for r in rows:
            cells = [re.sub(r'<[^>]+>|\s', '', c) for c in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)]
            if len(cells) < 7 or not re.match(r'\d{4}\.\d{2}\.\d{2}', cells[0]):
                continue
            n = lambda s: int(re.sub(r'[^\d\-]', '', s.replace('+', '')) or 0)
            out[cells[0].replace('.', '-')] = (n(cells[6]), n(cells[1]), n(cells[4]))
            got += 1
        if got == 0 or len(out) >= 1100:
            break
    return out


def flows():
    for name, fn in (('Daum 금융', flows_daum), ('네이버 금융', flows_naver)):
        try:
            a, b = fn('005930'), fn('000660')
            if len(a) > 300 and len(b) > 300:
                return a, b, name
            log(f'  {name} 행 수 부족: {len(a)}, {len(b)}')
        except Exception as e:
            log(f'  {name} 실패:', e)
    raise RuntimeError('외국인 순매수 데이터를 받지 못함')


def unfinished(now=None):
    """장이 아직 끝나지 않은 날짜를 (미국, 한국)으로 돌려준다. 이미 끝났으면 None."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    et, kst = now.astimezone(US_TZ), now.astimezone(KST)
    us = et.date().isoformat() if et.time() < datetime.time(16, 15) else None
    kr = kst.date().isoformat() if kst.time() < datetime.time(16, 0) else None
    return us, kr


# ---------- 계산 ----------
def rnd(x):
    return None if x is None else int(math.floor(x + 0.5))


def pct(a, i):
    if i < W - 1 or a[i] is None:
        return None
    x, lo, eq, n = a[i], 0, 0, 0
    for k in range(i - W + 1, i + 1):
        v = a[k]
        if v is None:
            continue
        n += 1
        if v < x:
            lo += 1
        elif v == x:
            eq += 1
    return None if n < W * 0.9 else (lo + 0.5 * eq) / n * 100


def P(a):
    return [pct(a, i) for i in range(len(a))]


def inv(a):
    return [None if p is None else 100 - p for p in P(a)]


def mean(a, i, n):
    if i < n - 1:
        return None
    w = a[i - n + 1:i + 1]
    return None if any(v is None for v in w) else sum(w) / n


def mom_f(C):
    return [None if (m := mean(C, i, 125)) is None or C[i] is None else C[i] / m - 1 for i in range(len(C))]


def vol_f(C):
    r = [math.log(C[i] / C[i - 1]) if i > 0 and C[i] is not None and C[i - 1] is not None else None for i in range(len(C))]
    out = []
    for i in range(len(C)):
        w = r[i - 19:i + 1] if i >= 20 else None
        if not w or any(x is None for x in w):
            out.append(None)
            continue
        mu = sum(w) / 20
        out.append(math.sqrt(sum((x - mu) ** 2 for x in w) / 19) * math.sqrt(252))
    return out


def rel_f(C, B):
    return [None if i < 20 or None in (C[i], C[i - 20], B[i], B[i - 20]) else C[i] / C[i - 20] - B[i] / B[i - 20] for i in range(len(C))]


def al(m, ds):
    return [m[d]['c'] if m and d in m else None for d in ds]


def main():
    log('1) Yahoo Finance')
    H, hold_asof = soxx_holdings()
    log(f'  구성종목 {len(H)}개 ({hold_asof})')
    D, failed = {}, []
    for s in ['SOXX', 'SPY', 'SOXL', 'SOXS', '005930.KS', '000660.KS', '^KS11'] + H:
        try:
            D[s] = yahoo(s)
        except Exception as e:
            log('  실패', s, e)
            D[s] = None
            failed.append(s)
        time.sleep(0.3)
    for need in ['SOXX', 'SPY', 'SOXL', 'SOXS', '005930.KS', '000660.KS', '^KS11']:
        if not D[need]:
            sys.exit(f'필수 데이터 {need}를 받지 못해 중단')
    check_jumps(D)

    for s in ('SOXX', '005930.KS', '^KS11'):
        tail = [(d, round(D[s][d]['raw'], 2), D[s][d]['v']) for d in sorted(D[s])[-3:]]
        log(f'  {s} 마지막 3개 봉(날짜, 종가, 거래량): {tail}')
    # 장중 값은 쓰지 않는다(지수는 종가 기준)
    us_cut, kr_cut = unfinished()
    for s, m in D.items():
        cut = kr_cut if s in KR_SYMS else us_cut
        if m and cut:
            m.pop(cut, None)
    log(f'  장 마감 전이라 제외한 날짜: 미국 {us_cut or "없음"}, 한국 {kr_cut or "없음"}')

    # 미국
    ud = sorted(D['SOXX'])
    C, SP = al(D['SOXX'], ud), al(D['SPY'], ud)
    mom, vol, rel = mom_f(C), vol_f(C), rel_f(C, SP)

    def dv(m, d):
        return m[d]['raw'] * m[d]['v'] if d in m else None
    shr = []
    for d in ud:
        l, s = dv(D['SOXL'], d), dv(D['SOXS'], d)
        shr.append(None if l is None or s is None or l + s == 0 else l / (l + s))
    bet = [mean(shr, i, 10) for i in range(len(ud))]
    HC = [al(D[s], ud) for s in H if D.get(s)]
    net, brd = [], []
    for i in range(len(ud)):
        h = l = e = 0
        ab = e2 = 0
        for a in HC:
            if a[i] is None:
                continue
            if i >= W - 1:
                v = [x for x in a[i - W + 1:i + 1] if x is not None]
                if len(v) >= W * 0.95:
                    e += 1
                    h += a[i] >= max(v)
                    l += a[i] <= min(v)
            if i >= 49:
                w = a[i - 49:i + 1]
                if all(x is not None for x in w):
                    e2 += 1
                    ab += a[i] > sum(w) / 50
        net.append(None if e < 10 else (h - l) / e)
        brd.append(None if e2 < 10 else ab / e2)
    st = [mean(net, i, 10) for i in range(len(ud))]
    us_sc = [P(mom), P(st), P(brd), P(bet), inv(vol), P(rel)]

    # 한국
    kd = sorted(d for d in D['005930.KS'] if d in D['000660.KS'] and d in D['^KS11'])
    s1, s2, KP = al(D['005930.KS'], kd), al(D['000660.KS'], kd), al(D['^KS11'], kd)
    B = [100.0]
    for i in range(1, len(kd)):
        B.append(B[-1] * (1 + 0.5 * (s1[i] / s1[i - 1] - 1) + 0.5 * (s2[i] / s2[i - 1] - 1)))
    km, kv, kr = mom_f(B), vol_f(B), rel_f(B, KP)
    kr_sc = [P(km), inv(kv), P(kr)]

    log('2) 외국인 순매수')
    fa, fb, fsrc = flows()
    if kr_cut:
        fa.pop(kr_cut, None)
        fb.pop(kr_cut, None)
    fd = sorted(d for d in fa if d in fb)
    num = [fa[d][0] * fa[d][1] + fb[d][0] * fb[d][1] for d in fd]
    den = [fa[d][2] * fa[d][1] + fb[d][2] * fb[d][1] for d in fd]
    ratio = [None if i < 19 or sum(den[i - 19:i + 1]) <= 0 else sum(num[i - 19:i + 1]) / sum(den[i - 19:i + 1]) for i in range(len(fd))]
    fsc = P(ratio)
    flow = {d: (ratio[i], fsc[i]) for i, d in enumerate(fd)}
    log(f'  {fsrc}: {len(fd)}일')

    # 최근 1년 행 만들기
    def window(ds):
        start = (datetime.date.fromisoformat(ds[-1]) - datetime.timedelta(days=733)).isoformat()
        return [i for i, d in enumerate(ds) if d >= start]
    us_rows = []
    for i in window(ud):
        sc = [a[i] for a in us_sc]
        if None in sc:
            continue
        lx = D['SOXL'].get(ud[i])
        if lx is None:
            continue
        us_rows.append([ud[i], round(C[i], 1), round(lx['raw'], 2)] + [rnd(x) for x in sc])
    kr_rows = []
    for i in window(kd):
        f = flow.get(kd[i])
        sc = [a[i] for a in kr_sc] + [f[1] if f else None]
        if None in sc:
            continue
        kr_rows.append([kd[i], B[i], D['005930.KS'][kd[i]]['raw'], D['000660.KS'][kd[i]]['raw']] + [rnd(x) for x in sc])
    if len(us_rows) < 400 or len(kr_rows) < 400:
        sys.exit(f'행 수 부족: 미국 {len(us_rows)}, 한국 {len(kr_rows)}')
    base = kr_rows[0][1]
    for r in kr_rows:
        r[1] = round(r[1] / base * 100, 1)

    def last2(m):
        k = sorted(m)
        return m[k[-1]]['raw'], m[k[-2]]['raw']
    ui, ki = len(ud) - 1, len(kd) - 1
    (x1, x0), (l1, l0), (a1, a0), (b1, b0) = last2(D['SOXX']), last2(D['SOXL']), last2(D['005930.KS']), last2(D['000660.KS'])
    k4 = lambda v: None if v is None else round(v, 4)
    lf = flow.get(kr_rows[-1][0])
    data = {
        'asof': {'us': us_rows[-1][0], 'kr': kr_rows[-1][0]},
        'keys': {'us': ['mom', 'str', 'brd', 'bet', 'vol', 'rel'], 'kr': ['mom', 'vol', 'rel', 'flow']},
        'us': us_rows, 'kr': kr_rows,
        'latest': {
            'us': {'mom': k4(mom[ui]), 'str': k4(st[ui]), 'brd': k4(brd[ui]), 'bet': k4(bet[ui]), 'vol': k4(vol[ui]), 'rel': k4(rel[ui]), 'close': x1, 'prev': x0, 'soxl': l1, 'soxlp': l0},
            'kr': {'mom': k4(km[ki]), 'vol': k4(kv[ki]), 'rel': k4(kr[ki]), 's1': a1, 's1p': a0, 's2': b1, 's2p': b0, 'kospi': round(KP[ki], 2),
                   'flow': None if not lf else round(lf[0] * 100, 2)},
        },
        'updated': datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M'),
        'holdings_asof': hold_asof, 'holdings_n': len(HC), 'flow_source': fsrc, 'failed': failed,
    }
    # Yahoo가 지수의 전날 봉을 늦게 채우는 때가 있어, 이전 결과보다 날짜가 앞서면 그 시장은 이전 값을 유지
    try:
        old = json.load(open(os.path.join(OUT, 'data.json'), encoding='utf-8'))
    except Exception:
        old = None
    for mk in ('us', 'kr'):
        if not old or old.get('asof', {}).get(mk, '') <= data['asof'][mk] or not old.get(mk):
            continue
        if len(old[mk][-1]) != len(data[mk][-1]):
            continue
        log(f"  {mk}: 새 자료({data['asof'][mk]})가 이전 자료({old['asof'][mk]})보다 앞서 이전 값을 유지")
        data[mk], data['asof'][mk], data['latest'][mk] = old[mk], old['asof'][mk], old['latest'][mk]
    us_rows, kr_rows = data['us'], data['kr']
    os.makedirs(OUT, exist_ok=True)
    json.dump(data, open(os.path.join(OUT, 'data.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    tpl = open(os.path.join(ROOT, 'template.html'), encoding='utf-8').read()
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    page = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0}[hidden]{display:none!important}</style>'
            '</head><body>' + tpl.replace('__DATA__', payload) + '</body></html>')
    open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8').write(page)
    comp = lambda r, k: sum(r[k:]) / len(r[k:])
    log(f"3) 완료: 미국 {us_rows[-1][0]} 지수 {rnd(comp(us_rows[-1], 3))} ({len(us_rows)}행, {us_rows[0][0]}부터) / 한국 {kr_rows[-1][0]} 지수 {rnd(comp(kr_rows[-1], 4))} ({len(kr_rows)}행, {kr_rows[0][0]}부터)"
        f" / 실패 종목 {failed or '없음'}")


if __name__ == '__main__':
    main()
