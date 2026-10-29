# Licensing and third-party notices

ArgusAI's original code is licensed under the GNU General Public License,
version 3 only (`GPL-3.0-only`). The complete terms are in [LICENSE](LICENSE).
Copyright remains with the respective ArgusAI contributors. The project is
published by [DAWA Technology / 达瓦科技](https://dawahezhi.com/).

Individuals and organizations may use ArgusAI, including commercially, under
the GPL. No separate permission or fee is required merely because the user is
a business. Distribution of a covered work must meet the GPL's source-code,
notice and other requirements. Paid support, deployment or development does
not withdraw the recipient's GPL rights. There is no warranty to the extent
permitted by applicable law; see sections 15 and 16 of the GPL.

Third-party files retain their own copyright notices, license terms and
applicable exceptions. This project does not relicense third-party code or
claim ownership of it. In particular, `GPL-3.0-only` describes ArgusAI's own
license choice; it does not replace a component's `GPL-2.0-or-later`, LGPL,
MIT, BSD, Apache, MPL or other original terms.

## Source code and rebuilding

The project source is available at <https://github.com/HG-ha/Argusai>.
Published releases are at <https://github.com/HG-ha/Argusai/releases>.
The release workflow creates an `ArgusAI-VERSION-source.tar.gz` archive and
SHA-256 checksum from the same validated Git commit as the native builds.
The archive includes the application's source, vendored source, frontend
source, dependency locks and build scripts. It is not a substitute for
retaining the source and notices of any additional covered components in a
particular binary distribution.

Build instructions are in the repository's
[build and release guide](https://github.com/HG-ha/Argusai/blob/main/wiki/Build-and-Release.md).
`uv.lock` records Python versions and source archives; the frontend and managed
JS runners have their own `package-lock.json` files. When distributing a
modified build, provide the corresponding source and build instructions for
that build, rather than pointing only to a different upstream version.

## Where to find component notices

Native builds include a readable `licenses/` directory beside the executable,
or at `ArgusAI.app/Contents/Resources/licenses/` on macOS. It contains the GPL,
this notice, source-location information, and a Python notice inventory with
the available original license documents. The same legal resources are
embedded under `argusai/legal/`. Frontend notices are included at
`argusai/webui/dist/THIRD_PARTY_NOTICES.txt`.

The inventories identify their scope and any missing upstream documents.
Including a package in an inventory does not mean all of its build/dev tools
are part of the executable, and generating an inventory does not by itself
complete a license compatibility or corresponding-source review.

| Component or group | Original license information and handling |
| --- | --- |
| sqlmap | `argusai/vendor/sqlmap/LICENSE`: GPL version 2 or later with the upstream clarifications and exceptions. Its original source and notices remain in the vendor subtree. See `argusai/vendor/sqlmap/doc/THIRD-PARTY.md` and the individual source-file headers for its own dependencies. |
| sqlmap third-party files | Individual headers remain authoritative. Some use LGPL; icmpsh source headers specify GPLv3 or later, despite a different classification in the upstream summary. Preserve the source files and their notices rather than applying the summary as a blanket replacement. |
| Vue, Naive UI and frontend dependencies | MIT, BSD, Apache and ISC notices are collected from the locked packages by `scripts/frontend_notices.py`. Preserve the full generated file with the frontend. |
| css-render packages | The three locked 0.15.14 packages omit license files in their npm archives. The original MIT license from the exact upstream v0.15.14 source is retained in `scripts/packaging/css-render-0.15.14.LICENSE.txt`, with its fixed source and hash recorded in `frontend-licenses.json`. |
| vdirs 0.1.8 | The official package declares MIT and author `07akioni`, but no original license document was recovered from its published archive or public repository history. The inventory explicitly marks this metadata-only gap; its separate SPDX reference template is not represented as the package's original copyright notice. |
| Python dependencies | Original package notices are collected by `scripts/python_notices.py`. Component licenses remain separate from ArgusAI's license. Native C/Rust subcomponents must also be checked against the actual distributed wheel and platform. |
| certifi | Its Mozilla-derived certificate data is covered by MPL-2.0. Retain its notice and provide the corresponding covered source under the MPL; see <https://github.com/certifi/python-certifi>. |
| pywebview / WebView2 | pywebview's BSD license does not replace the Microsoft SDK notices. Matching SDK LICENSE and NOTICE documents are retained for the verified bundled DLLs. The independently installed WebView2 browser Runtime is a separate distribution. |
| innoextract | The official archives carry the program's zlib-style license and a `license/` directory for bundled libraries, including the GCC Runtime Library Exception. Keep that directory with the tool. |
| Nuitka | The build tool's AGPL terms and its Runtime Library Exception are distinct. Preserve the applicable runtime notices and exception; use of that exception does not relicense the compiler itself. |
| Managed runtimes and external tools | Python, Node, WABT, browsers and external reverse/security tools have their own terms. They are not relicensed by the GPL notice for ArgusAI. Preserve notices and source obligations for anything you additionally redistribute. |

## Distribution review still required

The vdirs original-notice gap, primp's MIT metadata alongside its educational-use
wording, proxy-tools' MIT/BSD metadata discrepancy, and the full native static
dependency notices require follow-through before declaring a particular binary
release complete. The selected GPL license does not silently resolve these
documentation and provenance issues. No broader third-party rights or
commercial relicensing rights are asserted here.
