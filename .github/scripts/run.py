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

# 你擔心模糊：提高 DPI 會更清楚但檔案會變大
DPI = 220  # 建議 200~250；如果你覺得還不夠清楚，可改 240

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

def render_all_pages(pdf_path: Path) -> list[Path]:
    # 轉所有頁
    images = convert_from_path(str(pdf_path), dpi=DPI)
    out_files: list[Path] = []

    # 先輸出到 tmp，再 replace，避免產生半張圖
    for i, img in enumerate(images, start=1):
        filename = f"veg_p{i:02d}.png"
        out_png = PAGES_DIR / filename
        tmp = out_png.with_suffix(".tmp.png")
        img.save(str(tmp), "PNG")
        tmp.replace(out_png)
        out_files.append(out_png)

    return out_files

def clean_extra_pages(keep: set[str]):
    # 如果今天頁數變少，把舊的多餘頁刪掉（避免 LINE 推到舊頁）
    for p in PAGES_DIR.glob("veg_p*.png"):
        if p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass

def main():
    taipei = timezone(timedelta(hours=8))
    now_tpe = datetime.now(taipei).strftime("%Y-%m-%d %H:%M:%S %z")

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

    # 轉全頁
    pages = render_all_pages(VEG_PDF)
    keep_names = {p.name for p in pages}
    clean_extra_pages(keep_names)

    # manifest：提供 Apps Script 知道有哪些頁面、用哪個 hash 判斷更新
    manifest = {
        "generated_at_taipei": now_tpe,
        "veg_pdf_sha256": veg_hash,
        "dpi": DPI,
        "pages": [p.name for p in pages],  # e.g. veg_p01.png, veg_p02.png...
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_tpe,
        "status": "updated",
        "veg_pdf_sha256": veg_hash,
        "page_count": len(pages),
    })
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
