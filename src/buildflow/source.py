"""Resolve one Git revision and materialize only that immutable source input."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
from .contracts import BuildError, RunnerProtocol
from .locking import Lock


@dataclass(frozen=True)
class ResolvedSource:
    """Exact upstream revision resolved for one build."""

    url: str
    revision: str
    ref: str = "HEAD"


def resolve_git(url: str, ref: str = "HEAD", *, runner: RunnerProtocol) -> ResolvedSource:
    """Resolve latest HEAD once, or accept an explicit full object ID."""
    if not url or url.startswith("-") or not ref or ref.startswith("-"):
        raise BuildError("Git URL/ref 无效")
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
        return ResolvedSource(url, ref.lower(), ref)
    text = runner.capture(["git", "ls-remote", "--exit-code", url, ref], cwd=Path.cwd())
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", rows[0][0]):
        raise BuildError(f"Git ref 必须唯一解析: {ref}")
    return ResolvedSource(url, rows[0][0].lower(), ref)


def fetch_git(source: ResolvedSource, destination: Path | str, cache_dir: Path | str, *, runner: RunnerProtocol) -> Path:
    """Fetch the exact SHA into an owned cache and create a detached checkout."""
    if not isinstance(source.url, str) or not source.url or source.url.startswith("-"):
        raise BuildError("Git URL 无效")
    destination = Path(destination).absolute()
    cache_dir = Path(cache_dir).absolute()
    if destination.exists() or destination.is_symlink():
        raise BuildError(f"源码目标已存在，拒绝修改检出: {destination}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source.revision):
        raise BuildError("源码 revision 必须为完整 Git SHA")
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(source.url.encode()).hexdigest()
    repository = cache_dir / (key + ".git")
    with Lock(cache_dir / (key + ".lock")):
        if repository.is_symlink():
            raise BuildError(f"源码缓存不能是符号链接: {repository}")
        if not repository.exists():
            runner.run(["git", "init", "--bare", "--initial-branch=main", str(repository)], cwd=cache_dir)
        # A fixed SHA may use its verified object cache, never a cached HEAD.
        reference = "refs/heads/buildflow-" + source.revision
        cached = runner.capture(["git", "--git-dir", str(repository), "for-each-ref",
                                 "--format=%(objectname)", reference], cwd=cache_dir).strip()
        if cached != source.revision:
            runner.run(["git", "--git-dir", str(repository), "fetch", "--no-tags", "--depth=1", "--", source.url, source.revision], cwd=cache_dir)
        actual = runner.capture(["git", "--git-dir", str(repository), "rev-parse", "--verify", source.revision + "^{commit}"], cwd=cache_dir).strip()
        if actual != source.revision:
            raise BuildError(f"源码 SHA 不匹配: {actual}")
        runner.run(["git", "--git-dir", str(repository), "update-ref", "refs/heads/buildflow-" + source.revision, source.revision], cwd=cache_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            runner.run(["git", "clone", "--no-checkout", str(repository), str(destination)], cwd=destination.parent)
            runner.run(["git", "checkout", "--detach", source.revision], cwd=destination)
            runner.run(["git", "remote", "remove", "origin"], cwd=destination)
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination)
            raise
    return destination
