import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from pdf2image import convert_from_path
from PIL import Image

URL = "https://www.tapmc.com.tw/Pages/Trans/Price2"

OUT = Path("docs")
OUT.mkdir(parents=True, exist_ok=True)

PAGES_DIR = OUT / "veg_pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = OUT / "state.json"
MANIFEST_PATH = OUT / "veg_manifest.json"
VEG_PDF = OUT / "veg.pdf"

DPI = 220  # 200~250

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

    # 切到蔬菜
    try:
        page.get_by_text("蔬菜").first.click(timeout=3000)
        page.wait_for_timeout(600)
    except Exception:
        pass

    # 點查詢（有些情況需要）
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

def render_pdf_pages(pdf_path: Path) -> list[Image.Image]:
    return convert_from_path(str(pdf_path), dpi=DPI)

def is_template_or_blank(img: Image.Image) -> bool:
    """
    判斷是否為「模板/空白」：
    1) 取灰階
    2) 計算「非白色像素比例」
    模板通常只有少量線條與標題，非白比例會很低
    """
    g = img.convert("L")
    # 壓小加速
    g = g.resize((800, int(800 * g.height / g.width)))
    pixels = g.getdata()
    # 250 以上視為近白
    non_white = sum(1 for p in pixels if p < 250)
    ratio = non_white / (len(pixels) or 1)
    # 這個門檻可微調：0.01~0.03
    return ratio < 0.015

def write_pages(images: list[Image.Image]) -> list[Path]:
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

def main():
    taipei = timezone(timedelta(hours=8))
    now_dt = datetime.now(taipei)
    now_tpe = now_dt.strftime("%Y-%m-%d %H:%M:%S %z")
    today = now_dt.strftime("%Y-%m-%d")

    state = load_json(STATE_PATH)
    prev_good_hash = state.get("veg_pdf_sha256_good", "")  # 只記「成功出表」的 hash

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        ok = False
        last_err = ""
        for attempt in range(1, 11):  # 這裡加到 10 次比較穩
            ok = try_download_veg_pdf(page)
            if not ok or not VEG_PDF.exists():
                last_err = f"attempt_{attempt}_download_failed"
                continue

            # 先轉第一頁做模板判斷（不要覆蓋掉舊檔）
            try:
                imgs = render_pdf_pages(VEG_PDF)
                if not imgs:
                    last_err = f"attempt_{attempt}_pdf_no_pages"
                    continue

                if is_template_or_blank(imgs[0]):
                    last_err = f"attempt_{attempt}_pdf_template_no_data"
                    # 等一下再重試
                    page.wait_for_timeout(1200)
                    continue

                # 有內容：成功
                browser.close()

                veg_hash = sha256_file(VEG_PDF)

                # 若 hash 沒變且 manifest 已存在，就不更新（省 commit）
                if veg_hash == prev_good_hash and MANIFEST_PATH.exists():
                    state.update({
                        "time_taipei": now_tpe,
                        "status": "no_change",
                        "veg_pdf_sha256_good": veg_hash,
                        "date": today,
                        "detail": "same_good_hash_skip",
                    })
                    save_json(STATE_PATH, state)
                    return

                # 寫出所有頁
                page_files = write_pages(imgs)
                keep_names = {p.name for p in page_files}
                clean_extra_pages(keep_names)

                manifest = {
                    "date": today,
                    "generated_at_taipei": now_tpe,
                    "veg_pdf_sha256": veg_hash,
                    "dpi": DPI,
                    "pages": [p.name for p in page_files],
                }
                save_json(MANIFEST_PATH, manifest)

                state.update({
                    "time_taipei": now_tpe,
                    "status": "updated",
                    "veg_pdf_sha256_good": veg_hash,
                    "page_count": len(page_files),
                    "date": today,
                    "detail": "pdf_ok",
                })
                save_json(STATE_PATH, state)
                return

            except Exception as e:
                last_err = f"attempt_{attempt}_exception_{type(e).__name__}"
                page.wait_for_timeout(1200)
                continue

        browser.close()

    # 10 次仍拿不到「有內容」PDF：寫 state + 寫 manifest（pages 空），但不要動 veg_pages
    manifest = {
        "date": today,
        "generated_at_taipei": now_tpe,
        "status": "not_ready_or_template",
        "dpi": DPI,
        "pages": [],
        "detail": last_err or "all_attempts_failed",
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_tpe,
        "status": "not_ready_or_template",
        "date": today,
        "detail": last_err or "all_attempts_failed",
    })
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
