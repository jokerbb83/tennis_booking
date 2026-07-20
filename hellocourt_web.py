# -*- coding: utf-8 -*-
"""
HelloCourt — 강서구 테니스코트 빈자리 감시 웹 대시보드
- 실행: python hellocourt_web.py  →  브라우저 자동 오픈 (http://localhost:8899)
- 감시: 강서구립 3~6번, 우장산 1~4번 / 이번 달 + 다음 달
- 알림: 웹 대시보드(브라우저 알림 + 사운드) + 텔레그램(선택)

설치:
  pip install playwright
  playwright install chromium
"""

import asyncio
import json
import random
import sys
import threading
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

# ============ 설정 ============

PORT = 8899
RENT_TYPE = "1001"

TARGETS = [
    ("GANGSEO03", "02", "3", "강서구립 3번"),
    ("GANGSEO03", "02", "4", "강서구립 4번"),
    ("GANGSEO03", "02", "5", "강서구립 5번"),
    ("GANGSEO03", "02", "6", "강서구립 6번"),
    # 우장산: URL place 번호가 실제 코트 번호보다 1 큼
    ("GANGSEO02", "03", "2", "우장산 1번"),
    ("GANGSEO02", "03", "3", "우장산 2번"),
    ("GANGSEO02", "03", "4", "우장산 3번"),
    ("GANGSEO02", "03", "5", "우장산 4번"),
]

MONTHS_AHEAD = 2
CHECK_INTERVAL_SEC = 90
STATE_FILE = Path("court_state.json")

# 텔레그램 설정 — 저장소에 토큰이 노출되지 않도록 코드 밖에서 읽습니다.
# 방법 1(권장): 같은 폴더에 telegram.json 생성 (git에 올리지 말 것)
#   {"token": "1234567:AAH...", "chat_id": "987654321"}
# 방법 2: 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_tg_file = Path(__file__).with_name("telegram.json")
if _tg_file.exists():
    try:
        _tg = json.loads(_tg_file.read_text(encoding="utf-8"))
        TELEGRAM_BOT_TOKEN = _tg.get("token", TELEGRAM_BOT_TOKEN)
        TELEGRAM_CHAT_ID = str(_tg.get("chat_id", TELEGRAM_CHAT_ID))
    except Exception as e:
        print(f"[telegram.json 읽기 실패] {e}")

URL_TMPL = (
    "https://sports.gangseo.seoul.kr/fmcs/28"
    "?facilities_type=L&base_date={base_date}&rent_type=" + RENT_TYPE +
    "&center={center}&part={part}&place={place}#type_list"
)

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# ============ 공유 상태 ============

LOCK = threading.Lock()
STATE = {
    "started_at": datetime.now().strftime("%H:%M:%S"),
    "cycle": 0,
    "scanning": None,          # 현재 조회 중인 코트 이름
    "last_cycle_at": None,
    "next_cycle_in": None,
    "courts": {},              # name -> {"status", "checked_at", "slots":[{"date","time","label"}]}
    "alerts": [],              # [{"id","time","text"}]
    "alert_seq": 0,
}
for _, _, _, n in TARGETS:
    STATE["courts"][n] = {"status": "대기", "checked_at": None, "slots": []}


def fmt_date(d: str) -> str:
    dt = date(int(d[:4]), int(d[4:6]), int(d[6:]))
    return f"{int(d[4:6])}/{int(d[6:])}({WEEKDAY_KO[dt.weekday()]})"


def month_base_dates(n: int) -> list[str]:
    out, t = [], date.today()
    y, m = t.year, t.month
    for i in range(n):
        if i == 0:
            out.append(t.strftime("%Y%m%d"))
        else:
            mm = m + i
            yy = y + (mm - 1) // 12
            mm = (mm - 1) % 12 + 1
            out.append(f"{yy}{mm:02d}01")
    return out


# ============ 알림(텔레그램) ============

def send_telegram(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data, timeout=10,
        )
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


# ============ 중복 알림 방지 ============

def load_seen() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1), encoding="utf-8")


# ============ 스캐너 (검증 완료된 로직) ============

async def scan_month(page, url: str) -> list[tuple[str, str]]:
    async def read_slots():
        return await page.eval_on_selector_all(
            "td[id^='date-'] a[class*='state_']",
            "els => els.map(e => ({date: e.closest('td').id.replace('date-',''), "
            "time: e.textContent.trim(), ok: e.className.includes('state_Y')}))",
        )

    raw = []
    for attempt in range(3):
        if attempt == 0:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("td[id^='date-'] a[class*='state_']", timeout=15000)
            await page.wait_for_timeout(800)
        except Exception:
            pass
        raw = await read_slots()
        if raw:
            break

    if not raw:
        raise RuntimeError("슬롯 로딩 실패")

    today = date.today().strftime("%Y%m%d")
    return [
        (it["date"], it["time"]) for it in raw
        if it.get("ok") and len(it.get("date", "")) == 8 and it.get("time") and it["date"] >= today
    ]


async def monitor_loop():
    seen = load_seen()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            locale="ko-KR",
        )
        page = await context.new_page()

        while True:
            cycle_slots = {n: [] for _, _, _, n in TARGETS}
            cycle_fail = set()

            for base_date in month_base_dates(MONTHS_AHEAD):
                for center, part, place, name in TARGETS:
                    with LOCK:
                        STATE["scanning"] = f"{name} ({base_date[:4]}.{base_date[4:6]})"
                    url = URL_TMPL.format(base_date=base_date, center=center, part=part, place=place)
                    try:
                        slots = await scan_month(page, url)
                        cycle_slots[name].extend(slots)
                    except Exception as e:
                        cycle_fail.add(name)
                        print(f"[{name}] 조회 실패: {e}")
                    with LOCK:
                        STATE["courts"][name]["checked_at"] = datetime.now().strftime("%H:%M:%S")
                    await page.wait_for_timeout(1200)

            # 상태/알림 반영
            new_alerts = []
            with LOCK:
                for center, part, place, name in TARGETS:
                    c = STATE["courts"][name]
                    if name in cycle_fail and not cycle_slots[name]:
                        c["status"] = "실패"
                        continue
                    c["status"] = "정상"
                    c["slots"] = [
                        {"date": d, "time": t, "label": f"{fmt_date(d)} {t}"}
                        for d, t in sorted(set(cycle_slots[name]))
                    ]
                    for d, t in cycle_slots[name]:
                        key = f"{center}|{place}|{d}|{t}"
                        if key not in seen:
                            seen.add(key)
                            STATE["alert_seq"] += 1
                            alert = {
                                "id": STATE["alert_seq"],
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "text": f"{name} · {fmt_date(d)} {t}",
                            }
                            STATE["alerts"].insert(0, alert)
                            new_alerts.append(alert)
                    STATE["alerts"] = STATE["alerts"][:50]
                STATE["cycle"] += 1
                STATE["scanning"] = None
                STATE["last_cycle_at"] = datetime.now().strftime("%H:%M:%S")

            if new_alerts:
                save_seen(seen)
                msg = "🎾 코트 빈자리!\n" + "\n".join(a["text"] for a in new_alerts)
                msg += "\n예약: https://sports.gangseo.seoul.kr/fmcs/28"
                send_telegram(msg)
                print(msg)

            wait = max(30, CHECK_INTERVAL_SEC + random.randint(-10, 10))
            with LOCK:
                STATE["next_cycle_in"] = wait
            await asyncio.sleep(wait)


# ============ 웹 대시보드 ============

PAGE_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HelloCourt — 코트 빈자리 감시</title>
<style>
  :root {
    --navy: #0C1D37; --navy2: #12294D; --line: #AED700;
    --clay: #C96F4A; --white: #F4F6F0; --dim: #7C8AA0; --red: #E05252;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--navy); color: var(--white); min-height: 100vh;
    font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif;
  }
  /* 코트 라인 모티프: 좌측 서비스 라인 */
  body::before {
    content: ""; position: fixed; left: 28px; top: 0; bottom: 0; width: 2px;
    background: var(--line); opacity: .35;
  }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 24px 80px; }

  header { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  h1 { font-size: 22px; font-weight: 800; letter-spacing: -.5px; }
  h1 em { font-style: normal; color: var(--line); }
  .meta { font-size: 13px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .meta .live { color: var(--line); }
  .meta .live::before {
    content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--line); margin-right: 6px; animation: pulse 1.6s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .25 } }

  /* 히어로: 현재 잡을 수 있는 빈자리 */
  .hero { margin: 36px 0 40px; }
  .hero .eyebrow { font-size: 12px; letter-spacing: 2px; color: var(--dim); margin-bottom: 12px; }
  .hero .none { font-size: 34px; font-weight: 300; color: var(--dim); letter-spacing: -1px; }
  .vacancy {
    display: flex; align-items: baseline; gap: 14px; padding: 16px 0;
    border-bottom: 1px solid rgba(174,215,0,.18); cursor: pointer;
  }
  .vacancy:hover .go { color: var(--line); }
  .vacancy .when { font-size: 26px; font-weight: 800; letter-spacing: -.5px; color: var(--line); font-variant-numeric: tabular-nums; }
  .vacancy .court { font-size: 16px; color: var(--white); }
  .vacancy .go { margin-left: auto; font-size: 13px; color: var(--dim); }

  /* 코트 그리드 */
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  @media (max-width: 640px) { .grid { grid-template-columns: repeat(2, 1fr); } }
  .court-card {
    background: var(--navy2); border-radius: 8px; padding: 14px 14px 12px;
    border: 1px solid transparent;
  }
  .court-card.hot { border-color: var(--line); }
  .court-card.fail { border-color: var(--red); }
  .court-card h3 { font-size: 14px; font-weight: 700; margin-bottom: 6px; }
  .court-card .st { font-size: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .court-card.hot .st { color: var(--line); }
  .court-card.fail .st { color: var(--red); }
  .court-card ul { list-style: none; margin-top: 8px; }
  .court-card li { font-size: 12px; color: var(--line); padding: 2px 0; font-variant-numeric: tabular-nums; }

  section h2 { font-size: 12px; letter-spacing: 2px; color: var(--dim); margin: 40px 0 14px; }

  /* 알림 로그 */
  .log { list-style: none; }
  .log li {
    display: flex; gap: 14px; padding: 8px 0; font-size: 14px;
    border-bottom: 1px solid rgba(255,255,255,.05); font-variant-numeric: tabular-nums;
  }
  .log .t { color: var(--dim); flex-shrink: 0; }
  .log li.fresh { animation: flash 1.2s 2; }
  @keyframes flash { 0%,100% { background: transparent } 50% { background: rgba(174,215,0,.12) } }
  .log .empty { color: var(--dim); border: none; }

  .notice-btn {
    background: none; border: 1px solid var(--dim); color: var(--dim); border-radius: 20px;
    padding: 6px 14px; font-size: 12px; cursor: pointer; margin-top: 10px;
  }
  .notice-btn.on { border-color: var(--line); color: var(--line); cursor: default; }
  footer { margin-top: 60px; font-size: 12px; color: var(--dim); }
  footer a { color: var(--dim); }
  @media (prefers-reduced-motion: reduce) { .meta .live::before, .log li.fresh { animation: none; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Hello<em>Court</em></h1>
    <div class="meta" id="meta">연결 중…</div>
  </header>

  <div class="hero">
    <div class="eyebrow">지금 잡을 수 있는 빈자리</div>
    <div id="vacancies"><div class="none">아직 없음 — 감시 중</div></div>
    <button class="notice-btn" id="noticeBtn" onclick="askNotice()">브라우저 알림 켜기</button>
  </div>

  <section>
    <h2>코트 상태</h2>
    <div class="grid" id="grid"></div>
  </section>

  <section>
    <h2>알림 기록</h2>
    <ul class="log" id="log"><li class="empty">빈자리가 발견되면 여기에 기록됩니다.</li></ul>
  </section>

  <footer>새 알림 시 소리가 나며, 항목을 클릭하면 예약 페이지가 열립니다 ·
    <a href="https://sports.gangseo.seoul.kr/fmcs/28" target="_blank">강서구 예약 사이트</a></footer>
</div>

<script>
const RESERVE_URL = "https://sports.gangseo.seoul.kr/fmcs/28";
let lastAlertId = null;   // null = 첫 로딩(기존 알림에는 소리 안 냄)

function askNotice() {
  Notification.requestPermission().then(refreshBtn);
}
function refreshBtn() {
  const b = document.getElementById("noticeBtn");
  if (Notification.permission === "granted") { b.textContent = "브라우저 알림 켜짐"; b.classList.add("on"); }
}
refreshBtn();

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25, 0.5].forEach(t => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value = 1320; g.gain.value = 0.15;
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.15);
    });
  } catch (e) {}
}

async function tick() {
  let s;
  try {
    s = await (await fetch("/status")).json();
  } catch (e) {
    document.getElementById("meta").textContent = "서버 연결 끊김 — 프로그램이 꺼졌는지 확인하세요";
    return;
  }

  // 상단 상태
  const meta = document.getElementById("meta");
  meta.innerHTML = s.scanning
    ? `<span class="live">조회 중 · ${s.scanning}</span>`
    : `<span class="live">감시 중</span> · ${s.cycle}회 순회 · 마지막 ${s.last_cycle_at || "-"}`;

  // 히어로: 현재 빈자리 전체
  const all = [];
  for (const [name, c] of Object.entries(s.courts))
    for (const sl of c.slots) all.push({ name, ...sl });
  all.sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time));
  const v = document.getElementById("vacancies");
  v.innerHTML = all.length
    ? all.map(x => `<div class="vacancy" onclick="window.open(RESERVE_URL)">
        <span class="when">${x.label.split(" ")[1]}</span>
        <span class="court">${x.label.split(" ")[0]} · ${x.name} 코트</span>
        <span class="go">예약하러 가기 →</span></div>`).join("")
    : `<div class="none">아직 없음 — 감시 중</div>`;

  // 코트 그리드
  document.getElementById("grid").innerHTML = Object.entries(s.courts).map(([name, c]) => {
    const cls = c.slots.length ? "hot" : (c.status === "실패" ? "fail" : "");
    const st = c.status === "실패" ? "조회 실패" :
               c.slots.length ? `빈자리 ${c.slots.length}건` :
               (c.checked_at ? `확인 ${c.checked_at}` : "대기");
    const list = c.slots.slice(0, 4).map(sl => `<li>${sl.label}</li>`).join("");
    return `<div class="court-card ${cls}"><h3>${name}</h3><div class="st">${st}</div><ul>${list}</ul></div>`;
  }).join("");

  // 알림 로그 + 새 알림 감지
  const log = document.getElementById("log");
  if (s.alerts.length) {
    const maxId = s.alerts[0].id;
    const isNew = lastAlertId !== null && maxId > lastAlertId;
    log.innerHTML = s.alerts.map(a =>
      `<li class="${lastAlertId !== null && a.id > lastAlertId ? "fresh" : ""}">
         <span class="t">${a.time}</span><span>${a.text}</span></li>`).join("");
    if (isNew) {
      beep();
      if (Notification.permission === "granted") {
        const fresh = s.alerts.filter(a => a.id > lastAlertId);
        new Notification("🎾 코트 빈자리!", { body: fresh.map(a => a.text).join("\\n") });
      }
    }
    lastAlertId = maxId;
  } else if (lastAlertId === null) {
    lastAlertId = 0;
  }
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 콘솔 소음 제거
        pass

    def do_GET(self):
        if self.path == "/status":
            with LOCK:
                body = json.dumps(STATE, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        else:
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"대시보드: http://localhost:{PORT}")
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_telegram("테스트 알림입니다. 이 메시지가 보이면 텔레그램 설정 성공! 🎾")
        print("텔레그램 테스트 전송 완료 (설정이 비어있으면 아무 일도 안 일어납니다)")
        sys.exit(0)
    print("HelloCourt 시작 — 코트 8개 / 2개월치 감시")
    start_server()
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        print("\n종료합니다.")
