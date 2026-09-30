"""Every runtime dependency earns its place in the image.

A package declared and never imported still ships in every image, still gets audited,
and still pins a resolution. Nothing else notices when the last import of one goes.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Declared and deliberately never imported by ``app/``, each for a stated reason.
NOT_IMPORTED = {
    # The server the image runs; it imports the app, not the other way round.
    "uvicorn",
    # Floors under keyring-client's token verification, which imports them itself.
    "pyjwt",
    "cryptography",
}

#: Distributions whose import name is not their normalised distribution name.
IMPORT_NAMES = {"pyjwt": "jwt"}


def declared() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = (re.split(r"[\[<>=!~ ;]", spec, maxsplit=1)[0] for spec in project["dependencies"])
    return [name.lower() for name in names]


def imported() -> set[str]:
    found: set[str] = set()
    for source in (ROOT / "app").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        found.update(re.findall(r"^\s*(?:from|import) ([a-z_]\w*)", text, flags=re.MULTILINE))
    return found


def test_every_runtime_dependency_is_imported_by_the_app():
    modules = imported()
    unused = [
        name
        for name in declared()
        if name not in NOT_IMPORTED
        and IMPORT_NAMES.get(name, name.replace("-", "_")) not in modules
    ]
    assert unused == []


def test_every_exemption_is_still_declared():
    assert NOT_IMPORTED.issubset(declared())
