#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
GEN_DIR = ROOT / "generated"
OUTPUT = GEN_DIR / "kidney_external_therapy_KoPubWorld.hwpx"
BODY_FONT = "KoPubWorld바탕체"
TITLE_FONT = "KoPubWorld돋움체"


def local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def register_namespaces(xml_bytes: bytes) -> None:
    import io
    for _, item in ET.iterparse(io.BytesIO(xml_bytes), events=("start-ns",)):
        prefix, uri = item
        try:
            ET.register_namespace(prefix or "", uri)
        except ValueError:
            pass


def parse_markdown() -> tuple[str, list[dict], set[str]]:
    files = sorted(CONTENT_DIR.glob("*.md"))
    if not files:
        raise RuntimeError("No content/*.md files found")
    text = "\n\n".join(p.read_text(encoding="utf-8") for p in files)
    lines = text.splitlines()
    main_title = "제8장  腎臟病의 中醫 外治療法"
    content: list[dict] = []
    heading_texts: set[str] = {main_title}
    paragraph_buf: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_buf
        if paragraph_buf:
            joined = " ".join(s.strip() for s in paragraph_buf if s.strip()).strip()
            if joined:
                content.append({"type": "paragraph", "text": joined})
            paragraph_buf = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            flush_paragraph()
            i += 1
            continue
        if line.startswith("# "):
            flush_paragraph()
            main_title = line[2:].strip()
            heading_texts.add(main_title)
            i += 1
            continue
        if line.startswith("## "):
            flush_paragraph()
            h = line[3:].strip()
            heading_texts.add(h)
            content.append({"type": "heading", "text": h})
            i += 1
            continue
        if line.startswith(":::table "):
            flush_paragraph()
            caption = line[len(":::table "):].strip()
            i += 1
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip() != ":::endtable":
                if lines[i].strip():
                    table_lines.append(lines[i].strip())
                i += 1
            if i >= len(lines):
                raise RuntimeError(f"Unclosed table: {caption}")
            i += 1
            rows: list[list[str]] = []
            for tl in table_lines:
                if not (tl.startswith("|") and tl.endswith("|")):
                    raise RuntimeError(f"Malformed table row: {tl}")
                cells = [c.strip() for c in tl[1:-1].split("|")]
                if all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
                    continue
                rows.append(cells)
            if len(rows) < 2:
                raise RuntimeError(f"Table needs header and body: {caption}")
            headers, body_rows = rows[0], rows[1:]
            for r in body_rows:
                if len(r) != len(headers):
                    raise RuntimeError(f"Table width mismatch in {caption}: {r}")
            content.append({"type": "table", "caption": caption, "headers": headers, "rows": body_rows})
            continue
        paragraph_buf.append(line)
        i += 1
    flush_paragraph()
    return main_title, content, heading_texts


def obtain_generator() -> Path:
    target = ROOT / ".hwpx-skill"
    if target.exists():
        shutil.rmtree(target)
    subprocess.run([
        "git", "clone", "--depth", "1",
        "https://github.com/Steven-A3/HWPX-CLAUDE-SKILL.git",
        str(target),
    ], check=True)
    return target


def generate_base(main_title: str, content: list[dict], skill_dir: Path, out_path: Path) -> None:
    sys.path.insert(0, str(skill_dir))
    from scripts.generate_hwpx import generate_hwpx
    config = {
        "title": main_title,
        "date": "",
        "department": "",
        "include_cover": False,
        "sections": [{
            "type": "body",
            "title_bar": main_title,
            "content": content,
        }],
    }
    generate_hwpx(config, str(out_path))


def paragraph_text(p: ET.Element) -> str:
    return "".join((e.text or "") for e in p.iter() if local_name(e.tag) == "t")


def font_element_template(fontface: ET.Element) -> ET.Element:
    for child in fontface:
        if local_name(child.tag) == "font":
            return copy.deepcopy(child)
    raise RuntimeError("fontface has no font entry")


def modify_fonts_and_markers(hwpx_path: Path, heading_texts: set[str]) -> dict:
    tmpdir = Path(tempfile.mkdtemp(prefix="hwpx-fontfix-"))
    try:
        with zipfile.ZipFile(hwpx_path, "r") as zin:
            infos = zin.infolist()
            data = {zi.filename: zin.read(zi.filename) for zi in infos}

        heading_char_ids: set[str] = set()
        section_names = [n for n in data if n.startswith("Contents/section") and n.endswith(".xml")]
        normalized_headings = {re.sub(r"\s+", "", h) for h in heading_texts}
        for name in section_names:
            raw = data[name]
            register_namespaces(raw)
            root = ET.fromstring(raw)
            for p in root.iter():
                if local_name(p.tag) != "p":
                    continue
                full = paragraph_text(p)
                normalized = re.sub(r"[\s□]+", "", full)
                is_heading = normalized in normalized_headings
                if is_heading:
                    for run in p.iter():
                        if local_name(run.tag) == "run" and run.get("charPrIDRef") is not None:
                            heading_char_ids.add(run.get("charPrIDRef"))
                    # Remove the generator's decorative heading marker only.
                    for t in p.iter():
                        if local_name(t.tag) == "t" and t.text:
                            t.text = re.sub(r"^\s*□\s*", "", t.text, count=1)
            data[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        header_name = "Contents/header.xml"
        raw_header = data[header_name]
        register_namespaces(raw_header)
        hroot = ET.fromstring(raw_header)

        fontfaces = [e for e in hroot.iter() if local_name(e.tag) == "fontface"]
        if not fontfaces:
            raise RuntimeError("No fontface groups in header.xml")
        for ff in fontfaces:
            fonts = [c for c in list(ff) if local_name(c.tag) == "font"]
            by_id = {f.get("id"): f for f in fonts}
            if "0" not in by_id:
                f0 = font_element_template(ff)
                f0.set("id", "0")
                ff.append(f0)
                by_id["0"] = f0
            if "1" not in by_id:
                f1 = copy.deepcopy(by_id["0"])
                f1.set("id", "1")
                ff.append(f1)
                by_id["1"] = f1
            by_id["0"].set("face", BODY_FONT)
            by_id["1"].set("face", TITLE_FONT)
            current_fonts = [c for c in list(ff) if local_name(c.tag) == "font"]
            if ff.get("fontCnt") is not None:
                ff.set("fontCnt", str(len(current_fonts)))

        charpr_count = 0
        title_charpr_count = 0
        for cp in hroot.iter():
            if local_name(cp.tag) != "charPr":
                continue
            cp_id = cp.get("id")
            font_ref = next((c for c in cp if local_name(c.tag) == "fontRef"), None)
            if font_ref is None:
                continue
            chosen = "1" if cp_id in heading_char_ids else "0"
            for key in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user"):
                font_ref.set(key, chosen)
            charpr_count += 1
            if chosen == "1":
                title_charpr_count += 1

        if charpr_count == 0:
            raise RuntimeError("No charPr/fontRef entries found")
        if title_charpr_count == 0:
            raise RuntimeError("No title/heading charPr entries identified")
        data[header_name] = ET.tostring(hroot, encoding="utf-8", xml_declaration=True)

        # Repack with the original member order and compression; mimetype stays first and STORED.
        out_tmp = hwpx_path.with_suffix(".repacked.hwpx")
        with zipfile.ZipFile(out_tmp, "w") as zout:
            for zi in infos:
                payload = data[zi.filename]
                new_zi = copy.copy(zi)
                if zi.filename == "mimetype":
                    new_zi.compress_type = zipfile.ZIP_STORED
                zout.writestr(new_zi, payload)
        out_tmp.replace(hwpx_path)

        return {
            "sections": len(section_names),
            "heading_char_ids": sorted(heading_char_ids),
            "charpr_count": charpr_count,
            "title_charpr_count": title_charpr_count,
            "body_font": BODY_FONT,
            "title_font": TITLE_FONT,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def verify(hwpx_path: Path, heading_char_ids: set[str] | None = None) -> None:
    with zipfile.ZipFile(hwpx_path, "r") as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.testzip() is None
        for n in z.namelist():
            if n.endswith(".xml"):
                ET.fromstring(z.read(n))
        header = ET.fromstring(z.read("Contents/header.xml"))
        faces_ok = 0
        for ff in header.iter():
            if local_name(ff.tag) != "fontface":
                continue
            faces = {c.get("id"): c.get("face") for c in ff if local_name(c.tag) == "font"}
            assert faces.get("0") == BODY_FONT, faces
            assert faces.get("1") == TITLE_FONT, faces
            faces_ok += 1
        assert faces_ok > 0
        refs = []
        for cp in header.iter():
            if local_name(cp.tag) == "charPr":
                fr = next((c for c in cp if local_name(c.tag) == "fontRef"), None)
                if fr is not None:
                    vals = [fr.get(k) for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")]
                    assert len(set(vals)) == 1 and vals[0] in {"0", "1"}, vals
                    refs.append(vals[0])
        assert "0" in refs and "1" in refs


def main() -> None:
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    main_title, content, heading_texts = parse_markdown()
    skill_dir = obtain_generator()
    with tempfile.TemporaryDirectory(prefix="hwpx-build-") as td:
        base = Path(td) / "base.hwpx"
        generate_base(main_title, content, skill_dir, base)
        shutil.copy2(base, OUTPUT)
    report = modify_fonts_and_markers(OUTPUT, heading_texts)
    verify(OUTPUT)
    (GEN_DIR / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Generated: {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
