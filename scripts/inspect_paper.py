from pathlib import Path
import httpx
from pypdf import PdfReader

path = Path("inputs/2510.21513v2.pdf")
path.parent.mkdir(exist_ok=True)
if not path.exists():
    response = httpx.get("https://arxiv.org/pdf/2510.21513v2", timeout=45, follow_redirects=True)
    response.raise_for_status()
    path.write_bytes(response.content)
reader = PdfReader(path)
for number, page in enumerate(reader.pages, 1):
    text = page.extract_text() or ""
    if "github" in text.lower() or "availability" in text.lower() or "replication" in text.lower():
        print("PAGE", number, text[-6000:])
    for annotation in page.get("/Annots", []):
        uri = annotation.get_object().get("/A", {}).get("/URI", "")
        if any(host in uri for host in ("github", "zenodo", "figshare")):
            print("LINK", uri)
