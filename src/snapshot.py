"""Snapshot and rollback management."""

import json
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ssh import SSHClient


@dataclass
class SnapshotManifest:
    """Manifest describing a snapshot."""

    id: str
    created_at: str
    phase: str
    description: str
    files: List[str] = field(default_factory=list)
    docker_containers: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)


class SnapshotManager:
    """Manage system snapshots for rollback."""

    SNAPSHOT_DIR = "/var/lib/edge-server-snapshots"

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def _generate_id(self, phase: str) -> str:
        """Generate snapshot ID."""
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        return f"{timestamp}_{phase}"

    def create(
        self,
        phase: str,
        description: str,
        paths: Optional[List[str]] = None,
        include_docker: bool = False,
    ) -> Optional[str]:
        """Create a snapshot. Returns snapshot ID on success."""
        snapshot_id = self._generate_id(phase)
        snapshot_path = f"{self.SNAPSHOT_DIR}/{snapshot_id}"

        # Create snapshot directory
        result = self.ssh.run(f"mkdir -p {snapshot_path}", sudo=True)
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
                    tar_path = f"{snapshot_path}/{basename}.tar.gz"
                    result = self.ssh.run(
                        f"tar -czf {tar_path} -C $(dirname {path}) {basename}",
                        sudo=True,
                    )
                    if result.success:
                        manifest.files.append(path)

        # Backup Docker container list
        if include_docker:
            result = self.ssh.run("docker ps --format '{{.Names}}' 2>/dev/null || true")
            if result.success and result.stdout:
                containers = result.stdout.strip().split("\n")
                manifest.docker_containers = containers

                # Save container inspect data
                for container in containers:
                    inspect_result = self.ssh.run(f"docker inspect {container}", sudo=True)
                    if inspect_result.success:
                        container_file = f"{snapshot_path}/{container}.json"
                        self.ssh.run(
                            f"echo '{inspect_result.stdout}' > {container_file}",
                            sudo=True,
                        )

        # Write manifest
        manifest_json = json.dumps(
            {
                "id": manifest.id,
                "created_at": manifest.created_at,
                "phase": manifest.phase,
                "description": manifest.description,
                "files": manifest.files,
                "docker_containers": manifest.docker_containers,
                "metadata": manifest.metadata,
            },
            indent=2,
        )

        self.ssh.run(
            f"cat > {snapshot_path}/manifest.json << 'EOF'\n{manifest_json}\nEOF", sudo=True
        )

        # Update latest symlink
        self.ssh.run(f"ln -sfn {snapshot_path} {self.SNAPSHOT_DIR}/latest", sudo=True)

        return snapshot_id

    def list_snapshots(self) -> List[SnapshotManifest]:
        """List all snapshots."""
        result = self.ssh.run(f"ls -1 {self.SNAPSHOT_DIR} 2>/dev/null | grep -v latest || true")
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
            result = self.ssh.run(f"readlink {self.SNAPSHOT_DIR}/latest")
            if result.success:
                snapshot_id = Path(result.stdout.strip()).name

        manifest_path = f"{self.SNAPSHOT_DIR}/{snapshot_id}/manifest.json"
        result = self.ssh.run(f"cat {manifest_path}")
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

        snapshot_path = f"{self.SNAPSHOT_DIR}/{manifest.id}"

        # Stop Docker containers if they're in the snapshot
        if manifest.docker_containers:
            for container in manifest.docker_containers:
                self.ssh.run(f"docker stop {container} 2>/dev/null || true", sudo=True)

        # Restore files
        for file_path in manifest.files:
            basename = Path(file_path).name
            tar_path = f"{snapshot_path}/{basename}.tar.gz"
            parent_dir = str(Path(file_path).parent)

            # Remove current and restore from backup
            self.ssh.run(f"rm -rf {file_path}", sudo=True)
            self.ssh.run(f"tar -xzf {tar_path} -C {parent_dir}", sudo=True)

        return True

    def delete(self, snapshot_id: str) -> bool:
        """Delete a snapshot."""
        snapshot_path = f"{self.SNAPSHOT_DIR}/{snapshot_id}"
        result = self.ssh.run(f"rm -rf {snapshot_path}", sudo=True)
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
        paths: Optional[List[Path]] = None,
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
        manifest_file.write_text(
            json.dumps(
                {
                    "id": manifest.id,
                    "created_at": manifest.created_at,
                    "phase": manifest.phase,
                    "description": manifest.description,
                    "files": manifest.files,
                    "docker_containers": manifest.docker_containers,
                    "metadata": manifest.metadata,
                },
                indent=2,
            )
        )

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
                if path.exists():
                    if path.is_dir():
                        import shutil

                        shutil.rmtree(path)
                    else:
                        path.unlink()
                # Extract
                with tarfile.open(tar_path, "r:gz") as tar:
                    tar.extractall(path.parent)

        return True
