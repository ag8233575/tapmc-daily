import json
import hashlib
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

# 清晰度：你若覺得模糊可調 240，但檔案會變大
DPI = 220

# 用來判斷「PDF是真的有資料」的關鍵字（命中任一即可）
DATA_KEYWORDS = ["LA", "LC", "LB", "甘藍", "大白菜", "小白菜", "青江菜", "蔬菜類"]

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

def roc_date_str(dt_tpe: datetime) -> str:
    roc_year = dt_tpe.year - 1911
    return f"{roc_year:03d}/{dt_tpe.month:02d}/{dt_tpe.day:02d}"

def pdf_has_data(pdf_path: Path) -> bool:
    """
    用 pdftotext 檢查 PDF 是否包含資料關鍵字。
    這能精準抓出「框線模板 PDF」。
    """
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        txt = (r.stdout or "") + "\n" + (r.stderr or "")
        txt = txt.replace("\x00", "")
        return any(kw in txt for kw in DATA_KEYWORDS)
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

def pick_veg_in_select(page) -> bool:
    """
    優先用 <select> 的 select_option 選蔬菜（比點文字穩）
    """
    selects = page.locator("select")
    if selects.count() == 0:
        # 沒有 select 就退回點文字
        try:
            page.get_by_text("蔬菜").first.click(timeout=3000)
            return True
        except Exception:
            return False

    for i in range(selects.count()):
        sel = selects.nth(i)
        try:
            options_text = sel.locator("option").all_inner_texts()
            if any("蔬菜" in t for t in options_text):
                # 先用 label
                try:
                    sel.select_option(label="蔬菜")
                    return True
                except Exception:
                    pass
                # 再用 value
                vals = sel.locator("option").evaluate_all(
                    "els => els.map(e => ({v:e.value, t:(e.textContent||'').trim()}))"
                )
                for it in vals:
                    if it and "蔬菜" in (it.get("t") or ""):
                        sel.select_option(value=it.get("v"))
                        return True
        except Exception:
            continue
    return False

def fill_date(page, roc_date: str) -> bool:
    """
    填日期（民國）
    """
    inputs = page.locator("input")
    n = inputs.count()
    if n == 0:
        return False

    # 優先找目前 value 長得像日期的欄位
    for i in range(min(n, 12)):
        inp = inputs.nth(i)
        try:
            val = inp.input_value(timeout=500)
            if "/" in val and len(val) >= 8:
                inp.click(timeout=1500)
                inp.fill(roc_date, timeout=1500)
                return True
        except Exception:
            continue

    # 找不到就填第一個（備援）
    try:
        inputs.first.click(timeout=1500)
        inputs.first.fill(roc_date, timeout=1500)
        return True
    except Exception:
        return False

def click_query(page) -> bool:
    try:
        page.get_by_role("button", name="查詢").click(timeout=4000)
        return True
    except Exception:
        pass
    try:
        page.get_by_text("查詢").first.click(timeout=4000)
        return True
    except Exception:
        return False

def wait_for_data_on_page(page) -> bool:
    """
    等畫面真的出現資料關鍵字（避免下載模板）
    """
    for kw in DATA_KEYWORDS:
        try:
            page.get_by_text(kw).first.wait_for(timeout=8000)
            return True
        except Exception:
            continue
    return False

def download_pdf(page) -> bool:
    """
    點下載 PDF
    """
    try:
        with page.expect_download(timeout=30000) as d:
            try:
                page.get_by_role("button", name="下載PDF檔").click(timeout=6000)
            except Exception:
                page.get_by_text("下載PDF檔").first.click(timeout=6000)
        d.value.save_as(str(VEG_PDF))
        return VEG_PDF.exists() and VEG_PDF.stat().st_size > 10_000
    except Exception:
        return False

def main():
    taipei = timezone(timedelta(hours=8))
    now_dt = datetime.now(taipei)
    now_tpe = now_dt.strftime("%Y-%m-%d %H:%M:%S %z")
    today_tpe = now_dt.strftime("%Y-%m-%d")
    roc_today = roc_date_str(now_dt)

    # 這裡設定：最多嘗試到台北 08:02:00（避免太早抓到模板）
    deadline = now_dt.replace(hour=8, minute=2, second=0, microsecond=0)
    # 如果 workflow 在 00:xx UTC 跑到台北早上，deadline 是今天 08:02
    # 若你手動在下午跑，也別等到明天，最多等 2 分鐘
    if now_dt > deadline:
        deadline = now_dt + timedelta(minutes=2)

    state = load_json(STATE_PATH)
    prev_hash = state.get("veg_pdf_sha256", "")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
        page = context.new_page()

        attempt = 0
        got_valid_pdf = False
        last_status = ""

        while datetime.now(taipei) <= deadline:
            attempt += 1
            last_status = f"attempt_{attempt}"

            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)

            pick_veg_in_select(page)
            fill_date(page, roc_today)

            click_query(page)
            page.wait_for_timeout(1200)

            # 等資料出現（若沒出現也繼續下載，但後面會用 pdftotext 驗證）
            wait_for_data_on_page(page)

            ok = download_pdf(page)
            if not ok:
                last_status = f"{last_status}_pdf_download_failed"
                time.sleep(10)
                continue

            # 驗證是否真有資料
            if pdf_has_data(VEG_PDF):
                got_valid_pdf = True
                last_status = f"{last_status}_pdf_ok"
                break
            else:
                # 這就是你遇到的「框線模板 PDF」
                last_status = f"{last_status}_pdf_template_no_data"
                # 等一下再試（常見：站方 7:40~8:00 才完成）
                time.sleep(20)

        context.close()
        browser.close()

    if not got_valid_pdf:
        # 不更新 manifest/pages：避免 Apps Script 推播錯圖
        state.update({
            "time_taipei": now_tpe,
            "status": "not_ready_or_template",
            "detail": last_status,
        })
        save_json(STATE_PATH, state)
        return

    veg_hash = sha256_file(VEG_PDF)

    # PDF 沒變就不更新（省額度）
    if veg_hash == prev_hash and MANIFEST_PATH.exists():
        state.update({
            "time_taipei": now_tpe,
            "status": "no_change",
            "veg_pdf_sha256": veg_hash,
        })
        save_json(STATE_PATH, state)
        return

    # 轉全頁 PNG
    pages = render_all_pages(VEG_PDF)
    keep_names = {p.name for p in pages}
    clean_extra_pages(keep_names)

    manifest = {
        "date": today_tpe,
        "generated_at_taipei": now_tpe,
        "source": "pdf",
        "veg_pdf_sha256": veg_hash,
        "dpi": DPI,
        "pages": [p.name for p in pages],
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_tpe,
        "status": "updated_pdf",
        "detail": last_status,
        "veg_pdf_sha256": veg_hash,
        "page_count": len(pages),
    })
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
