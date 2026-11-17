# Third-party software and models

mcp-secret is MIT-licensed. It installs and downloads the following at setup time; none of it is
copied into this repository or into the app bundle.

| Component | Use | License |
| --- | --- | --- |
| [GLiNER](https://github.com/urchade/GLiNER) | entity detection runtime | Apache-2.0 |
| [E3-JSI/gliner-multi-pii-domains-v1](https://huggingface.co/E3-JSI/gliner-multi-pii-domains-v1) | detector weights, fetched by `mcp-secret models` | Apache-2.0 |
| [urchade/gliner_multi_pii-v1](https://huggingface.co/urchade/gliner_multi_pii-v1) | base model of the one above | Apache-2.0 |
| [Docling](https://github.com/docling-project/docling) | PDF, DOCX, XLSX, PPTX, HTML parsing | MIT |
| [ocrmac](https://github.com/straussmaximilian/ocrmac) | OCR through macOS Vision | MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | the `serve` MCP server | MIT |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | `export`: PDF redaction | AGPL-3.0 |
| [sv-ttk](https://github.com/rdbende/Sun-Valley-ttk-theme) | theme of the Windows/Linux UI | MIT |
| [uv](https://github.com/astral-sh/uv) | installs the core on first launch of the app | Apache-2.0 / MIT |

Their transitive dependencies (see `uv.lock`) are MIT, BSD, Apache-2.0, ISC or PSF licensed.
PyMuPDF is AGPL-3.0: a build that ships it inside a bundle is bound by the AGPL; the app bundle
does not (it installs the core with uv on first launch).
