"""Read Microsoft Teams transcript exports (.vtt / .docx) or plain text into 'Speaker: text' lines."""

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

_VTT_VOICE = re.compile(r"<v\s+([^>]+)>")
_VTT_TIME = re.compile(r"^(\d+:)?\d+:\d+[.,]\d+\s+-->")  # Teams writes "0:0:2.270 --> ..."
_TAG = re.compile(r"<[^>]+>")


def read_vtt(text: str) -> str:
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        stamp, body = "", []
        for line in block.strip().splitlines():
            line = line.strip()
            if _VTT_TIME.match(line):
                h_m_s = line.split("-->")[0].strip().split(".")[0].split(",")[0].split(":")
                stamp = ":".join(p.zfill(2) for p in h_m_s)
            elif line and line != "WEBVTT" and stamp:
                body.append(line)
        if not body:
            continue
        joined = " ".join(body)
        voice = _VTT_VOICE.search(joined)
        speaker = voice.group(1).strip() if voice else "Unknown"
        words = _TAG.sub("", joined).strip()
        if words:
            out.append(f"[{stamp}] {speaker}: {words}")
    return "\n".join(out)


def read_docx(path: Path) -> str:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")).strip()
        if text:
            paras.append(text)
    return "\n".join(paras)


def read_transcript_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return read_vtt(text)
    return text
