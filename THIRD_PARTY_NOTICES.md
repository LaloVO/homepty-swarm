# Third-party notices

Homepty Swarm is distributed under AGPL-3.0. This file is an operational inventory, not
a substitute for the license text carried by each dependency or for the release SBOM.

The production backend dependency graph is locked by `backend/uv.lock`. Every released API
and worker image must also publish an image-derived SBOM and vulnerability report.

## Direct backend dependencies

| Component | Locked version | License signal | Upstream |
| --- | ---: | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause | https://github.com/pallets/flask |
| Flask-Cors | 6.0.2 | MIT | https://github.com/corydolphin/flask-cors |
| OpenAI Python | 1.109.1 | Apache-2.0 | https://github.com/openai/openai-python |
| Zep Cloud Python SDK | 3.13.0 | verify from released distribution | https://github.com/getzep/zep-python |
| CAMEL AI | 0.2.78 | verify from released distribution | https://github.com/camel-ai/camel |
| CAMEL OASIS | 0.2.5 | Apache-2.0 | https://github.com/camel-ai/oasis |
| PyMuPDF | 1.26.7 | AGPL-3.0 or commercial license | https://github.com/pymupdf/PyMuPDF |
| charset-normalizer | 3.4.4 | MIT | https://github.com/jawah/charset_normalizer |
| chardet | 5.2.0 | LGPL-2.1-or-later | https://github.com/chardet/chardet |
| python-dotenv | 1.2.3 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| Pydantic | 2.12.5 | MIT | https://github.com/pydantic/pydantic |
| rfc8785 | 0.1.4 | Apache-2.0 | https://github.com/trailofbits/rfc8785.py |

`PyMuPDF` is deliberately called out because its AGPL/commercial dual license is material.
The service itself remains AGPL-3.0, but a release gate must still retain its notices and
verify every transitive component from the built image.

## Release obligations

- Preserve this repository's `LICENSE` and upstream copyright notices.
- Publish the exact Corresponding Source commit used by each network deployment.
- Generate SBOMs from both final images, not only from `pyproject.toml`.
- Do not ship `backend/uploads/`, `graphify-out/`, reports, caches, local databases or `.env`.
- Treat an unknown or incompatible license as a failed release gate.
