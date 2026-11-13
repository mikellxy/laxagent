"""Local, bounded text extraction. Called in a cancellable worker process."""
from pathlib import Path

MAX_TEXT_BYTES = 50 * 1024
TRUNCATED = "\n[文本已截断，最多返回 50 KiB；可使用 ask_appendices 查询附件。]"


def _docx_content(container):
    from docx.table import Table
    for item in container.iter_inner_content():
        if isinstance(item, Table):
            for row in item.rows:
                seen = set()
                for cell in row.cells:
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    yield from _docx_content(cell)
                yield "\n"
        else:
            yield item.text + "\n"


def _slide_content(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _slide_content(shape.shapes)
        elif shape.has_text_frame:
            yield shape.text + "\n"
        elif shape.has_table:
            for row in shape.table.rows:
                yield "\t".join(cell.text for cell in row.cells) + "\n"


def _parts(path: Path):
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".csv", ".html", ".htm"):
        with path.open(encoding="utf-8-sig") as source:
            while chunk := source.read(8192):
                yield chunk
    elif suffix == ".pdf":
        from pypdf import PdfReader
        with path.open("rb") as source:
            reader = PdfReader(source)
            yield "[仅提取 PDF 文本，不含图像/OCR 内容或完整排版；无文字页可使用 ask_appendices。]\n"
            found = False
            for index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                found = found or bool(text.strip())
                yield f"\n第 {index} 页\n" + (text if text.strip() else "[本页无可提取文字，可能为空白或扫描页。]") + "\n"
            if not found:
                raise ValueError("no extractable text")
    elif suffix == ".docx":
        from docx import Document
        yield "[正文段落与表格文本；不含图片、页眉页脚、批注等内容。]\n"
        yield from _docx_content(Document(path))
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        values = load_workbook(path, read_only=True, data_only=True)
        try:
            formulas = load_workbook(path, read_only=True, data_only=False)
            try:
                yield "[公式值来自文件缓存，未重新计算；无缓存时显示公式。]\n"
                for sheet in formulas:
                    yield f"\n工作表：{sheet.title}\n"
                    for row, cached_row in zip(sheet.iter_rows(), values[sheet.title].iter_rows()):
                        for cell, cached in zip(row, cached_row):
                            value = cached.value if cached.value is not None else cell.value
                            if value is not None:
                                yield f"{cell.coordinate}: {value}\n"
            finally:
                formulas.close()
        finally:
            values.close()
    elif suffix == ".pptx":
        from pptx import Presentation
        yield "[幻灯片文字与表格，按文档形状顺序；不含图片、备注或完整视觉排版。]\n"
        for index, slide in enumerate(Presentation(path).slides, 1):
            yield f"\n幻灯片 {index}\n"
            yield from _slide_content(slide.shapes)
    else:
        raise ValueError("unsupported text format")


def extract_text(path: str) -> dict:
    """Keep parser exceptions and source paths out of persisted tool output."""
    parts = _parts(Path(path))
    content = bytearray()
    try:
        for part in parts:
            encoded = part.encode("utf-8")
            remaining = MAX_TEXT_BYTES - len(content)
            content.extend(encoded[:remaining])
            if len(encoded) > remaining:
                marker = TRUNCATED.encode("utf-8")
                text = bytes(content[:MAX_TEXT_BYTES - len(marker)]).decode("utf-8", errors="ignore")
                return {"content": text + TRUNCATED}
        text = content.decode("utf-8")
        return {"content": text} if text.strip() else {"error_code": "APPENDIX_NO_TEXT"}
    except OSError:
        return {"error_code": "APPENDIX_READ_FAILED"}
    except Exception:
        return {"error_code": "APPENDIX_EXTRACTION_FAILED"}
    finally:
        parts.close()
