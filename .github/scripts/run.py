import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from pdf2image import convert_from_path

URL = "https://www.tapmc.com.tw/Pages/Trans/Price2"
OUT = Path("docs")
OUT.mkdir(parents=True, exist_ok=True)

STATE_PATH = OUT / "state.json"
VEG_PDF = OUT / "veg.pdf"
FRUIT_PDF = OUT / "fruit.pdf"
VEG_PNG = OUT / "latest_veg.png"
FRUIT_PNG = OUT / "latest_fruit.png"

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def safe_convert_first_page(pdf_path: Path, out_png: Path):
    images = convert_from_path(str(pdf_path), first_page=1, last_page=1, dpi=200)
    tmp = out_png.with_suffix(".tmp.png")
    images[0].save(str(tmp), "PNG")
    tmp.replace(out_png)

def try_download(page, kind_text: str, out_pdf: Path) -> bool:
    """
    回傳 True/False 表示是否成功下載
    """
    page.goto(URL, wait_until="networkidle")

    # 嘗試切換 tab（若有）
    try:
        page.get_by_text(kind_text).first.click(timeout=3000)
        page.wait_for_timeout(600)
    except Exception:
        pass

    # 嘗試按查詢
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
        d.value.save_as(str(out_pdf))
        return True
    except Exception:
        return False

def main():
    # 台北時間（UTC+8）寫進 state 方便對照
    taipei = timezone(timedelta(hours=8))
    now_tpe = datetime.now(taipei).strftime("%Y-%m-%d %H:%M:%S %z")

    state = load_state()
    prev = state.get("last_hash", "")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # retry 3 次（網站慢/偶發）
        ok_veg = ok_fruit = False
        for _ in range(3):
            ok_veg = try_download(page, "蔬菜", VEG_PDF)
            if ok_veg:
                break

        for _ in range(3):
            ok_fruit = try_download(page, "水果", FRUIT_PDF)
            if ok_fruit:
                break

        browser.close()

    if not (ok_veg and ok_fruit):
        # 下載沒成功：不覆蓋舊圖，只更新 state 方便看
        state.update({
            "time_taipei": now_tpe,
            "status": "download_failed",
            "veg_ok": ok_veg,
            "fruit_ok": ok_fruit,
        })
        save_state(state)
        return

    # 計算本次 PDF hash（用來判斷有沒有新資料）
    veg_hash = sha256_file(VEG_PDF)
    fruit_hash = sha256_file(FRUIT_PDF)
    combined = veg_hash + "|" + fruit_hash

    # 若與上次一樣，代表資料沒變：不轉圖、不 commit（省資源）
    if combined == prev and VEG_PNG.exists() and FRUIT_PNG.exists():
        state.update({
            "time_taipei": now_tpe,
            "status": "no_change",
            "last_hash": combined,
        })
        save_state(state)
        return

    # 轉圖（先寫 tmp 再 replace，避免產出半張圖）
    safe_convert_first_page(VEG_PDF, VEG_PNG)
    safe_convert_first_page(FRUIT_PDF, FRUIT_PNG)

    state.update({
        "time_taipei": now_tpe,
        "status": "updated",
        "last_hash": combined,
        "veg_pdf_sha256": veg_hash,
        "fruit_pdf_sha256": fruit_hash,
    })
    save_state(state)

if __name__ == "__main__":
    main()
