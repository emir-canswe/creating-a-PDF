# -*- coding: utf-8 -*-
"""
Akıllı Metin → PDF Dönüştürücü — Flask uygulaması.
Metni analiz eder, başlık/alt başlık/paragraf ayrımı yapar ve ReportLab ile PDF üretir.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

# -----------------------------------------------------------------------------
# Uygulama yolları
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated_pdfs"
GENERATED_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "gelistirme-icin-degistirin-uretimde-env-kullanin")


class BlockType(str, Enum):
    """Metin bloğu türü: başlık, alt başlık veya düz paragraf."""

    HEADING = "heading"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"


@dataclass
class TextBlock:
    """Analiz sonucu tek bir mantıksal metin parçasını temsil eder."""

    kind: BlockType
    text: str


def _escape_paragraph_xml(text: str) -> str:
    """
    ReportLab Paragraph içeriği için XML özel karakterlerini kaçırır.
    Böylece kullanıcı metnindeki < ve & karakterleri PDF'i bozmaz.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _is_mostly_uppercase_line(line: str) -> bool:
    """
    Satırın başlık adayı olup olmadığını kontrol eder.
    Harflerin çoğu büyük harf ise ve yeterli uzunluktaysa True döner.
    """
    stripped = line.strip()
    if len(stripped) < 3:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 3:
        return False
    upper_count = sum(1 for c in letters if c.isupper())
    return (upper_count / len(letters)) >= 0.85


def _looks_like_subheading(line: str) -> bool:
    """
    Alt başlık ipuçları: Markdown ## veya kısa satır + iki nokta ile biter.
    """
    s = line.strip()
    if s.startswith("##") and len(s) > 2:
        return True
    if 2 <= len(s) <= 120 and s.endswith(":") and "\n" not in s:
        # Uzun cümlelerde yanlış pozitif azaltmak için kelime sayısı sınırı
        words = s.split()
        if len(words) <= 12:
            return True
    return False


def analyze_text_to_blocks(raw_text: str) -> List[TextBlock]:
    """
    Ham metni basit kurallarla yapılandırılmış bloklara ayırır (hafif NLP / sezgisel ayrıştırma).

    - Paragraflar genelde boş satırlarla ayrılır.
    - Tamamı büyük harfe yakın tek satırlar başlık sayılır (kalın yazılacak).
    - ## ile başlayan veya kısa ve ':' ile biten satırlar alt başlık (italik).
    - Diğer metinler gövde paragrafıdır.
    """
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    blocks: List[TextBlock] = []
    # Önce çift satır sonu ile parçalara böl
    sections = re.split(r"\n\s*\n", text)

    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = section.split("\n")

        # Tek satırlık bölüm: başlık / alt başlık / paragraf
        if len(lines) == 1:
            line = lines[0].strip()
            if _is_mostly_uppercase_line(line):
                blocks.append(TextBlock(BlockType.HEADING, line))
            elif _looks_like_subheading(line):
                clean = line.lstrip("#").strip() if line.startswith("##") else line
                blocks.append(TextBlock(BlockType.SUBHEADING, clean))
            else:
                blocks.append(TextBlock(BlockType.PARAGRAPH, line))
            continue

        # Çok satırlı: satır satır sınıflandır, ardışık paragrafları birleştir
        buf: List[str] = []

        def flush_paragraph() -> None:
            if buf:
                blocks.append(TextBlock(BlockType.PARAGRAPH, " ".join(buf).strip()))
                buf.clear()

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                flush_paragraph()
                continue
            if _is_mostly_uppercase_line(line):
                flush_paragraph()
                blocks.append(TextBlock(BlockType.HEADING, line))
            elif _looks_like_subheading(line):
                flush_paragraph()
                clean = line.lstrip("#").strip() if line.startswith("##") else line
                blocks.append(TextBlock(BlockType.SUBHEADING, clean))
            else:
                buf.append(line)
        flush_paragraph()

    return blocks


def _find_ttf_font(preferred_names: List[str]) -> Optional[Path]:
    """
    Sistemde Türkçe destekli TrueType font dosyası arar (Windows / Linux / macOS).
    Bulunamazsa None döner; o zaman ReportLab varsayılan fontları kullanılır.
    """
    candidates: List[Path] = []

    windir = os.environ.get("WINDIR")
    if windir:
        fonts_dir = Path(windir) / "Fonts"
        for name in preferred_names:
            candidates.append(fonts_dir / name)

    # Linux yaygın yollar
    linux_roots = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts/TTF"),
    ]
    for root in linux_roots:
        for name in preferred_names:
            candidates.append(root / name)

    # macOS
    mac_root = Path("/Library/Fonts")
    for name in preferred_names:
        candidates.append(mac_root / name)
        candidates.append(Path.home() / "Library/Fonts" / name)

    for p in candidates:
        if p.is_file():
            return p
    return None


def register_unicode_fonts() -> Tuple[str, str, str]:
    """
    PDF için normal, kalın ve italik font adlarını kaydeder.
    Türkçe karakterler için TTF tercih edilir.

    Dönüş: (font_regular, font_bold, font_italic) kayıtlı isimler.
    """
    # Arial / DejaVu / Liberation sırasıyla dene
    regular_file = _find_ttf_font(
        ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )
    bold_file = _find_ttf_font(
        ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
    )
    italic_file = _find_ttf_font(
        ["ariali.ttf", "DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf"]
    )

    reg_name = "AppFont"
    bold_name = "AppFont-Bold"
    italic_name = "AppFont-Italic"

    if regular_file:
        pdfmetrics.registerFont(TTFont(reg_name, str(regular_file)))
    else:
        reg_name = "Helvetica"

    if bold_file:
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_file)))
    else:
        bold_name = "Helvetica-Bold"

    if italic_file:
        pdfmetrics.registerFont(TTFont(italic_name, str(italic_file)))
    else:
        italic_name = "Helvetica-Oblique"

    return reg_name, bold_name, italic_name


def _build_theme_styles(
    theme: str,
    base_size: int,
    font_reg: str,
    font_bold: str,
    font_italic: str,
) -> dict:
    """
    'minimal' veya 'akademik' tema için ParagraphStyle sözlüğü üretir.
    """
    is_academic = theme == "academic"
    title_size = base_size + (8 if is_academic else 10)
    heading_size = base_size + 4
    sub_size = base_size + 1

    title_align = TA_CENTER if is_academic else TA_LEFT
    body_align = TA_JUSTIFY if is_academic else TA_LEFT
    title_space_after = 16 if is_academic else 8
    meta_color = colors.HexColor("#333333") if is_academic else colors.HexColor("#555555")

    styles = {
        "title": ParagraphStyle(
            name="CustomTitle",
            fontName=font_bold,
            fontSize=title_size,
            leading=title_size * 1.2,
            alignment=title_align,
            spaceAfter=title_space_after,
        ),
        "meta": ParagraphStyle(
            name="CustomMeta",
            fontName=font_reg,
            fontSize=base_size - 1,
            leading=(base_size - 1) * 1.2,
            alignment=TA_CENTER if is_academic else TA_LEFT,
            textColor=meta_color,
            spaceAfter=18 if is_academic else 12,
        ),
        "heading": ParagraphStyle(
            name="CustomHeading",
            fontName=font_bold,
            fontSize=heading_size,
            leading=heading_size * 1.15,
            spaceBefore=14,
            spaceAfter=8,
            textColor=colors.black,
        ),
        "subheading": ParagraphStyle(
            name="CustomSub",
            fontName=font_italic,
            fontSize=sub_size,
            leading=sub_size * 1.2,
            spaceBefore=10,
            spaceAfter=6,
            textColor=colors.HexColor("#222222"),
        ),
        "body": ParagraphStyle(
            name="CustomBody",
            fontName=font_reg,
            fontSize=base_size,
            leading=base_size * 1.35,
            alignment=body_align,
            spaceAfter=10,
        ),
    }
    return styles


def _cover_page_flowables(
    doc_title: str,
    author: str,
    doc_date: str,
    styles: dict,
) -> List:
    """
    İsteğe bağlı kapak sayfası için ortalanmış başlık/yazar/tarih düzenini oluşturur.
    """
    story: List = [Spacer(1, 6 * cm)]
    story.append(Paragraph(_escape_paragraph_xml(doc_title), styles["title"]))
    story.append(Spacer(1, 1.5 * cm))
    meta_bits = []
    if author:
        meta_bits.append(_escape_paragraph_xml(author))
    if doc_date:
        meta_bits.append(_escape_paragraph_xml(doc_date))
    meta_text = " · ".join(meta_bits) if meta_bits else ""
    if meta_text:
        meta_style = ParagraphStyle(
            name="CoverMeta",
            parent=styles["meta"],
            fontSize=styles["meta"].fontSize + 1,
        )
        story.append(Paragraph(meta_text, meta_style))
    story.append(PageBreak())
    return story


def generate_pdf(
    raw_text: str,
    pdf_title: str,
    author: str,
    include_date: bool,
    manual_date: Optional[str],
    font_choice: str,
    font_size: int,
    theme: str,
    add_cover: bool,
    output_path: Path,
) -> None:
    """
    Analiz edilmiş metin ve kullanıcı seçenekleriyle PDF dosyasını diske yazar.
    Sayfa taşmaları SimpleDocTemplate ile otomatik yönetilir.
    """
    font_reg, font_bold, font_italic = register_unicode_fonts()

    # Kullanıcı font seçimi: mümkünse sistem TTF ile eşle
    user_map = {
        "Arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf"),
        "Times New Roman": ("times.ttf", "timesbd.ttf", "timesi.ttf"),
        "Courier New": ("cour.ttf", "courbd.ttf", "couri.ttf"),
    }
    if font_choice in user_map:
        r, b, i = user_map[font_choice]
        pr = _find_ttf_font([r])
        pb = _find_ttf_font([b])
        pi = _find_ttf_font([i])
        if pr:
            pdfmetrics.registerFont(TTFont("UserReg", str(pr)))
            font_reg = "UserReg"
        if pb:
            pdfmetrics.registerFont(TTFont("UserBold", str(pb)))
            font_bold = "UserBold"
        if pi:
            pdfmetrics.registerFont(TTFont("UserItalic", str(pi)))
            font_italic = "UserItalic"

    styles = _build_theme_styles(theme, font_size, font_reg, font_bold, font_italic)

    # Otomatik tarih: bugün; manuel mod: formdaki tarih alanı (YYYY-MM-DD → GG.AA.YYYY)
    if include_date:
        doc_date = date.today().strftime("%d.%m.%Y")
    else:
        doc_date = (manual_date or "").strip()

    blocks = analyze_text_to_blocks(raw_text)

    margins = (
        2.2 * cm,
        2.2 * cm,
        2.2 * cm,
        2.2 * cm,
    )
    if theme == "academic":
        margins = (2.5 * cm, 2.5 * cm, 2.5 * cm, 2.5 * cm)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=margins[0],
        rightMargin=margins[1],
        topMargin=margins[2],
        bottomMargin=margins[3],
        title=pdf_title,
        author=author or "",
    )

    story: List = []

    if add_cover:
        story.extend(_cover_page_flowables(pdf_title, author, doc_date, styles))
        # Kapakta başlık zaten var; içerikte yalnızca isteğe bağlı kısa meta
        meta_line_parts = []
        if author:
            meta_line_parts.append(f"Yazar: {_escape_paragraph_xml(author)}")
        if doc_date:
            meta_line_parts.append(f"Tarih: {_escape_paragraph_xml(doc_date)}")
        if meta_line_parts:
            story.append(Paragraph(" &nbsp;|&nbsp; ".join(meta_line_parts), styles["meta"]))
        story.append(Spacer(1, 0.5 * cm))
    else:
        story.append(Paragraph(_escape_paragraph_xml(pdf_title), styles["title"]))
        meta_line_parts = []
        if author:
            meta_line_parts.append(f"Yazar: {_escape_paragraph_xml(author)}")
        if doc_date:
            meta_line_parts.append(f"Tarih: {_escape_paragraph_xml(doc_date)}")
        if meta_line_parts:
            story.append(Paragraph(" &nbsp;|&nbsp; ".join(meta_line_parts), styles["meta"]))
        story.append(Spacer(1, 0.4 * cm))

    for block in blocks:
        safe = _escape_paragraph_xml(block.text)
        if block.kind == BlockType.HEADING:
            story.append(Paragraph(safe, styles["heading"]))
        elif block.kind == BlockType.SUBHEADING:
            story.append(Paragraph(safe, styles["subheading"]))
        else:
            story.append(Paragraph(safe, styles["body"]))

    doc.build(story)


@app.route("/", methods=["GET"])
def index():
    """Ana sayfa: formu gösterir."""
    return render_template(
        "index.html",
        default_date=date.today().strftime("%Y-%m-%d"),
    )


@app.route("/generate", methods=["POST"])
def generate():
    """
    Form verisini alır, doğrular, PDF üretir ve indirme sayfasına yönlendirir.
    """
    raw_text = (request.form.get("content") or "").strip()
    pdf_title = (request.form.get("pdf_title") or "Belge").strip() or "Belge"
    author = (request.form.get("author") or "").strip()
    font_choice = request.form.get("font") or "Arial"
    theme = request.form.get("theme") or "minimal"
    add_cover = request.form.get("add_cover") == "on"
    include_date = request.form.get("auto_date") == "on"
    manual_date_raw = (request.form.get("manual_date") or "").strip()

    try:
        font_size = int(request.form.get("font_size") or 11)
        font_size = max(8, min(font_size, 24))
    except ValueError:
        font_size = 11

    if not raw_text:
        flash("Metin alanı boş olamaz. Lütfen dönüştürmek için metin girin.", "error")
        return redirect(url_for("index"))

    if theme not in ("minimal", "academic"):
        theme = "minimal"
    if font_choice not in ("Arial", "Times New Roman", "Courier New"):
        font_choice = "Arial"

    manual_date = ""
    if manual_date_raw:
        try:
            manual_date = datetime.strptime(manual_date_raw, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            manual_date = manual_date_raw

    file_id = uuid.uuid4().hex
    safe_name = f"{file_id}.pdf"
    out_path = GENERATED_DIR / safe_name

    try:
        generate_pdf(
            raw_text=raw_text,
            pdf_title=pdf_title,
            author=author,
            include_date=include_date,
            manual_date=manual_date or None,
            font_choice=font_choice,
            font_size=font_size,
            theme=theme,
            add_cover=add_cover,
            output_path=out_path,
        )
    except Exception as exc:  # noqa: BLE001 — kullanıcıya genel hata mesajı
        app.logger.exception("PDF oluşturma hatası")
        flash(
            f"PDF oluşturulurken bir hata oluştu: {exc!s}. Lütfen tekrar deneyin.",
            "error",
        )
        return redirect(url_for("index"))

    flash("PDF başarıyla oluşturuldu. Aşağıdan indirebilirsiniz.", "success")
    return redirect(url_for("download_page", filename=safe_name, title=pdf_title))


@app.route("/download/<filename>")
def download_page(filename: str):
    """
    Üretilen dosya adını doğrular ve indirme ekranını gösterir.
    """
    if not _is_safe_generated_filename(filename):
        flash("Geçersiz dosya adı.", "error")
        return redirect(url_for("index"))
    path = GENERATED_DIR / filename
    if not path.is_file():
        flash("Dosya bulunamadı veya süresi dolmuş olabilir.", "error")
        return redirect(url_for("index"))
    title = request.args.get("title") or "Belge"
    return render_template("download.html", filename=filename, doc_title=title)


@app.route("/file/<filename>")
def download_file(filename: str):
    """
    PDF dosyasını güvenli şekilde kullanıcıya gönderir.
    """
    if not _is_safe_generated_filename(filename):
        flash("Geçersiz dosya adı.", "error")
        return redirect(url_for("index"))
    path = GENERATED_DIR / filename
    if not path.is_file():
        flash("Dosya bulunamadı.", "error")
        return redirect(url_for("index"))
    return send_file(
        path,
        as_attachment=True,
        download_name="belge.pdf",
        mimetype="application/pdf",
    )


def _is_safe_generated_filename(name: str) -> bool:
    """
    Sadece bizim ürettiğimiz uuid.pdf formatına izin verir (path traversal önleme).
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    return bool(re.fullmatch(r"[0-9a-f]{32}\.pdf", name))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
