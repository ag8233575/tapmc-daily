import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta, time as dtime

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

# 圖片清晰度：越高越清楚、檔案越大
DPI = 220  # 200~250 建議範圍

# 台北時區
TPE_TZ = timezone(timedelta(hours=8))

# 允許更新的時間窗（台北時間）
WINDOW_START = dtime(7, 30)
WINDOW_END = dtime(8, 10)

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

def now_tpe_dt() -> datetime:
    return datetime.now(TPE_TZ)

def now_tpe_str() -> str:
    return now_tpe_dt().strftime("%Y-%m-%d %H:%M:%S %z")

def in_window(dt: datetime) -> bool:
    t = dt.time()
    return (t >= WINDOW_START) and (t <= WINDOW_END)

def try_download_veg_pdf(page) -> bool:
    page.goto(URL, wait_until="networkidle")

    # 1️⃣ 選擇「蔬菜」(用 select_option 才是正確操作)
    try:
        # 找到包含「蔬菜」選項的 select
        selects = page.locator("select")
        found = False

        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                sel.select_option(label="蔬菜")
                found = True
                break
            except Exception:
                continue

        if not found:
            print("⚠️ 找不到蔬菜選項")
            return False

        page.wait_for_timeout(800)

    except Exception as e:
        print(f"選取蔬菜失敗: {e}")
        return False

    # 2️⃣ 點查詢並等待資料真正載入完成
    try:
        # 🤖 明確指定尋找名稱為「查詢」的「按鈕 (button)」
        page.get_by_role("button", name="查詢", exact=True).first.click(timeout=5000)

        # 等待網路請求完全結束
        page.wait_for_load_state("networkidle")

        # 額外給前端渲染時間
        page.wait_for_timeout(1500)

    except Exception as e:
        print(f"點擊查詢失敗: {e}")
        return False

    # 3️⃣ 等待下載 PDF
    try:
        with page.expect_download(timeout=30000) as d:
            try:
                # 🤖 明確指定尋找名稱為「下載PDF檔」的「按鈕 (button)」
                page.get_by_role("button", name="下載PDF檔", exact=True).first.click(timeout=5000)
            except Exception:
                # 備案：萬一名字微調，可以點包含 PDF 的按鈕
                page.get_by_role("button", name="PDF").first.click(timeout=5000)

        d.value.save_as(str(VEG_PDF))
        return True

    except Exception as e:
        print(f"下載失敗: {e}")
        return False

def pdf_looks_like_template(pdf_path: Path) -> tuple[bool, dict]:
    """
    判斷下載到的 PDF 是否「像模板/空白（尚未出資料）」。
    """
    info = {}
    try:
        # 只轉第一頁，低 DPI 省時間
        imgs = convert_from_path(str(pdf_path), dpi=120, first_page=1, last_page=1)
        if not imgs:
            return True, {"reason": "no_page_rendered"}

        img = imgs[0].convert("L")  # grayscale
        w, h = img.size

        # 下採樣，加速統計
        img_small = img.resize((max(200, w // 8), max(200, h // 8)))
        px = img_small.getdata()

        # 深色判斷門檻：0(黑)~255(白)
        dark = sum(1 for v in px if v < 230)
        total = len(px)
        ratio = dark / total if total else 0

        info = {"dark_ratio": round(ratio, 6), "sample_size": [img_small.size[0], img_small.size[1]]}

        # 門檻可調：如果你之後發現「真的有資料但被誤判模板」就把 0.012 調更低
        is_template = ratio < 0.012
        return is_template, info

    except Exception as e:
        return False, {"reason": "template_check_error", "error": str(e)}

def render_all_pages(pdf_path: Path) -> list[Path]:
    """
    轉全頁成 PNG
    """
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

def main():
    now_dt = now_tpe_dt()
    now_str = now_tpe_str()
    today_str = now_dt.strftime("%Y-%m-%d")

    state = load_json(STATE_PATH)

    # 🚨🚨🚨 【測試期間先註解掉時間鎖】 🚨🚨🚨
    # 等您確認手動測試一切順利後，再把下面這幾行的 '#' 拿掉
    # ----------------------------------------------------
    # if not in_window(now_dt):
    #     print("⏳ 目前不在允許的時間窗內，跳過執行。")
    #     state.update({
    #         "time_taipei": now_str,
    #         "status": "skip_outside_window",
    #         "date": today_str,
    #         "detail": "skip_run_outside_0730_0810",
    #     })
    #     save_json(STATE_PATH, state)
    #     return
    # ----------------------------------------------------

    print("🚀 開始執行抓取流程...")
    is_success = False  
    last_detail = ""
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = context.new_page()

        for attempt in range(1, 6):  # 嘗試 5 次
            print(f"--- 嘗試第 {attempt} 次下載 ---")
            ok = try_download_veg_pdf(page)
            if not ok or not VEG_PDF.exists():
                last_detail = f"attempt_{attempt}_download_failed"
                print(f"⚠️ 下載失敗或檔案不存在，等待 2 秒後重試...")
                page.wait_for_timeout(2000)
                continue

            # 檢查是否模板/未就緒
            is_tmpl, info = pdf_looks_like_template(VEG_PDF)
            if is_tmpl:
                last_detail = f"attempt_{attempt}_pdf_template_no_data"
                print(f"⚠️ 抓到空白模板 (深色比例 {info.get('dark_ratio')})，等待 3 秒後重試...")
                page.wait_for_timeout(3000) 
                continue

            # 不是模板，代表成功取得有效資料！
            print(f"✅ 成功取得有效 PDF！(深色比例 {info.get('dark_ratio')})")
            is_success = True
            break

        context.close()
        browser.close()

    # 迴圈結束後，檢查是否真的成功
    if not is_success:
        print(f"❌ 5 次嘗試都失敗，最後狀態: {last_detail}")
        state.update({
            "time_taipei": now_str,
            "status": "not_ready_or_template" if "template" in last_detail else "veg_download_failed",
            "date": today_str,
            "detail": last_detail,
        })
        save_json(STATE_PATH, state)
        return  # 終止程式，保護您的舊圖片不被錯誤覆蓋

    # 處理成功的 PDF
    veg_hash = sha256_file(VEG_PDF)
    prev_hash = state.get("veg_pdf_sha256", "")
    
    # 如果 PDF hash 沒變且 manifest 存在：不更新
    if veg_hash == prev_hash and MANIFEST_PATH.exists():
        print("ℹ️ PDF 內容無變動，跳過轉檔與更新。")
        state.update({
            "time_taipei": now_str,
            "status": "no_change",
            "date": today_str,
            "veg_pdf_sha256": veg_hash,
        })
        save_json(STATE_PATH, state)
        return

    # 轉 PNG
    print("🖼️ 開始將 PDF 轉成 PNG...")
    pages = render_all_pages(VEG_PDF)
    keep_names = {p.name for p in pages}
    clean_extra_pages(keep_names)

    manifest = {
        "generated_at_taipei": now_str,
        "date": today_str,
        "veg_pdf_sha256": veg_hash,
        "dpi": DPI,
        "pages": [p.name for p in pages],
    }
    save_json(MANIFEST_PATH, manifest)

    state.update({
        "time_taipei": now_str,
        "status": "updated",
        "date": today_str,
        "veg_pdf_sha256": veg_hash,
        "page_count": len(pages),
    })
    save_json(STATE_PATH, state)
    print("🎉 所有流程更新完成！")

if __name__ == "__main__":
    main()
