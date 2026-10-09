"""Publish flat artifact files without overwriting different existing content."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import uuid
from .contracts import Artifact, BuildError


def _filename(name: str) -> str:
    path = Path(name)
    windows = PureWindowsPath(name)
    if (not name or name in (".", "..") or len(path.parts) != 1 or path.is_absolute()
            or windows.drive or "/" in name or "\\" in name or ":" in name):
        raise BuildError(f"产物名称必须是直接文件名: {name}")
    return name


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_existing(path: Path, digest: str, mode: int) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if (path.is_symlink() or not path.is_file() or _digest(path) != digest
            or stat.S_IMODE(path.stat().st_mode) != mode):
        raise BuildError(f"拒绝覆盖同名不同内容或权限的产物，请选择新版本: {path}")
    return True


def publish(output: Path, work: Path, artifacts: list[Artifact], report: dict) -> Path:
    """Publish individual files and update success only after every file exists.

    Existing identical files are reused. Ordinary errors roll back newly added
    files; abrupt process death can leave files not referenced by last-success.
    This is not an atomic transaction across several files.
    """
    staging = output / f".pending-{uuid.uuid4().hex}"
    staging.mkdir()
    pointer_tmp = work.parent / f".last-success-{uuid.uuid4().hex}.tmp"
    added = []
    manifest = []
    names = set()
    committed = False
    try:
        for artifact in artifacts:
            name = _filename(artifact.name)
            if name.casefold() in names:
                raise BuildError(f"重复产物名称: {name}")
            names.add(name.casefold())
            source = Path(artifact.path)
            if source.is_symlink() or not source.is_file():
                raise BuildError(f"产物必须是存在的常规文件: {source}")
            candidate = staging / name
            shutil.copy2(source, candidate)
            digest = _digest(candidate)
            mode = stat.S_IMODE(candidate.stat().st_mode)
            _same_existing(output / name, digest, mode)
            manifest.append({"name": name, "size": candidate.stat().st_size, "sha256": digest, "mode": mode})
        for item in manifest:
            destination = output / item["name"]
            if _same_existing(destination, item["sha256"], item["mode"]):
                continue
            try:
                # Same-filesystem hard links publish a complete file with no
                # overwrite race; no byte-copy fallback may expose partial data.
                os.link(staging / item["name"], destination)
                added.append(destination)
            except FileExistsError:
                if not _same_existing(destination, item["sha256"], item["mode"]):
                    raise BuildError(f"发布目标被并发修改: {destination}")
        report_path = work / "report.json"
        report_path.write_text(json.dumps({**report, "artifacts": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pointer = {"schema": 2, "run_id": work.name,
                   "report": f"{work.name}/report.json", "artifacts": manifest}
        with pointer_tmp.open("x", encoding="utf-8") as file:
            json.dump(pointer, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(pointer_tmp, work.parent / "last-success.json")
        committed = True
        return output
    finally:
        if not committed:
            for path in reversed(added):
                path.unlink(missing_ok=True)
        shutil.rmtree(staging)
        pointer_tmp.unlink(missing_ok=True)
