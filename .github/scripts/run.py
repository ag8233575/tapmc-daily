import json
import hashlib
import re
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from pdf2image import convert_from_path

URL = "https://www.tapmc.com.tw/Pages/Trans/Price2"

OUT = Path("docs")
OUT.mkdir(parents=True, exist_ok=True)

PAGES_DIR = OUT / "veg_pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = OUT / "state.json"
MANIFEST_PATH = OUT / "veg_manifest.json"
VEG_PDF = OUT / "veg.pdf"

DPI = 220

DATA_KEYWORDS = [
    "蔬菜類",
    "LA", "LC", "LB", "LD", "LH", "LI",
    "甘藍", "大白菜", "小白菜", "青江菜", "菠菜", "高麗菜"
]

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_json(p: Path):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def taipei_now():
    taipei = timezone(timedelta(hours=8))
    return datetime.now(taipei)

def roc_date_str(dt: datetime) -> str:
    roc_year = dt.year - 1911
    return f"{roc_year:03d}/{dt.month:02d}/{dt.day:02d}"

def pdf_text(pdf_path: Path) -> str:
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception:
        return ""

def pdf_has_real_data(pdf_path: Path) -> tuple[bool, int]:
    txt = pdf_text(pdf_path)
    if not txt.strip():
        return (False, 0)

    hit = 0
    for k in DATA_KEYWORDS:
        if k in txt:
            hit += 1

    return (hit >= 2, hit)

def try_download_veg_pdf(page, expected_roc_date: str) -> bool:
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    # 滾動到按鈕區
    try:
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(300)
    except Exception:
        pass

    # 選蔬菜（盡可能）
    try:
        page.get_by_text("蔬菜", exact=True).first.click(timeout=2000)
    except Exception:
        try:
            page.locator("select").first.select_option(label="蔬菜")
        except Exception:
            pass

    # 日期填今天（ROC）
    try:
        inputs = page.locator("input")
        n = inputs.count()
        filled = False
        for i in range(min(n, 10)):
            el = inputs.nth(i)
            try:
                val = el.input_value(timeout=300)
            except Exception:
                val = ""
            if "/" in (val or "") or (val or "").isdigit() or val == "":
                el.fill(expected_roc_date, timeout=2000)
                filled = True
                break
        if not filled:
            inputs.first.fill(expected_roc_date, timeout=2000)
    except Exception:
        pass

    # 點查詢（非常重要）
    try:
        page.get_by_role("button", name=re.compile("查詢")).click(timeout=3000)
        page.wait_for_timeout(1500)
    except Exception:
        try:
            page.get_by_text("查詢").first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

    # 下載 PDF
    try:
        with page.expect_download(timeout=30000) as d:
            try:
                page.get_by_role("button", name=re.compile("下載PDF")).click(timeout=5000)
            except Exception:
                page.get_by_text("下載PDF檔").first.click(timeout=5000)

        d.value.save_as(str(VEG_PDF))
        return True
    except Exception:
        return False

def render_all_pages(pdf_path: Path) -> list[Path]:
    images = convert_from_path(str(pdf_path), dpi=DPI)
    out_files: list[Path] = []
    for i, img in enumerate(images, start=1):
        filename = f"veg_p{i:02d}.png"
        out_png = PAGES_DIR / filename
        tmp = out_png.with_suffix(".tmp.png")
        img.save(str(tmp), "PNG")
        tmp.replace(out_png)
        out_files.append(out_png)
    return out_files

def clean_extra_pages(keep: set[str]):
    for p in PAGES_DIR.glob("veg_p*.png"):
        if p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass

def in_window(now: datetime) -> bool:
    # 台北時間：只允許 07:30–08:10 執行（你可微調）
    hm = now.hour * 60 + now.minute
    return (7 * 60 + 30) <= hm <= (8 * 60 + 10)

def main():
    now = taipei_now()
    now_tpe_str = now.strftime("%Y-%m-%d %H:%M:%S %z")
    today_ymd = now.strftime("%Y-%m-%d")
    today_roc = roc_date_str(now)

    state = load_json(STATE_PATH)

    # ✅ 非早上時段：直接退出，不要覆蓋成模板狀態
    if not in_window(now):
        state.update({
            "time_taipei": now_tpe_str,
            "status": "skip_outside_window",
            "date": today_ymd,
            "detail": "skip_run_outside_0730_0810",
        })
        save_json(STATE_PATH, state)
        return

    max_attempts = 12
    sleep_seconds = 25  # 12 次約 5 分鐘
    ok_data = False
    detail = ""
    last_hit = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for attempt in range(1, max_attempts + 1):
            ok_download = try_download_veg_pdf(page, today_roc)
            if not ok_download or not VEG_PDF.exists():
                detail = f"attempt_{attempt}_pdf_download_failed"
                time.sleep(2)
                continue

            ok, hit = pdf_has_real_data(VEG_PDF)
            last_hit = hit
            if ok:
                ok_data = True
                detail = f"attempt_{attempt}_pdf_ok"
                break
            else:
                ok_data = False
                detail = f"attempt_{attempt}_pdf_template_no_data"
                time.sleep(sleep_seconds)

        browser.close()

    if not ok_data:
        state.update({
            "time_taipei": now_tpe_str,
            "status": "not_ready_or_template",
            "date": today_ymd,
            "detail": detail,
            "keyword_hit": last_hit,
        })
        if VEG_PDF.exists():
            state["veg_pdf_sha256"] = sha256_file(VEG_PDF)
        save_json(STATE_PATH, state)
        return

    veg_hash = sha256_file(VEG_PDF)

    pages = render_all_pages(VEG_PDF)
    keep_names = {p.name for p in pages}
    clean_extra_pages(keep_names)

    manifest = {
        "date": today_ymd,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "generated_at_taipei": now_tpe_str,
        "veg_pdf_sha256": veg_hash,
        "dpi": DPI,
        "pages": [p.name for p in pages],
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_tpe_str,
        "status": "updated",
        "date": today_ymd,
        "detail": detail,
        "veg_pdf_sha256": veg_hash,
        "page_count": len(pages),
    })
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
