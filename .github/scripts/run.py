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

# 清晰度：220 通常夠清楚；若你覺得仍模糊可 240
DPI = 220

# 這些字通常「模板 PDF」不會出現；有出現才算真的有資料
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

def ymd_taipei():
    return taipei_now().strftime("%Y-%m-%d")

def roc_date_str(dt: datetime) -> str:
    # 115/02/24 這種格式
    roc_year = dt.year - 1911
    return f"{roc_year:03d}/{dt.month:02d}/{dt.day:02d}"

def pdf_text(pdf_path: Path) -> str:
    """
    用 poppler 的 pdftotext 抽文字（workflow 已裝 poppler-utils）
    """
    try:
        # -layout 讓表格字比較完整
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

def pdf_has_real_data(pdf_path: Path) -> bool:
    """
    判斷是否為「模板/尚未生成」：
    - 真正有資料的 PDF 通常會有 '蔬菜類' 和一些代碼/品名
    - 模板常常只有框線、少量固定字
    """
    txt = pdf_text(pdf_path)
    if not txt.strip():
        return False

    hit = 0
    for k in DATA_KEYWORDS:
        if k in txt:
            hit += 1

    # 命中 2 個以上關鍵字，通常就真的有資料
    return hit >= 2

def try_download_veg_pdf(page, expected_roc_date: str) -> bool:
    """
    走「下載 PDF」路徑。要做三件事：
    1) 選蔬菜
    2) 日期填今天（ROC）
    3) 按查詢（很多網站要查詢後報表才會生成）
    """
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    # 有些手機版/桌機版 DOM 不一樣，先盡量滾到按鈕區
    try:
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(300)
    except Exception:
        pass

    # 類別選蔬菜（下拉）
    # 你的截圖顯示有「果菜類別」下拉，值包含「蔬菜」
    try:
        # 先點選下拉，再點「蔬菜」
        page.get_by_text("蔬菜", exact=True).first.click(timeout=2000)
    except Exception:
        # 若上面點不到，改用 select 方式（保險）
        try:
            page.locator("select").first.select_option(label="蔬菜")
        except Exception:
            pass

    # 日期欄位填今天（ROC）
    # 你截圖日期欄像是 input；用 placeholder 或 type=date 可能不同
    # 這裡用「找第一個帶日期格式的 input」的保守策略
    try:
        # 優先找有 115/xx/xx 的欄位
        inputs = page.locator("input")
        n = inputs.count()
        filled = False
        for i in range(min(n, 8)):
            el = inputs.nth(i)
            val = ""
            try:
                val = el.input_value(timeout=300)
            except Exception:
                pass
            # 如果欄位目前像日期，就填
            if "/" in (val or "") or (val or "").isdigit():
                el.fill(expected_roc_date, timeout=2000)
                filled = True
                break
        if not filled:
            # 退而求其次：直接填第一個 input
            inputs.first.fill(expected_roc_date, timeout=2000)
    except Exception:
        pass

    # 點查詢（非常重要：很多站要查詢後 PDF 才會是「有資料」）
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
            # 你的頁面按鈕文字是「下載PDF檔」
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

def main():
    now = taipei_now()
    now_tpe_str = now.strftime("%Y-%m-%d %H:%M:%S %z")
    today_ymd = now.strftime("%Y-%m-%d")
    today_roc = roc_date_str(now)

    state = load_json(STATE_PATH)

    # =========
    # 下載 + 驗證（模板就等一下再重試）
    # =========
    max_attempts = 10
    sleep_seconds = 35  # 每次模板就等 35 秒再重試（總長約 6 分鐘）
    ok_download = False
    ok_data = False
    detail = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for attempt in range(1, max_attempts + 1):
            ok_download = try_download_veg_pdf(page, today_roc)
            if not ok_download or not VEG_PDF.exists():
                detail = f"attempt_{attempt}_pdf_download_failed"
                time.sleep(2)
                continue

            # 下載到檔後，立刻判斷是不是模板
            if pdf_has_real_data(VEG_PDF):
                ok_data = True
                detail = f"attempt_{attempt}_pdf_ok"
                break
            else:
                ok_data = False
                detail = f"attempt_{attempt}_pdf_template_no_data"
                # 模板：等一下再重抓
                time.sleep(sleep_seconds)

        browser.close()

    # =========
    # 若沒拿到「有資料」的 PDF：只更新 state，不動 manifest/pages
    # =========
    if not ok_data:
        state.update({
            "time_taipei": now_tpe_str,
            "status": "not_ready_or_template",
            "date": today_ymd,
            "detail": detail,
        })
        # 若有下載到檔，仍可存 hash 方便追查
        if VEG_PDF.exists():
            state["veg_pdf_sha256"] = sha256_file(VEG_PDF)
        save_json(STATE_PATH, state)
        return

    # =========
    # 有資料：才轉圖、更新 manifest
    # =========
    veg_hash = sha256_file(VEG_PDF)

    pages = render_all_pages(VEG_PDF)
    keep_names = {p.name for p in pages}
    clean_extra_pages(keep_names)

    manifest = {
        "date": today_ymd,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),  # 用抓取時間；你若要改成 PDF 內的更新時間也可再加
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
