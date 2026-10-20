#!/usr/bin/env python3
"""Build the single-page PulseFeed handout from its editable Markdown source."""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import reportlab
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[3]
BG = HexColor('#FAF9F6')
INK = HexColor('#25283D')
LAVENDER = HexColor('#DED8F8')
MINT = HexColor('#CEF0DE')
PEACH = HexColor('#FFDCC7')
MUTED = HexColor('#56596A')


def rich(text: str) -> str:
    value = html.escape(text)
    value = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', value)
    return re.sub(r'`(.*?)`', r'\1', value)


def parse(source: Path) -> tuple[str, str, dict[str, list[str]]]:
    parts = re.split(r'^## (.+)\n', source.read_text(), flags=re.M)
    lead = parts[0].strip().split('\n\n')
    return lead[0].removeprefix('# '), lead[1], {
        parts[i]: parts[i + 1].strip().split('\n\n')
        for i in range(1, len(parts), 2)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'docs/showcase/one-pager.md')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/pdf/pulsefeed-one-pager.pdf')
    parser.add_argument('--font-dir', type=Path, default=Path(reportlab.__file__).parent / 'fonts')
    args = parser.parse_args()
    for name, file in [('Body', 'Vera.ttf'), ('Body-Bold', 'VeraBd.ttf')]:
        pdfmetrics.registerFont(TTFont(name, str(args.font_dir / file)))
    pdfmetrics.registerFontFamily('Body', normal='Body', bold='Body-Bold')
    title, subtitle, content = parse(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(args.output), pagesize=A4, invariant=1)
    c.setTitle('PulseFeed | Project one-pager')
    c.setAuthor('Rok Kovac')
    c.setSubject('Architecture, verified evidence and remaining deployment work')
    w, h = A4
    margin = 39
    usable = w - 2 * margin
    body = ParagraphStyle('body', fontName='Body', fontSize=9.55, leading=13.5,
                          textColor=INK, alignment=TA_LEFT)
    small = ParagraphStyle('small', parent=body, fontSize=8.3, leading=11.4,
                           textColor=MUTED)
    heading = ParagraphStyle('heading', parent=body, fontName='Body-Bold',
                             fontSize=12.2, leading=16)

    def p(text: str, x: float, top: float, width: float, style=body) -> float:
        para = Paragraph(rich(text), style)
        _, height = para.wrap(width, h)
        para.drawOn(c, x, top - height)
        return top - height

    def paragraphs(texts: list[str], x: float, top: float, width: float,
                   gap: float = 7, style=body) -> float:
        for text in texts:
            top = p(text, x, top, width, style) - gap
        return top + gap

    c.setFillColor(BG)
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setFillColor(MUTED)
    c.setFont('Body-Bold', 8)
    c.drawString(margin, h - 35, 'PROJECT NOTE / 20 OCT 2026')
    # Three offset squares echo the app's restrained pixel accents.
    for dx, dy, color in [(0, 0, LAVENDER), (11, 0, MINT), (11, 11, PEACH)]:
        c.setFillColor(color)
        c.rect(w - margin - 23 + dx, h - 69 + dy, 10, 10, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont('Body-Bold', 33)
    c.drawString(margin, h - 82, title)
    y = p(subtitle, margin, h - 96, usable - 34,
          ParagraphStyle('subtitle', parent=body, fontSize=12.6, leading=17)) - 15
    y = paragraphs(content['What it does'], margin, y, usable, gap=6) - 16

    # A vector flow remains sharp at any zoom. Names match the editable source.
    labels = [part.strip() for part in content['Model flow'][0].split(' -> ')]
    gap = 17
    box_w = (usable - 3 * gap) / 4
    box_h = 39
    for i, (label, color) in enumerate(zip(labels, [PEACH, LAVENDER, MINT, PEACH])):
        x = margin + i * (box_w + gap)
        c.setFillColor(color)
        c.roundRect(x, y - box_h, box_w, box_h, 3, fill=1, stroke=0)
        p(label, x + 8, y - 12, box_w - 16,
          ParagraphStyle('flow', parent=body, fontName='Body-Bold', fontSize=9.15, leading=12))
        if i < 3:
            arrow_y = y - box_h / 2
            c.setStrokeColor(INK)
            c.setLineWidth(.8)
            c.line(x + box_w + 4, arrow_y, x + box_w + gap - 4, arrow_y)
            c.line(x + box_w + gap - 7, arrow_y + 2.5, x + box_w + gap - 4, arrow_y)
            c.line(x + box_w + gap - 7, arrow_y - 2.5, x + box_w + gap - 4, arrow_y)
    y = p(content['Model flow'][1], margin, y - box_h - 8, usable, small) - 18

    gutter = 24
    col_w = (usable - gutter) / 2
    left_x, right_x = margin, margin + col_w + gutter
    left_y = p('Practical choices', left_x, y, col_w, heading) - 8
    right_y = p('Safeguards', right_x, y, col_w, heading) - 8
    left_y = paragraphs(content['Practical choices'], left_x, left_y, col_w, gap=9)
    right_y = paragraphs(content['Safeguards'], right_x, right_y, col_w, gap=9)
    y = min(left_y, right_y) - 19

    proof_text = content['Real proof'][0]
    proof_style = ParagraphStyle('proof', parent=body, fontSize=9.55, leading=13.5)
    proof_para = Paragraph(rich(proof_text), proof_style)
    _, proof_h = proof_para.wrap(usable - 24, h)
    band_h = proof_h + 43
    c.setFillColor(MINT)
    c.roundRect(margin, y - band_h, usable, band_h, 4, fill=1, stroke=0)
    p('Real proof', margin + 12, y - 10, usable - 24, heading)
    p(proof_text, margin + 12, y - 33, usable - 24, proof_style)
    y -= band_h + 17
    y = p('Current status', margin, y, usable, heading) - 7
    y = p(content['Current status'][0], margin, y, usable) - 10
    y = p(content['Current status'][1], margin, y, usable, small)
    if y < 30:
        raise ValueError(f'Content exceeds the one-page layout: bottom={y:.1f} pt')
    c.showPage()
    c.save()
    word_count = len(re.findall(r'\S+', args.source.read_text()))
    print(f'Built {args.output}; source words={word_count}; content bottom={y:.1f}pt')


if __name__ == '__main__':
    main()
