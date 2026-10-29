"""Snapshot and rollback management."""

import json
import shlex
import shutil
import tarfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ssh import SSHClient


@dataclass
class SnapshotManifest:
    """Manifest describing a snapshot."""

    id: str
    created_at: str
    phase: str
    description: str
    files: list[str] = field(default_factory=list)
    docker_containers: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


class SnapshotManager:
    """Manage system snapshots for rollback."""

    SNAPSHOT_DIR = "/var/lib/edge-server-snapshots"

    def __init__(self, ssh: SSHClient, snapshot_dir: Optional[str] = None):
        self.ssh = ssh
        self.snapshot_dir = (snapshot_dir or self.SNAPSHOT_DIR).rstrip("/")

    def _write_remote_file(self, path: str, content: str) -> bool:
        """Write content to a root-owned remote file.

        ``sudo cmd > file`` performs the redirect as the unprivileged SSH user,
        so it fails for /var/lib. Pipe the data into ``sudo tee`` instead.
        """
        result = self.ssh.run(f"tee {shlex.quote(path)} > /dev/null", sudo=True, input=content)
        return result.success

    def _generate_id(self, phase: str) -> str:
        """Generate snapshot ID."""
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        return f"{timestamp}_{phase}"

    def create(
        self,
        phase: str,
        description: str,
        paths: Optional[list[str]] = None,
        include_docker: bool = False,
    ) -> Optional[str]:
        """Create a snapshot. Returns snapshot ID on success."""
        snapshot_id = self._generate_id(phase)
        snapshot_path = f"{self.snapshot_dir}/{snapshot_id}"
        q_snapshot_path = shlex.quote(snapshot_path)

        # Create snapshot directory
        result = self.ssh.run(f"mkdir -p {q_snapshot_path}", sudo=True)
        if not result.success:
            return None

        manifest = SnapshotManifest(
            id=snapshot_id,
            created_at=datetime.now().isoformat(),
            phase=phase,
            description=description,
        )

        # Backup specified paths
        if paths:
            for path in paths:
                if self.ssh.path_exists(path):
                    # Create tarball of the path
                    basename = Path(path).name
                    parent = str(Path(path).parent)
                    tar_path = f"{snapshot_path}/{basename}.tar.gz"
                    result = self.ssh.run(
                        f"tar -czf {shlex.quote(tar_path)} -C {shlex.quote(parent)} "
                        f"{shlex.quote(basename)}",
                        sudo=True,
                    )
                    if result.success:
                        manifest.files.append(path)

        # Backup Docker container list
        if include_docker:
            result = self.ssh.run("docker ps --format '{{.Names}}' 2>/dev/null || true", sudo=True)
            if result.success and result.stdout:
                containers = [c for c in result.stdout.strip().split("\n") if c]
                manifest.docker_containers = containers

                # Save container inspect data
                for container in containers:
                    inspect_result = self.ssh.run(
                        f"docker inspect {shlex.quote(container)}", sudo=True
                    )
                    if inspect_result.success:
                        self._write_remote_file(
                            f"{snapshot_path}/{container}.json", inspect_result.stdout + "\n"
                        )

        # Write manifest
        manifest_json = json.dumps(asdict(manifest), indent=2)
        if not self._write_remote_file(f"{snapshot_path}/manifest.json", manifest_json + "\n"):
            return None

        # Update latest symlink
        self.ssh.run(
            f"ln -sfn {q_snapshot_path} {shlex.quote(self.snapshot_dir + '/latest')}", sudo=True
        )

        return snapshot_id

    def list_snapshots(self) -> list[SnapshotManifest]:
        """List all snapshots."""
        result = self.ssh.run(
            f"ls -1 {shlex.quote(self.snapshot_dir)} 2>/dev/null | grep -vx latest || true"
        )
        if not result.success or not result.stdout:
            return []

        snapshots = []
        for snapshot_id in result.stdout.strip().split("\n"):
            if not snapshot_id:
                continue
            manifest = self.get_manifest(snapshot_id)
            if manifest:
                snapshots.append(manifest)

        return sorted(snapshots, key=lambda s: s.created_at, reverse=True)

    def get_manifest(self, snapshot_id: str) -> Optional[SnapshotManifest]:
        """Get manifest for a snapshot."""
        if snapshot_id == "latest":
            # Resolve latest symlink
            result = self.ssh.run(f"readlink {shlex.quote(self.snapshot_dir + '/latest')}")
            if result.success and result.stdout.strip():
                snapshot_id = Path(result.stdout.strip()).name

        manifest_path = f"{self.snapshot_dir}/{snapshot_id}/manifest.json"
        result = self.ssh.run(f"cat {shlex.quote(manifest_path)}", sudo=True)
        if not result.success:
            return None

        try:
            data = json.loads(result.stdout)
            return SnapshotManifest(**data)
        except Exception:
            return None

    def restore(self, snapshot_id: str) -> bool:
        """Restore from a snapshot."""
        manifest = self.get_manifest(snapshot_id)
        if not manifest:
            return False

        snapshot_path = f"{self.snapshot_dir}/{manifest.id}"
        ok = True

        # Stop containers that were running when the snapshot was taken so their
        # files can be replaced, then bring them back afterwards.
        for container in manifest.docker_containers:
            self.ssh.run(f"docker stop {shlex.quote(container)} 2>/dev/null || true", sudo=True)

        # Restore files
        for file_path in manifest.files:
            basename = Path(file_path).name
            tar_path = f"{snapshot_path}/{basename}.tar.gz"
            parent_dir = str(Path(file_path).parent)

            if not self.ssh.run(f"test -f {shlex.quote(tar_path)}", sudo=True).success:
                ok = False
                continue

            # Remove current and restore from backup
            self.ssh.run(f"rm -rf {shlex.quote(file_path)}", sudo=True)
            result = self.ssh.run(
                f"tar -xzf {shlex.quote(tar_path)} -C {shlex.quote(parent_dir)}", sudo=True
            )
            if not result.success:
                ok = False

        for container in manifest.docker_containers:
            self.ssh.run(f"docker start {shlex.quote(container)} 2>/dev/null || true", sudo=True)

        return ok

    def delete(self, snapshot_id: str) -> bool:
        """Delete a snapshot."""
        if not snapshot_id or "/" in snapshot_id or snapshot_id in (".", "..", "latest"):
            return False
        snapshot_path = f"{self.snapshot_dir}/{snapshot_id}"
        result = self.ssh.run(f"rm -rf {shlex.quote(snapshot_path)}", sudo=True)
        return result.success

    def cleanup_old(self, keep: int = 5) -> int:
        """Delete old snapshots, keeping the most recent ones. Returns count deleted."""
        snapshots = self.list_snapshots()
        if len(snapshots) <= keep:
            return 0

        deleted = 0
        for snapshot in snapshots[keep:]:
            if self.delete(snapshot.id):
                deleted += 1
        return deleted


class LocalSnapshotManager:
    """Manage local snapshots (for testing or local deployments)."""

    def __init__(self, snapshot_dir: Optional[Path] = None):
        self.snapshot_dir = snapshot_dir or Path("/var/lib/edge-server-snapshots")

    def create(
        self,
        phase: str,
        description: str,
        paths: Optional[list[Path]] = None,
    ) -> Optional[str]:
        """Create a local snapshot."""
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        snapshot_id = f"{timestamp}_{phase}"
        snapshot_path = self.snapshot_dir / snapshot_id

        snapshot_path.mkdir(parents=True, exist_ok=True)

        manifest = SnapshotManifest(
            id=snapshot_id,
            created_at=datetime.now().isoformat(),
            phase=phase,
            description=description,
        )

        if paths:
            for path in paths:
                if path.exists():
                    tar_path = snapshot_path / f"{path.name}.tar.gz"
                    with tarfile.open(tar_path, "w:gz") as tar:
                        tar.add(path, arcname=path.name)
                    manifest.files.append(str(path))

        # Write manifest
        manifest_file = snapshot_path / "manifest.json"
        manifest_file.write_text(json.dumps(asdict(manifest), indent=2))

        # Update latest symlink
        latest = self.snapshot_dir / "latest"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(snapshot_path)

        return snapshot_id

    def restore(self, snapshot_id: str) -> bool:
        """Restore from a local snapshot."""
        if snapshot_id == "latest":
            snapshot_path = self.snapshot_dir / "latest"
            if not snapshot_path.exists():
                return False
            snapshot_path = snapshot_path.resolve()
        else:
            snapshot_path = self.snapshot_dir / snapshot_id

        manifest_file = snapshot_path / "manifest.json"
        if not manifest_file.exists():
            return False

        try:
            manifest = SnapshotManifest(**json.loads(manifest_file.read_text()))
        except Exception:
            return False

        for file_path in manifest.files:
            path = Path(file_path)
            tar_path = snapshot_path / f"{path.name}.tar.gz"
            if tar_path.exists():
                # Remove current
                if path.exists() or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                # Extract
                with tarfile.open(tar_path, "r:gz") as tar:
                    _safe_extract(tar, path.parent)

        return True


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball without allowing members to escape ``dest``."""
    if hasattr(tarfile, "tar_filter"):
        # "tar" blocks absolute paths and members escaping dest but keeps
        # ownership/permissions, which a system-file restore needs.
        tar.extractall(dest, filter="tar")
        return
    dest_resolved = dest.resolve()
    for member in tar.getmembers():  # pragma: no cover - only on old Pythons
        target = (dest / member.name).resolve()
        if dest_resolved != target and dest_resolved not in target.parents:
            raise ValueError(f"Unsafe path in snapshot archive: {member.name}")
        if member.issym() or member.islnk():
            raise ValueError(f"Links are not allowed in snapshot archive: {member.name}")
    tar.extractall(dest)  # noqa: S202 - members validated above
