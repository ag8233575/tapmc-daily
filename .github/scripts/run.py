import json
import hashlib
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

DPI = 220  # 你原本的設定保留；要更清楚可改 240

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

def try_download_veg_pdf(page) -> bool:
    page.goto(URL, wait_until="networkidle")

    # 切換到「蔬菜」（若頁面有 tab/按鈕）
    try:
        page.get_by_text("蔬菜").first.click(timeout=3000)
        page.wait_for_timeout(600)
    except Exception:
        pass

    # 若有查詢按鈕，點一下（有些頁面需要）
    try:
        page.get_by_text("查詢").first.click(timeout=2000)
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # 下載 PDF
    try:
        with page.expect_download(timeout=30000) as d:
            try:
                page.get_by_text("下載PDF檔").first.click(timeout=5000)
            except Exception:
                page.get_by_text("PDF").first.click(timeout=5000)
        d.value.save_as(str(VEG_PDF))
        return True
    except Exception:
        return False

def render_all_pages(pdf_path: Path, date_str: str) -> list[Path]:
    """
    轉所有頁，輸出成：veg_YYYY-MM-DD_p01.png, veg_YYYY-MM-DD_p02.png...
    這樣每天不覆蓋舊檔，LINE/Pages 快取就不會造成昨天/今天混亂。
    """
    images = convert_from_path(str(pdf_path), dpi=DPI)
    out_files: list[Path] = []

    for i, img in enumerate(images, start=1):
        filename = f"veg_{date_str}_p{i:02d}.png"
        out_png = PAGES_DIR / filename
        tmp = out_png.with_suffix(".tmp.png")
        img.save(str(tmp), "PNG")
        tmp.replace(out_png)
        out_files.append(out_png)

    return out_files

def main():
    taipei = timezone(timedelta(hours=8))
    now_dt = datetime.now(taipei)
    now_tpe = now_dt.strftime("%Y-%m-%d %H:%M:%S %z")
    date_str = now_dt.strftime("%Y-%m-%d")  # 今天（台北日期）

    state = load_json(STATE_PATH)
    prev_hash = state.get("veg_pdf_sha256", "")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        ok = False
        for _ in range(3):  # retry 3 次
            ok = try_download_veg_pdf(page)
            if ok:
                break

        browser.close()

    if not ok or not VEG_PDF.exists():
        state.update({
            "time_taipei": now_tpe,
            "status": "veg_download_failed",
        })
        save_json(STATE_PATH, state)
        return

    veg_hash = sha256_file(VEG_PDF)

    # PDF 沒變就不更新（避免一直 commit）
    if veg_hash == prev_hash and MANIFEST_PATH.exists():
        state.update({
            "time_taipei": now_tpe,
            "status": "no_change",
            "veg_pdf_sha256": veg_hash,
        })
        save_json(STATE_PATH, state)
        return

    # 轉全頁（日期檔名）
    pages = render_all_pages(VEG_PDF, date_str)

    # manifest：提供 Apps Script 知道「今天是哪天」與「今天有哪些檔」
    manifest = {
        "date": date_str,
        "generated_at_taipei": now_tpe,
        "veg_pdf_sha256": veg_hash,
        "dpi": DPI,
        "pages": [p.name for p in pages],  # e.g. veg_2026-02-24_p01.png ...
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_tpe,
        "status": "updated",
        "veg_pdf_sha256": veg_hash,
        "page_count": len(pages),
        "date": date_str,
    })
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
