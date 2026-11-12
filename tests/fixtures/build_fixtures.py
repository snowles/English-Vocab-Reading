"""生成测试用最小合法 EPUB fixture（离线、确定性）。

产物：
    epub2_basic.epub    EPUB 2.0 + NCX + 图片 + CSS（常规结构）
    epub2_ncx.epub      EPUB 2.0 变体：章节在子目录、纯 NCX 导航、内部锚点
    epub3_complex.epub  EPUB 3.0：nav + 既有脚注 + 复杂内联标签 + 跨节点短语

运行：`python tests/fixtures/build_fixtures.py`
所有时间戳固定为 1980-01-01，重复生成字节一致（ZIP 里 mimetype 首位且不压缩）。
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AL+2wAAAABJRU5ErkJggg=="
)

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _write_epub(path: Path, opf_path: str, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, b"application/epub+zip")

        info = zipfile.ZipInfo("META-INF/container.xml", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, CONTAINER.format(opf=opf_path).encode("utf-8"))

        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)


# ---------------------------------------------------------------------------
# EPUB 2 + 图片 + CSS
# ---------------------------------------------------------------------------

EPUB2_BASIC_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Basic Fixture</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:fixture-epub2-basic</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover" href="images/cover.png" media-type="image/png"/>
    <item id="ch1" href="chapter1.html" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.html" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

EPUB2_BASIC_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:fixture-epub2-basic"/></head>
  <docTitle><text>Basic Fixture</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1"><navLabel><text>One</text></navLabel><content src="chapter1.html"/></navPoint>
    <navPoint id="np2" playOrder="2"><navLabel><text>Two</text></navLabel><content src="chapter2.html"/></navPoint>
  </navMap>
</ncx>
"""

EPUB2_BASIC_CSS = """body { font-family: serif; margin: 1em; }
h1 { font-size: 1.4em; }
p { text-indent: 1.2em; }
"""

EPUB2_BASIC_CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
  <h1 id="c1">Chapter One</h1>
  <p><img src="images/cover.png" alt="cover"/></p>
  <p>The <em>art</em> of reading is not the same as the study of grammar.</p>
  <p>She wrote an article about the planet, and took part in the debate.</p>
  <p>Do not give up when the work becomes hard.</p>
  <p>They look forward to the holidays.</p>
  <p>Tom said the art's value is well-known.</p>
  <p>See <a href="chapter2.html">the study notes</a> for more.</p>
</body>
</html>
"""

EPUB2_BASIC_CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Two</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
  <h1 id="c2">Chapter Two</h1>
  <p>The child went home after the article was finished.</p>
  <p>About ten percent of the class will buy the book.</p>
  <p>We seek the truth in every part of the world.</p>
</body>
</html>
"""


def build_epub2_basic() -> Path:
    path = HERE / "epub2_basic.epub"
    _write_epub(
        path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": EPUB2_BASIC_OPF.encode("utf-8"),
            "OEBPS/toc.ncx": EPUB2_BASIC_NCX.encode("utf-8"),
            "OEBPS/style.css": EPUB2_BASIC_CSS.encode("utf-8"),
            "OEBPS/images/cover.png": PNG_1PX,
            "OEBPS/chapter1.html": EPUB2_BASIC_CH1.encode("utf-8"),
            "OEBPS/chapter2.html": EPUB2_BASIC_CH2.encode("utf-8"),
        },
    )
    return path


# ---------------------------------------------------------------------------
# EPUB 2 变体：子目录章节 + 纯 NCX + 内部锚点
# ---------------------------------------------------------------------------

EPUB2_NCX_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>NCX Fixture</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:fixture-epub2-ncx</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="c1" href="text/part1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/part2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="text/part3.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/>
    <itemref idref="c2"/>
    <itemref idref="c3"/>
  </spine>
</package>
"""

EPUB2_NCX_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:fixture-epub2-ncx"/></head>
  <docTitle><text>NCX Fixture</text></docTitle>
  <navMap>
    <navPoint id="a" playOrder="1"><navLabel><text>Part 1</text></navLabel><content src="text/part1.xhtml"/></navPoint>
    <navPoint id="b" playOrder="2"><navLabel><text>Part 2</text></navLabel><content src="text/part2.xhtml"/></navPoint>
    <navPoint id="c" playOrder="3"><navLabel><text>Part 3</text></navLabel><content src="text/part3.xhtml"/></navPoint>
  </navMap>
</ncx>
"""

EPUB2_NCX_PART1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Part One</title></head>
<body>
  <h1 id="p1">Part One</h1>
  <p>Every part of the study begins with an article.</p>
  <p id="note">This paragraph is a link target; jump back to <a href="../text/part2.xhtml#p2">Part Two</a>.</p>
</body>
</html>
"""

EPUB2_NCX_PART2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Part Two</title></head>
<body>
  <h1 id="p2">Part Two</h1>
  <p>The work was hard, but they did not give up.</p>
  <p>Return to <a href="part1.xhtml#note">the note</a>.</p>
</body>
</html>
"""

EPUB2_NCX_PART3 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Part Three</title></head>
<body>
  <h1 id="p3">Part Three</h1>
  <p>Children study art because they look forward to beauty.</p>
</body>
</html>
"""


def build_epub2_ncx() -> Path:
    path = HERE / "epub2_ncx.epub"
    _write_epub(
        path,
        "OPS/package.opf",
        {
            "OPS/package.opf": EPUB2_NCX_OPF.encode("utf-8"),
            "OPS/toc.ncx": EPUB2_NCX_NCX.encode("utf-8"),
            "OPS/text/part1.xhtml": EPUB2_NCX_PART1.encode("utf-8"),
            "OPS/text/part2.xhtml": EPUB2_NCX_PART2.encode("utf-8"),
            "OPS/text/part3.xhtml": EPUB2_NCX_PART3.encode("utf-8"),
        },
    )
    return path


# ---------------------------------------------------------------------------
# EPUB 3：nav + 既有脚注 + 复杂内联 + 跨节点短语
# ---------------------------------------------------------------------------

EPUB3_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Complex Fixture</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:fixture-epub3-complex</dc:identifier>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="css/style.css" media-type="text/css"/>
    <item id="cover" href="images/cover.png" media-type="image/png"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

EPUB3_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="chapter1.xhtml">Chapter One</a></li>
      <li><a href="chapter2.xhtml">Chapter Two</a></li>
    </ol>
  </nav>
</body>
</html>
"""

EPUB3_CSS = """body { font-family: serif; }
.fn { font-size: 0.85em; }
"""

EPUB3_CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>One</title><link rel="stylesheet" type="text/css" href="css/style.css"/></head>
<body epub:type="bodymatter">
  <h1 id="c1">Chapter One</h1>
  <p>The <em>art</em> of the <strong>article</strong> is a <span class="term">part</span> of study.</p>
  <p>We must give <em>up</em> now<sup>1</sup>.</p>
  <p>Read more at https://example.com/art-study before the work begins.</p>
  <div hidden="hidden"><p>Hidden art and study text must never be marked.</p></div>
  <p style="display:none">Another hidden study line.</p>
  <pre><code>int art = 1; // study</code></pre>
  <p>They look forward to the holidays, and the child will buy a book.</p>
  <p>See <a href="chapter2.xhtml#end">the end</a> for the article notes.</p>
  <aside epub:type="footnote" id="fn1"><p>An existing footnote about art.</p></aside>
</body>
</html>
"""

EPUB3_CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Two</title><link rel="stylesheet" type="text/css" href="css/style.css"/></head>
<body epub:type="bodymatter">
  <h1 id="c2">Chapter Two</h1>
  <p>About ten percent of the class went home to study.</p>
  <p>We seek the truth in every part of the world.</p>
  <p id="end">The end of the second chapter.</p>
</body>
</html>
"""


def build_epub3_complex() -> Path:
    path = HERE / "epub3_complex.epub"
    _write_epub(
        path,
        "OPS/content.opf",
        {
            "OPS/content.opf": EPUB3_OPF.encode("utf-8"),
            "OPS/nav.xhtml": EPUB3_NAV.encode("utf-8"),
            "OPS/css/style.css": EPUB3_CSS.encode("utf-8"),
            "OPS/images/cover.png": PNG_1PX,
            "OPS/chapter1.xhtml": EPUB3_CH1.encode("utf-8"),
            "OPS/chapter2.xhtml": EPUB3_CH2.encode("utf-8"),
        },
    )
    return path


def build_all() -> list[Path]:
    return [build_epub2_basic(), build_epub2_ncx(), build_epub3_complex()]


if __name__ == "__main__":
    for fixture in build_all():
        print(f"built {fixture} ({fixture.stat().st_size} bytes)")
