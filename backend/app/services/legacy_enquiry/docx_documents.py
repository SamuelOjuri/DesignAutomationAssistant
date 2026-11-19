"""Read Word paragraphs and the image parts referenced by body, header and footer drawings."""

from io import BytesIO
from pathlib import PurePosixPath
import posixpath
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from defusedxml import ElementTree


_PARAGRAPH_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p",
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}p",
}
_IMAGE_TAGS = {
    "{http://schemas.openxmlformats.org/drawingml/2006/main}blip": "embed",
    "{http://purl.oclc.org/ooxml/drawingml/main}blip": "embed",
    "{urn:schemas-microsoft-com:vml}imagedata": "id",
}
_RELATIONSHIP_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_IMAGE_RELATIONSHIPS = {f"{namespace}/image" for namespace in _RELATIONSHIP_NAMESPACES}


class UnreadableDocxDrawing(ValueError):
    """A referenced internal drawing could not be read from the package."""


def _drawing_targets(archive, entries, part, root):
    references = []
    for node in root.iter():
        attribute = _IMAGE_TAGS.get(node.tag)
        if attribute:
            references.extend(node.attrib[f"{{{ns}}}{attribute}"] for ns in _RELATIONSHIP_NAMESPACES
                              if f"{{{ns}}}{attribute}" in node.attrib)
    if not references:
        return
    owner = PurePosixPath(part)
    rels_path = str(owner.parent / "_rels" / (owner.name + ".rels"))
    if rels_path not in entries:
        raise UnreadableDocxDrawing("A Word drawing has no relationship file. Save a fresh DOCX or export the drawing.")
    relationships = {node.get("Id"): node for node in ElementTree.fromstring(archive.read(rels_path))}
    for reference in dict.fromkeys(references):
        linked = relationships.get(reference)
        if linked is None or linked.get("Type") not in _IMAGE_RELATIONSHIPS:
            raise UnreadableDocxDrawing("A Word drawing has an invalid image relationship. Save a fresh DOCX.")
        if linked.get("TargetMode") == "External":
            continue
        target = urlsplit(linked.get("Target", ""))
        path = unquote(target.path)
        if not path or target.scheme or target.netloc or target.query or target.fragment or "\\" in path:
            raise UnreadableDocxDrawing("A Word drawing has an invalid image path. Save a fresh DOCX.")
        name = posixpath.normpath(path.lstrip("/") if path.startswith("/") else posixpath.join(str(owner.parent), path))
        if name not in entries or name == ".." or name.startswith("../"):
            raise UnreadableDocxDrawing("A Word drawing's image is missing from the DOCX. Save a fresh DOCX.")
        yield name


def read_docx(content, *, max_expanded_bytes, max_image_bytes, max_drawings):
    """Return the paragraph text and unique, package-relative drawing parts without writing files."""
    with ZipFile(BytesIO(content)) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > max_expanded_bytes:
            raise ValueError("Document exceeds the decompression limit")
        entries = set(archive.namelist())
        if len(entries) != len(archive.infolist()):
            raise ValueError("Document contains duplicate package parts")
        paragraphs, drawing_parts = [], {}
        for part in archive.namelist():
            if part != "word/document.xml" and not (part.startswith(("word/header", "word/footer"))
                                                   and part.endswith(".xml")):
                continue
            root = ElementTree.fromstring(archive.read(part))
            paragraphs.extend("".join(node.itertext()) for node in root.iter() if node.tag in _PARAGRAPH_TAGS)
            for name in _drawing_targets(archive, entries, part, root):
                drawing_parts[name] = None
        if len(drawing_parts) > max_drawings:
            raise UnreadableDocxDrawing(
                f"Word document exceeds the {max_drawings} embedded drawing limit. Split the document."
            )
        images = []
        for name in drawing_parts:
            entry = archive.getinfo(name)
            images.append((name, entry.file_size, archive.read(entry) if entry.file_size <= max_image_bytes else None))
    return "\n".join(paragraphs), images
