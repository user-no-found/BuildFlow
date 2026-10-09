"""Read package identity from installed metadata or a source checkout."""
from importlib import metadata
from pathlib import Path
import tomllib


def version() -> str:
    """Return the authoritative distribution version."""
    source = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if source.is_file():
        return tomllib.loads(source.read_text(encoding="utf-8"))["project"]["version"]
    return metadata.version("buildflow")


def identity() -> dict:
    """Describe the loaded source input without depending on Git availability."""
    import hashlib
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    files = sorted(package.glob("*.py"))
    authority = package.parents[1] / "pyproject.toml"
    if authority.is_file():
        files.append(authority)
    for path in files:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return {"version": version(), "source_tree_sha256": digest.hexdigest()}
