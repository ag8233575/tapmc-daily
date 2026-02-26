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
WEBSHOT = OUT / "veg_web.png"

DPI = 220  # PDF -> PNG 清晰度

def sha256_file(p: Path) -> str:
    import hashlib
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
    # 2026-02-24 -> 115/02/24
    roc_year = dt_tpe.year - 1911
    return f"{roc_year:03d}/{dt_tpe.month:02d}/{dt_tpe.day:02d}"

def click_if_exists(page, *, role=None, name=None, text=None, timeout=2000) -> bool:
    try:
        if role and name:
            page.get_by_role(role, name=name).click(timeout=timeout)
        elif text:
            page.get_by_text(text).first.click(timeout=timeout)
        else:
            return False
        return True
    except Exception:
        return False

def fill_date_and_query(page, roc_date: str):
    # 進頁面
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    # 選「蔬菜」（你手機畫面有下拉）
    # 這裡用文字靠近法：先點下拉，再點選項
    # 如果站改版，至少不會整個崩掉，後面還有截圖備援
    try:
        # 點到類別下拉（通常在「果菜類別」附近）
        page.get_by_text("果菜類別").first.wait_for(timeout=5000)
    except Exception:
        pass

    # 嘗試選蔬菜（多種方式）
    click_if_exists(page, text="蔬菜", timeout=2000)

    # 填日期（找得到就填）
    # 常見是 input[type=text] 或 input 帶日期圖示
    filled = False
    try:
        # 優先找「查詢日期」附近的 input
        page.get_by_text("查詢日期").first.wait_for(timeout=3000)
        # 找下一個 input
        inp = page.locator("input").first
        # 更保守：挑最像日期那格（頁面通常第一個 input 就是日期）
        inp.click(timeout=2000)
        # 全選覆蓋
        inp.press("Control+A")
        inp.type(roc_date, delay=30)
        filled = True
    except Exception:
        pass

    # 有些頁面要按「查詢」
    click_if_exists(page, role="button", name="查詢", timeout=3000) or click_if_exists(page, text="查詢", timeout=3000)
    page.wait_for_timeout(1500)

    # 等表格真的出現（避免 PDF 空白）
    # 用一個「蔬菜表格常見字」當旗標（例如 LA、甘藍）
    # 若站上文字不固定，這段最多等 10 秒，等不到也會走備援。
    try:
        page.get_by_text("LA").first.wait_for(timeout=10000)
    except Exception:
        # 不一定會有 LA，但至少等一下網路
        page.wait_for_timeout(2000)

    return filled

def download_pdf(page) -> bool:
    # 下載 PDF
    try:
        with page.expect_download(timeout=30000) as d:
            # 你畫面有「下載PDF檔」
            if not (click_if_exists(page, role="button", name="下載PDF檔", timeout=5000) or click_if_exists(page, text="下載PDF檔", timeout=5000)):
                # fallback：有些站按鈕不是 button
                page.get_by_text("PDF").first.click(timeout=5000)
        d.value.save_as(str(VEG_PDF))
        return True
    except Exception:
        return False

def pdf_is_blank(pdf_path: Path) -> bool:
    """
    把 PDF 第1頁轉低解析度圖，判斷「幾乎全白」就視為空白。
    """
    try:
        imgs = convert_from_path(str(pdf_path), dpi=80, first_page=1, last_page=1)
        if not imgs:
            return True
        img = imgs[0].convert("L")  # 灰階
        # 算白色比例（>250 視為白）
        import numpy as np
        arr = np.array(img)
        white_ratio = (arr > 250).mean()
        # 超過 0.985 幾乎全白
        return white_ratio > 0.985
    except Exception:
        return True

def render_pdf_all_pages(pdf_path: Path) -> list[Path]:
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

def take_webshot(page) -> bool:
    """
    直接截圖頁面（可當 PDF 空白備援）
    """
    try:
        page.wait_for_timeout(800)
        page.screenshot(path=str(WEBSHOT), full_page=True)
        return WEBSHOT.exists() and WEBSHOT.stat().st_size > 10_000
    except Exception:
        return False

def main():
    taipei = timezone(timedelta(hours=8))
    now_dt = datetime.now(taipei)
    now_tpe = now_dt.strftime("%Y-%m-%d %H:%M:%S %z")
    today_tpe = now_dt.strftime("%Y-%m-%d")
    roc_today = roc_date_str(now_dt)

    state = load_json(STATE_PATH)
    prev_hash = state.get("veg_pdf_sha256", "")

    status = "init"
    source = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Taipei"
        )
        page = context.new_page()

        ok = False
        filled = False

        # retry 3 次：每次都重新 query，再抓 PDF
        for attempt in range(3):
            try:
                filled = fill_date_and_query(page, roc_today)
                ok = download_pdf(page)
                if ok and VEG_PDF.exists() and VEG_PDF.stat().st_size > 10_000:
                    # 檔案夠大才像真的有內容
                    if not pdf_is_blank(VEG_PDF):
                        status = "pdf_ok"
                        source = "pdf"
                        break
                    else:
                        status = "pdf_blank"
                else:
                    status = "pdf_download_failed"
            except Exception:
                status = "exception"
            # 等一下再試
            page.wait_for_timeout(1200)

        # 如果 PDF 失敗或空白 -> 用網頁截圖備援
        if source != "pdf":
            # 再確保頁面是在今天資料
            fill_date_and_query(page, roc_today)
            if take_webshot(page):
                status = "webshot_ok"
                source = "webshot"
            else:
                status = "webshot_failed"
                source = "none"

        context.close()
        browser.close()

    # 若兩者都失敗
    if source == "none":
        state.update({
            "time_taipei": now_tpe,
            "status": status,
        })
        save_json(STATE_PATH, state)
        return

    manifest = {
        "date": today_tpe,
        "generated_at_taipei": now_tpe,
        "source": source,  # pdf 或 webshot
        "dpi": DPI,
        "pages": [],
    }

    # PDF 成功：轉全頁
    if source == "pdf":
        veg_hash = sha256_file(VEG_PDF)

        # PDF 沒變就不更新（省 commit）
        if veg_hash == prev_hash and MANIFEST_PATH.exists():
            state.update({
                "time_taipei": now_tpe,
                "status": "no_change",
                "veg_pdf_sha256": veg_hash,
            })
            save_json(STATE_PATH, state)
            return

        pages = render_pdf_all_pages(VEG_PDF)
        keep_names = {p.name for p in pages}
        clean_extra_pages(keep_names)

        manifest.update({
            "veg_pdf_sha256": veg_hash,
            "pages": [p.name for p in pages],
        })

        state.update({
            "time_taipei": now_tpe,
            "status": status,
            "veg_pdf_sha256": veg_hash,
            "page_count": len(pages),
        })
        save_json(STATE_PATH, state)
        save_json(MANIFEST_PATH, manifest)
        return

    # webshot 成功：當作單頁輸出 veg_p01.png（讓 Apps Script 不用改）
    # 把 veg_web.png 複製成 veg_pages/veg_p01.png
    out_png = PAGES_DIR / "veg_p01.png"
    img = Image.open(WEBSHOT)
    img.save(out_png, "PNG")

    # 清掉多餘頁，避免推到舊頁
    clean_extra_pages({"veg_p01.png"})

    # 用 webshot 的 hash 做更新判斷（避免一直 commit）
    veg_hash = sha256_file(out_png)

    if veg_hash == prev_hash and MANIFEST_PATH.exists():
        state.update({
            "time_taipei": now_tpe,
            "status": "no_change",
            "veg_pdf_sha256": veg_hash,
        })
        save_json(STATE_PATH, state)
        return

    manifest.update({
        "veg_pdf_sha256": veg_hash,
        "pages": ["veg_p01.png"],
    })

    state.update({
        "time_taipei": now_tpe,
        "status": status,
        "veg_pdf_sha256": veg_hash,
        "page_count": 1,
    })
    save_json(STATE_PATH, state)
    save_json(MANIFEST_PATH, manifest)

if __name__ == "__main__":
    main()
