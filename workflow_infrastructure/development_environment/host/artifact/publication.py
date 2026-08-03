"""Build and publish content-addressed host-bootstrap objects."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
from typing import Protocol

from workflow_infrastructure.development_environment.aws import aws_cli_error_matches
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)
from workflow_infrastructure.development_environment.host.artifact.download import (
    HostArtifactDownloader,
)
from workflow_infrastructure.development_environment.host.artifact.model import (
    HostArtifactResolution,
)

_BOOTSTRAP_SOURCE_RELATIVE_PATH_LIST = [
    Path("host_bootstrap.py"),
    Path("workflow_infrastructure/__init__.py"),
    Path("workflow_infrastructure/development_environment/__init__.py"),
    Path("workflow_infrastructure/development_environment/clock.py"),
    Path("workflow_infrastructure/development_environment/error.py"),
    Path("workflow_infrastructure/development_environment/host/__init__.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/__init__.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/artifacts.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/command.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/k3s.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/manager.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/manifest.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/network.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/services.py"),
    Path("workflow_infrastructure/development_environment/host/bootstrap/storage.py"),
]


class AwsClientProtocol(Protocol):
    """AWS operations required by content-addressed publication."""

    def run(self, aws_argument_list: list[str], *, check: bool = True):
        """Run one AWS CLI operation.

        Args:
            aws_argument_list: Ordered AWS argument values.
            check: Whether a nonzero command exit raises an error.
        """


class HostBootstrapObjectPublisher:
    """Own deterministic bootstrap archives and immutable S3 publication."""

    def __init__(
        self,
        *,
        aws: AwsClientProtocol,
        aws_region: str,
        cache_root_path: Path,
        project_root_path: Path,
    ) -> None:
        """Bind publication to one project checkout and AWS environment.

        Args:
            aws: Configured AWS CLI boundary.
            aws_region: Exact development region.
            cache_root_path: Resolver cache containing verified bytes.
            project_root_path: Exact infrastructure source checkout.
        """

        self._aws = aws
        self._aws_region = aws_region
        self._downloader = HostArtifactDownloader(cache_root_path=cache_root_path)
        self._project_root_path = project_root_path

    def publish(self, *, bucket_name: str, resolution: HostArtifactResolution) -> dict[str, str]:
        """Publish exact Python and bootstrap archives and return compute inputs.

        Args:
            bucket_name: Environment-owned private artifact bucket.
            resolution: Verified architecture-specific artifact graph.

        Returns:
            Exact CloudFormation parameter map.
        """

        python_artifact = resolution.artifact_by_name_map["python"]
        python_path = self._downloader.cache_path_get(python_artifact.url)
        if not python_path.is_file() or hashlib.sha256(python_path.read_bytes()).hexdigest() != python_artifact.sha256:
            raise DevelopmentEnvironmentError("Verified Python artifact cache is unavailable")
        with tempfile.TemporaryDirectory(prefix="host-bootstrap-publication-") as temporary_directory:
            bundle_path = Path(temporary_directory) / "bootstrap.tar.gz"
            bootstrap_manifest_sha256 = self._bundle_write(bundle_path=bundle_path, resolution=resolution)
            bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            python_key = f"host-bootstrap/python/sha256/{python_artifact.sha256}/python.tar.gz"
            bundle_key = f"host-bootstrap/bundle/sha256/{bundle_sha256}/bootstrap.tar.gz"
            self._object_publish(
                bucket_name=bucket_name,
                key=python_key,
                path=python_path,
                sha256=python_artifact.sha256,
            )
            self._object_publish(
                bucket_name=bucket_name,
                key=bundle_key,
                path=bundle_path,
                sha256=bundle_sha256,
            )
        source_url_prefix = f"https://{bucket_name}.s3.{self._aws_region}.amazonaws.com"
        return {
            **resolution.cloudformation_parameter_by_name_map_get(),
            "HostBootstrapBundleSha256": bundle_sha256,
            "HostBootstrapBundleSourceInfo": json.dumps(
                {"path": f"{source_url_prefix}/{bundle_key}"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            "HostBootstrapManifestSha256": bootstrap_manifest_sha256,
            "HostPythonArtifactSha256": python_artifact.sha256,
            "HostPythonArtifactSourceInfo": json.dumps(
                {"path": f"{source_url_prefix}/{python_key}"},
                separators=(",", ":"),
                sort_keys=True,
            ),
        }

    def _bundle_write(self, *, bundle_path: Path, resolution: HostArtifactResolution) -> str:
        """Write one deterministic archive and return its inner manifest identity.

        Args:
            bundle_path: Destination gzip tar archive.
            resolution: Verified artifact graph.

        Returns:
            SHA-256 of canonical bootstrap manifest bytes.
        """

        host_artifact_manifest_bytes = json.dumps(
            resolution.manifest_payload_get(), separators=(",", ":"), sort_keys=True
        ).encode()
        artifact_by_name_map: dict[str, dict[str, object]] = {}
        for name, artifact in sorted(resolution.artifact_by_name_map.items()):
            if name == "python":
                continue
            bundle_relative_path = artifact.bundle_relative_path_get()
            artifact_path = self._downloader.cache_path_get(artifact.url)
            artifact_bytes = artifact_path.read_bytes()
            if len(artifact_bytes) != artifact.size or hashlib.sha256(artifact_bytes).hexdigest() != artifact.sha256:
                raise DevelopmentEnvironmentError(f"Verified host artifact cache is unavailable for {name}")
            artifact_by_name_map[name] = {
                "path": bundle_relative_path.as_posix(),
                "sha256": artifact.sha256,
                "size": artifact.size,
                "version": artifact.version,
            }
        bootstrap_manifest_bytes = json.dumps(
            {
                "architecture": resolution.architecture,
                "artifact_by_name_map": artifact_by_name_map,
                "host_artifact_manifest_sha256": resolution.manifest_sha256_get(),
                "python_version": resolution.artifact_by_name_map["python"].version,
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        archive_entry_by_path_map: dict[PurePosixPath, bytes] = {
            PurePosixPath("bootstrap-manifest.json"): bootstrap_manifest_bytes,
            PurePosixPath("host-artifact-manifest.json"): host_artifact_manifest_bytes,
        }
        for relative_path in _BOOTSTRAP_SOURCE_RELATIVE_PATH_LIST:
            source_path = self._project_root_path / relative_path
            if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_nlink != 1:
                raise DevelopmentEnvironmentError(f"Bootstrap source is unavailable: {relative_path.as_posix()}")
            archive_entry_by_path_map[PurePosixPath(relative_path.as_posix())] = source_path.read_bytes()
        for name, artifact in sorted(resolution.artifact_by_name_map.items()):
            if name != "python":
                archive_entry_by_path_map[artifact.bundle_relative_path_get()] = self._downloader.cache_path_get(
                    artifact.url
                ).read_bytes()
        with bundle_path.open("wb") as file:
            with gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as gzip_file:
                with tarfile.open(fileobj=gzip_file, mode="w") as archive:
                    for archive_path, data in sorted(
                        archive_entry_by_path_map.items(),
                        key=lambda item: item[0].as_posix(),
                    ):
                        info = tarfile.TarInfo(archive_path.as_posix())
                        info.gid = 0
                        info.gname = ""
                        info.mode = 0o755 if archive_path == PurePosixPath("host_bootstrap.py") else 0o644
                        info.mtime = 0
                        info.size = len(data)
                        info.uid = 0
                        info.uname = ""
                        archive.addfile(info, io.BytesIO(data))
        return hashlib.sha256(bootstrap_manifest_bytes).hexdigest()

    def _object_publish(self, *, bucket_name: str, key: str, path: Path, sha256: str) -> None:
        """Create one immutable S3 object or prove an identical object exists.

        Args:
            bucket_name: Exact environment bucket.
            key: Content-addressed object key.
            path: Local ordinary file.
            sha256: Exact lowercase digest.
        """

        checksum_base64 = base64.b64encode(bytes.fromhex(sha256)).decode()
        result = self._aws.run(
            [
                "s3api",
                "head-object",
                "--bucket",
                bucket_name,
                "--key",
                key,
                "--checksum-mode",
                "ENABLED",
            ],
            check=False,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise DevelopmentEnvironmentError("Existing host-bootstrap object metadata is malformed") from error
            if payload.get("ChecksumSHA256") != checksum_base64 or payload.get("ContentLength") != path.stat().st_size:
                raise DevelopmentEnvironmentError("Content-addressed host-bootstrap object differs from local bytes")
            return
        if not aws_cli_error_matches(
            result,
            code_set=frozenset({"404", "NoSuchKey"}),
            operation="HeadObject",
        ):
            raise DevelopmentEnvironmentError("Unable to inspect content-addressed host-bootstrap object")
        self._aws.run(
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket_name,
                "--key",
                key,
                "--body",
                str(path),
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum_base64,
            ]
        )
        verification_result = self._aws.run(
            [
                "s3api",
                "head-object",
                "--bucket",
                bucket_name,
                "--key",
                key,
                "--checksum-mode",
                "ENABLED",
            ]
        )
        try:
            verification_payload = json.loads(verification_result.stdout)
        except json.JSONDecodeError as error:
            raise DevelopmentEnvironmentError("Published host-bootstrap object metadata is malformed") from error
        if verification_payload.get("ChecksumSHA256") != checksum_base64:
            raise DevelopmentEnvironmentError("Published host-bootstrap object checksum differs")
