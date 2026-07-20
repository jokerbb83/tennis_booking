# -*- coding: utf-8 -*-
"""
monitor.py — GitHub Actions용 1회 스캔 스크립트
- 8개 코트 × 2개월 조회 → status.json 기록
- 새 빈자리는 텔레그램으로 알림 (환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
- 로컬 테스트: pip install playwright && playwright install chromium && python monitor.py
"""

import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

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
STATUS_FILE = Path(__file__).with_name("status.json")

KST = timezone(timedelta(hours=9))
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

URL_TMPL = (
    "https://sports.gangseo.seoul.kr/fmcs/28"
    "?facilities_type=L&base_date={base_date}&rent_type=" + RENT_TYPE +
    "&center={center}&part={part}&place={place}#type_list"
)


def now_kst() -> datetime:
    return datetime.now(KST)


def fmt_date(d: str) -> str:
    dt = date(int(d[:4]), int(d[4:6]), int(d[6:]))
    return f"{int(d[4:6])}/{int(d[6:])}({WEEKDAY_KO[dt.weekday()]})"


def month_base_dates(n: int) -> list[str]:
    out, t = [], now_kst().date()
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


def send_telegram(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[텔레그램 미설정 — 알림 생략]")
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data, timeout=10,
        )
        print("[텔레그램 전송 완료]")
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


OFF_WORDS = ("끄기", "꺼", "정지", "중지", "/off", "off", "stop")
ON_WORDS = ("켜기", "켜", "시작", "/on", "on", "start")
STATUS_WORDS = ("상태", "/status", "status")

# ---- 알림 필터 ----
# 텔레그램 명령 예:
#   필터 토 일            → 토·일요일만
#   필터 주말 17-21       → 주말 17:00~21:00 시작 슬롯만
#   필터 평일 19          → 평일 19:00~21:00만
#   필터 7/25 7/26        → 특정 날짜만
#   필터 해제             → 필터 없음(전체 알림)
#   필터                  → 현재 필터 보기

WD_CHARS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
SLOT_STARTS = (7, 9, 11, 13, 15, 17, 19)


def parse_filter(text: str) -> dict | None:
    """'필터 ...' 명령을 필터 dict로. 해제면 {}, 형식 오류면 None"""
    import re
    body = text.replace("필터", "", 1).strip()
    if body in ("해제", "삭제", "없음", "off", "reset"):
        return {}
    wd, hours, dates = set(), set(), set()
    for tok in body.split():
        if tok in ("주말",):
            wd |= {5, 6}
        elif tok in ("평일",):
            wd |= {0, 1, 2, 3, 4}
        elif all(ch in WD_CHARS for ch in tok.replace(",", "")):
            for ch in tok.replace(",", ""):
                wd.add(WD_CHARS[ch])
        elif re.fullmatch(r"\d{1,2}[/.]\d{1,2}", tok):          # 7/25
            m, d = re.split(r"[/.]", tok)
            dates.add(f"{int(m):02d}{int(d):02d}")
        elif re.fullmatch(r"\d{1,2}\s*[-~]\s*\d{1,2}", tok):     # 17-21
            a, b = re.split(r"[-~]", tok)
            a, b = int(a), int(b)
            hours |= {h for h in SLOT_STARTS if a <= h < b}
        elif re.fullmatch(r"\d{1,2}(:\d{2})?", tok):             # 19 또는 19:00
            hours.add(int(tok.split(":")[0]))
        else:
            return None
    if not (wd or hours or dates):
        return None
    f = {}
    if wd: f["weekdays"] = sorted(wd)
    if hours: f["hours"] = sorted(hours)
    if dates: f["dates"] = sorted(dates)
    return f


def filter_desc(f: dict | None) -> str:
    if not f:
        return "없음 (전체 알림)"
    parts = []
    if f.get("weekdays"):
        parts.append("·".join(WEEKDAY_KO[w] for w in f["weekdays"]) + "요일")
    if f.get("dates"):
        parts.append(", ".join(f"{int(d[:2])}/{int(d[2:])}" for d in f["dates"]))
    if f.get("hours"):
        parts.append(", ".join(f"{h:02d}시" for h in f["hours"]) + " 시작")
    return " / ".join(parts)


def slot_passes(f: dict | None, d: str, t: str) -> bool:
    """필터 f에 대해 날짜 d(YYYYMMDD)·시간 t('HH:MM~HH:MM') 슬롯 통과 여부"""
    if not f:
        return True
    if f.get("weekdays") is not None or f.get("dates") is not None:
        dt = date(int(d[:4]), int(d[4:6]), int(d[6:]))
        ok_day = False
        if f.get("weekdays") and dt.weekday() in f["weekdays"]:
            ok_day = True
        if f.get("dates") and d[4:] in f["dates"]:
            ok_day = True
        if not ok_day:
            return False
    if f.get("hours"):
        try:
            start_h = int(t.split(":")[0])
        except ValueError:
            return True
        if start_h not in f["hours"]:
            return False
    return True


def check_telegram_commands(enabled: bool, offset: int, filt: dict | None):
    """켜기/끄기/상태/필터 명령 처리. (enabled, offset, status_requested, filt) 반환"""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return enabled, offset, False, filt
    status_requested = False
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset + 1}&timeout=0"
        res = json.loads(urllib.request.urlopen(url, timeout=10).read())
        for upd in res.get("result", []):
            offset = max(offset, upd["update_id"])
            msg = upd.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                continue
            text = (msg.get("text") or "").strip()
            low = text.lower()
            if text.startswith("필터") or low.startswith("/filter"):
                nf = parse_filter(text.replace("/filter", "필터", 1))
                if nf is None and text.strip() not in ("필터", "/filter"):
                    send_telegram(
                        "필터 형식을 이해하지 못했어요.\n예)\n"
                        "필터 주말\n필터 토 일 17-21\n필터 평일 19\n필터 7/25\n필터 해제"
                    )
                elif text.strip() in ("필터", "/filter"):
                    send_telegram(f"현재 알림 필터: {filter_desc(filt)}")
                else:
                    filt = nf if nf else None
                    send_telegram(f"알림 필터 설정: {filter_desc(filt)}")
            elif any(w in low for w in OFF_WORDS):
                if enabled:
                    enabled = False
                    send_telegram("감시를 껐습니다. 다시 켜려면 '켜기'라고 보내주세요.")
            elif any(w in low for w in ON_WORDS):
                if not enabled:
                    enabled = True
                    send_telegram("감시를 다시 켰습니다. 🎾")
            elif any(w in low for w in STATUS_WORDS):
                status_requested = True
    except Exception as e:
        print(f"[텔레그램 명령 확인 실패] {e}")
    return enabled, offset, status_requested, filt


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

    today = now_kst().strftime("%Y%m%d")
    return [
        (it["date"], it["time"]) for it in raw
        if it.get("ok") and len(it.get("date", "")) == 8 and it.get("time") and it["date"] >= today
    ]


def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"courts": {}, "alerts": [], "seen": []}


async def main():
    prev = load_status()
    seen = set(prev.get("seen", []))
    alerts = prev.get("alerts", [])
    enabled = prev.get("enabled", True)
    tg_offset = prev.get("tg_offset", 0)
    filt = prev.get("filter") or None

    # 텔레그램으로 온 켜기/끄기/상태/필터 명령 처리
    enabled, tg_offset, status_req, filt = check_telegram_commands(enabled, tg_offset, filt)

    if status_req:
        n = sum(len(c.get("slots", [])) for c in prev.get("courts", {}).values())
        send_telegram(
            f"감시 {'켜짐 🟢' if enabled else '꺼짐 ⚪'}\n"
            f"현재 빈자리 {n}건 · 마지막 조회 {prev.get('updated_at', '-')}\n"
            f"알림 필터: {filter_desc(filt)}"
        )

    if not enabled:
        # 꺼진 상태: 조회하지 않고 상태만 기록
        STATUS_FILE.write_text(json.dumps({
            "updated_at": prev.get("updated_at", now_kst().strftime("%Y-%m-%d %H:%M:%S")),
            "enabled": False,
            "filter": filt,
            "filter_desc": filter_desc(filt),
            "courts": prev.get("courts", {}),
            "alerts": alerts[:50],
            "seen": sorted(seen),
            "tg_offset": tg_offset,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("감시 꺼짐 — 조회 생략")
        return

    courts = {}
    new_alerts = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            locale="ko-KR",
        )
        page = await context.new_page()

        for center, part, place, name in TARGETS:
            all_slots, ok = [], True
            for base_date in month_base_dates(MONTHS_AHEAD):
                url = URL_TMPL.format(base_date=base_date, center=center, part=part, place=place)
                try:
                    all_slots.extend(await scan_month(page, url))
                except Exception as e:
                    ok = False
                    print(f"[{name} {base_date[:6]}] 실패: {e}")
                await page.wait_for_timeout(1000)

            courts[name] = {
                "status": "정상" if ok else "일부 실패",
                "slots": [
                    {"date": d, "time": t, "label": f"{fmt_date(d)} {t}"}
                    for d, t in sorted(set(all_slots))
                ],
            }
            print(f"[{name}] 빈자리 {len(courts[name]['slots'])}건")

            for d, t in all_slots:
                if not slot_passes(filt, d, t):
                    continue  # 필터 미통과: 알림 안 함 (필터 바꾸면 그때 알림됨)
                key = f"{center}|{place}|{d}|{t}"
                if key not in seen:
                    seen.add(key)
                    new_alerts.append({
                        "time": now_kst().strftime("%m/%d %H:%M"),
                        "text": f"{name} · {fmt_date(d)} {t}",
                    })

        await browser.close()

    if new_alerts:
        alerts = new_alerts + alerts
        msg = "🎾 코트 빈자리!\n" + "\n".join(a["text"] for a in new_alerts)
        msg += "\n예약: https://sports.gangseo.seoul.kr/fmcs/28"
        send_telegram(msg)

    # 지난 날짜의 seen 키는 정리 (파일 무한 증식 방지)
    today = now_kst().strftime("%Y%m%d")
    seen = {k for k in seen if k.split("|")[2] >= today}

    STATUS_FILE.write_text(json.dumps({
        "updated_at": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "enabled": True,
        "filter": filt,
        "filter_desc": filter_desc(filt),
        "courts": courts,
        "alerts": alerts[:50],
        "seen": sorted(seen),
        "tg_offset": tg_offset,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"status.json 기록 완료 / 새 알림 {len(new_alerts)}건")


if __name__ == "__main__":
    asyncio.run(main())
