from pathlib import Path
from playwright.sync_api import sync_playwright
from pdf2image import convert_from_path

URL = "https://www.tapmc.com.tw/Pages/Trans/Price2"
OUT = Path("docs")
OUT.mkdir(parents=True, exist_ok=True)

def download_pdf_by_text(page, text_keyword, out_pdf: Path):
    """
    進入北農頁面後，找「下載PDF檔」相關按鈕。
    由於網站可能分「蔬菜/水果」兩區，這裡採用：
    先嘗試點到對應區塊(蔬菜/水果)，再按下載。
    """
    page.goto(URL, wait_until="networkidle")

    # 嘗試先點「蔬菜」或「水果」切換（有些版本有 tab）
    try:
        page.get_by_text(text_keyword).first.click(timeout=3000)
        page.wait_for_timeout(800)
    except Exception:
        pass

    # 嘗試先按「查詢」（有些版本要查詢後才會出現下載）
    try:
        page.get_by_text("查詢").click(timeout=2000)
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # 下載 PDF：用「下載PDF檔」文字去找（不行就退而求其次找 PDF）
    with page.expect_download(timeout=30000) as d:
        try:
            page.get_by_text("下載PDF檔").first.click(timeout=5000)
        except Exception:
            # 有些網站按鈕文字不同，找包含 PDF 的連結/按鈕
            page.get_by_text("PDF").first.click(timeout=5000)

    d.value.save_as(str(out_pdf))

def pdf_first_page_to_png(pdf_path: Path, out_png: Path):
    images = convert_from_path(str(pdf_path), first_page=1, last_page=1, dpi=200)
    images[0].save(str(out_png), "PNG")

def main():
    veg_pdf = OUT / "veg.pdf"
    fruit_pdf = OUT / "fruit.pdf"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        download_pdf_by_text(page, "蔬菜", veg_pdf)
        download_pdf_by_text(page, "水果", fruit_pdf)

        browser.close()

    pdf_first_page_to_png(veg_pdf, OUT / "latest_veg.png")
    pdf_first_page_to_png(fruit_pdf, OUT / "latest_fruit.png")

if __name__ == "__main__":
    main()
