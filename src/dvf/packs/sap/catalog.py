"""The SAP business object templates shipped with dvf.

Templates are configuration fragments under ``dvf/packs/sap/templates``: whole suites whose
connection names, tables, and filters are ``${var:...}`` placeholders. Copy one next to your
configuration (``dvf sap copy-template NAME``), add it to ``extends:``, and set its variables in
``vars:``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_VAR_RE = re.compile(r"\$\{var:([A-Za-z0-9_]+)\}")


@dataclass(frozen=True)
class Template:
    name: str
    description: str

    @property
    def path(self) -> Path:
        return template_path(self.name)

    @property
    def variables(self) -> tuple[str, ...]:
        """The ``${var:...}`` names the template needs."""
        text = self.path.read_text(encoding="utf-8")
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        return tuple(sorted(set(_VAR_RE.findall(body))))


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in (
        Template(
            "cvi_business_partner",
            "Customer and vendor to Business Partner (CVI) links: counts and unlinked records",
        ),
        Template(
            "gl_balances",
            "GL balances per company code, period, and account, with trial balance checks",
        ),
        Template("open_items", "Customer (BSID) and vendor (BSIK) open items per company code"),
        Template(
            "material_master",
            "Material master: general data, per plant (MARC), per sales organization (MVKE)",
        ),
    )
}


def template_path(name: str) -> Path:
    """The packaged file of template ``name``."""
    if name not in TEMPLATES:
        known = ", ".join(sorted(TEMPLATES))
        raise KeyError(f"unknown SAP template '{name}'; available: {known}")
    root = resources.files("dvf.packs.sap") / "templates" / f"{name}.yaml"
    return Path(str(root))


def copy_template(name: str, folder: Path, *, force: bool = False) -> Path:
    """Copy template ``name`` into ``folder`` and return the new file."""
    source = template_path(name)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass force to replace it")
    shutil.copyfile(source, target)
    return target


__all__ = ["TEMPLATES", "Template", "copy_template", "template_path"]
