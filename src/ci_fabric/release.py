"""Local release registry lookup and verification."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, validate_pep440, validate_semver
from .context import Context
from .errors import CiError
from .runner import run_command
from .supplychain import load_manifest, verify_checksums, write_checksums
from .util import (
    ensure_dir,
    file_sha256,
    is_within,
    isoformat,
    read_text,
    relpath,
    remove_path,
    run_git,
    utc_now,
    version_key,
    write_json,
    write_text_atomic,
)


@dataclass(slots=True)
class ReleaseBundle:
    version: str
    directory: Path
    manifest: dict[str, Any]
    metadata: dict[str, Any]
    notes: str


@dataclass(slots=True)
class ReleaseRecord:
    version: str
    directory: Path
    manifest_path: Path
    checksums_path: Path
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    tag: str | None = None
    notes_source: str | None = None
    generated_notes: bool = False
    created_at: str = ""

    def to_dict(self, root: Path) -> dict[str, Any]:
        return {
            "version": self.version,
            "directory": relpath(self.directory, root),
            "manifest": relpath(self.manifest_path, root),
            "checksums": relpath(self.checksums_path, root),
            "artifacts": self.artifacts,
            "tag": self.tag,
            "notes_source": self.notes_source,
            "generated_notes": self.generated_notes,
            "created_at": self.created_at,
        }


def releases_root(root: Path, config: Config) -> Path:
    return (root / config.release.output_dir).resolve()


def release_directory(root: Path, config: Config, version: str) -> Path:
    candidate = (releases_root(root, config) / version).resolve()
    try:
        candidate.relative_to(releases_root(root, config))
    except ValueError as exc:
        raise CiError(f"invalid release version: {version!r}") from exc
    return candidate


def list_releases(root: Path, config: Config) -> list[str]:
    base = releases_root(root, config)
    if not base.is_dir():
        return []
    return sorted(
        [path.name for path in base.iterdir() if path.is_dir() and not path.name.startswith(".")],
        key=version_key,
    )


def _latest_version(root: Path, config: Config) -> str | None:
    base = releases_root(root, config)
    pointer = base / "latest.json"
    if pointer.is_file():
        try:
            data = json.loads(read_text(pointer))
            version = data.get("version") if isinstance(data, dict) else None
            if isinstance(version, str) and (base / version).is_dir():
                return version
        except (OSError, json.JSONDecodeError):
            pass
    versions = list_releases(root, config)
    return versions[-1] if versions else None


def verify_release_bundle(bundle: ReleaseBundle) -> list[str]:
    errors: list[str] = []
    if not bundle.directory.is_dir():
        return [f"release directory is missing: {bundle.directory}"]
    checksums = bundle.directory / "SHA256SUMS"
    errors.extend(verify_checksums(checksums, bundle.directory))
    artifacts = bundle.manifest.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("release manifest contains no artifacts")
        return errors
    for descriptor in artifacts:
        if not isinstance(descriptor, dict):
            errors.append("malformed artifact descriptor")
            continue
        relative = descriptor.get("path")
        expected = descriptor.get("sha256")
        if not isinstance(relative, str):
            errors.append("artifact has no path")
            continue
        path = (bundle.directory / relative).resolve()
        if not is_within(path, bundle.directory):
            errors.append(f"artifact escapes release directory: {relative}")
            continue
        if not path.is_file():
            errors.append(f"artifact is missing: {relative}")
            continue
        digest, _ = file_sha256(path)
        if isinstance(expected, str) and digest != expected:
            errors.append(f"artifact checksum mismatch: {relative}")
    return errors


def resolve_release(
    root: Path,
    config: Config,
    requested: str | None = None,
) -> ReleaseBundle:
    version = requested
    if version in {None, "", "latest"}:
        version = _latest_version(root, config)
    if not version:
        raise CiError(
            "no local release is available",
            remediation=(
                "Create one explicitly with `./ci release --version X.Y.Z` and then deploy. "
                "Deployment never silently rebuilds a different artifact."
            ),
        )
    directory = release_directory(root, config, version)
    if not directory.is_dir():
        available = ", ".join(list_releases(root, config)) or "none"
        raise CiError(
            f"release not found: {version}",
            remediation=f"Available local releases: {available}.",
        )
    manifest_path = directory / "artifact-manifest.json"
    manifest = load_manifest(manifest_path)
    if manifest.get("kind") != "package":
        raise CiError(f"release manifest is not a package manifest: {manifest_path}")
    metadata: dict[str, Any] = {}
    metadata_path = directory / "release.json"
    if metadata_path.is_file():
        try:
            loaded = json.loads(read_text(metadata_path))
            if isinstance(loaded, dict):
                metadata = loaded
        except (OSError, json.JSONDecodeError) as exc:
            raise CiError(f"invalid release metadata {metadata_path}: {exc}") from exc
    notes_path = directory / "RELEASE_NOTES.md"
    notes = read_text(notes_path) if notes_path.is_file() else ""
    bundle = ReleaseBundle(
        version=version,
        directory=directory,
        manifest=manifest,
        metadata=metadata,
        notes=notes,
    )
    errors = verify_release_bundle(bundle)
    if errors:
        raise CiError(
            "release verification failed: " + "; ".join(errors[:8]),
            remediation="Do not deploy a release whose checksums or manifest are inconsistent.",
        )
    return bundle


def select_artifact(bundle: ReleaseBundle, pattern: str | None) -> Path:
    artifacts = bundle.manifest.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        raise CiError("release has no artifacts")
    candidates: list[Path] = []
    for descriptor in artifacts:
        if not isinstance(descriptor, dict):
            continue
        relative = descriptor.get("path")
        if not isinstance(relative, str):
            continue
        path = (bundle.directory / relative).resolve()
        if pattern and not (
            fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern)
        ):
            continue
        candidates.append(path)
    if not candidates:
        raise CiError(
            f"no release artifact matched {pattern!r}",
            remediation="Check deploy.<env>.artifact_name and the release manifest.",
        )
    archives = [
        path for path in candidates if path.name.endswith((".tar", ".tar.gz", ".tgz", ".zip"))
    ]
    return (archives or candidates)[0]


def release_summary(bundle: ReleaseBundle) -> dict[str, Any]:
    return {
        "version": bundle.version,
        "directory": str(bundle.directory),
        "manifest": str(bundle.directory / "artifact-manifest.json"),
        "artifacts": [
            descriptor.get("path")
            for descriptor in bundle.manifest.get("artifacts", [])
            if isinstance(descriptor, dict)
        ],
        "notes_chars": len(bundle.notes),
        "source_revision": bundle.manifest.get("revision"),
    }


def relative_release_path(root: Path, config: Config, version: str) -> str:
    return relpath(release_directory(root, config, version), root)


def _validate_release_version(config: Config, version: str) -> None:
    fmt = config.project.version_format
    if fmt == "semver" and not validate_semver(version):
        raise CiError(f"release version is not valid semver: {version!r}")
    if fmt == "pep440" and not validate_pep440(version):
        raise CiError(f"release version is not valid PEP 440: {version!r}")
    if version in {".", ".."} or "/" in version or "\\" in version:
        raise CiError(f"release version contains an unsupported path character: {version!r}")


def _release_notes(
    context: Context,
    config: Config,
    version: str,
    notes_file: str | None,
) -> tuple[str, str | None, bool]:
    root = context.root
    source: str | None = None
    if notes_file:
        candidate = Path(notes_file)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not is_within(candidate, root):
            raise CiError(f"release notes file escapes the repository: {notes_file}")
        if not candidate.is_file():
            raise CiError(f"release notes file not found: {notes_file}")
        source = relpath(candidate, root)
        text = read_text(candidate)
    elif config.release.changelog:
        changelog = (root / config.release.changelog).resolve()
        if is_within(changelog, root) and changelog.is_file():
            text = read_text(changelog)
            source = config.release.changelog
        else:
            text = ""
    else:
        text = ""
    if not text.strip():
        if config.release.require_notes:
            raise CiError(
                "release notes are required but no notes were found",
                remediation=(
                    "Write RELEASE_NOTES.md, pass --notes-file, or set "
                    "[release].require_notes = false deliberately."
                ),
            )
        text = f"# {config.project.name} {version}\n\nNo release notes were supplied.\n"
        return text, None, True
    return text, source, False


def _effective_signer(
    config: Config,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Resolve the signer into (kind, sign_command, verify_command, identity, issuer).

    Returns ``("none", None, None, None, None)`` for an unsigned release, the
    BYO commands for ``sign_command``, or the cosign-keyless command pair with
    the RESOLVED identity and issuer (config value, else environment — hosted
    releases supply a per-tag identity via COSIGN_CERTIFICATE_IDENTITY, which
    a static ci.toml value cannot express). Raises ``CiError`` for a
    misconfigured or unavailable signer so a release never silently ships
    unsigned when a signer was requested.
    """
    if config.release.sign_command:
        return "external", config.release.sign_command, config.release.verify_command, None, None
    if config.release.signer == "cosign-keyless":
        identity = config.release.signing_identity or os.environ.get("COSIGN_CERTIFICATE_IDENTITY")
        issuer = config.release.signing_issuer or os.environ.get("COSIGN_CERTIFICATE_OIDC_ISSUER")
        if not identity or not issuer:
            raise CiError(
                "cosign-keyless requires a signing identity and OIDC issuer",
                remediation=(
                    "Set [release].signing_identity and [release].signing_issuer in "
                    "ci.toml (for example the email/GitHub identity cosign will "
                    "issue the certificate for, and https://token.actions."
                    "githubusercontent.com or https://oauth2.sigstore.dev/auth), "
                    "or export COSIGN_CERTIFICATE_IDENTITY / "
                    "COSIGN_CERTIFICATE_OIDC_ISSUER."
                ),
            )
        if shutil.which("cosign") is None:
            raise CiError(
                "cosign is not installed or not on PATH",
                remediation=(
                    "Install cosign (https://docs.sigstore.dev/cosign/system_config/"
                    'installation/) or switch [release].signer to "none".'
                ),
            )
        bundle_name = "SHA256SUMS.sigstore.json"
        sign_command = f"cosign sign-blob --yes --bundle {bundle_name} SHA256SUMS"
        verify_command = (
            f"cosign verify-blob --bundle {bundle_name} "
            f"--certificate-identity {shlex.quote(identity)} "
            f"--certificate-oidc-issuer {shlex.quote(issuer)} SHA256SUMS"
        )
        return "cosign-keyless", sign_command, verify_command, identity, issuer
    if config.release.signer not in {"none", ""}:
        raise CiError(
            f"unknown signer: {config.release.signer!r}",
            remediation='Use signer = "cosign-keyless" or "none".',
        )
    return "none", None, None, None, None


def create_release(
    context: Context,
    *,
    version: str | None = None,
    tag: bool = False,
    notes_file: str | None = None,
    force: bool = False,
    echo: Any = print,
) -> ReleaseRecord:
    config = context.config
    root = context.root
    resolved = version or config.project.version
    if not resolved:
        raise CiError(
            "no release version is available",
            remediation="Set project.version (or version_file) in ci.toml, or pass --version.",
        )
    resolved = resolved.strip().lstrip("vV") if version else resolved.strip()
    _validate_release_version(config, resolved)
    if config.release.require_clean and context.git.dirty:
        raise CiError(
            f"working tree is dirty; refusing to create release {resolved}",
            remediation=(
                "Commit or stash changes first. Set [release].require_clean = false only "
                "for a deliberate non-reproducible local snapshot."
            ),
        )
    package_manifest_path = context.paths.artifacts_dir / "package-manifest.json"
    if not package_manifest_path.is_file():
        raise CiError(
            "this run produced no package artifact to release",
            remediation=(
                "Run the full pipeline first (`./ci ci`), or run `./ci release`, which "
                "executes build/package/artifact validation before creating the release."
            ),
        )
    manifest = load_manifest(package_manifest_path)
    descriptors = manifest.get("artifacts")
    if not isinstance(descriptors, list) or not descriptors:
        raise CiError("package manifest contains no artifacts")
    checksum_errors = verify_checksums(
        context.paths.artifacts_dir / "SHA256SUMS",
        context.paths.artifacts_dir,
    )
    if checksum_errors:
        raise CiError(
            "package checksums do not verify: " + "; ".join(checksum_errors[:5]),
            remediation="Do not release an artifact whose checksums are inconsistent.",
        )

    notes_text, notes_source, generated_notes = _release_notes(
        context, config, resolved, notes_file
    )

    releases = releases_root(root, config)
    ensure_dir(releases)
    target = release_directory(root, config, resolved)
    if target.exists() and not force:
        raise CiError(
            f"release {resolved} already exists: {relpath(target, root)}",
            remediation=(
                "Choose a new version or pass --force to replace the local bundle deliberately."
            ),
        )
    token = os.urandom(4).hex()
    temp = releases / f".{resolved}.{token}.tmp"
    backup: Path | None = None
    if temp.exists():
        remove_path(temp)
    ensure_dir(temp)

    try:
        released_artifacts: list[dict[str, Any]] = []
        used_names: set[str] = set()
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise CiError("package manifest contains a malformed artifact descriptor")
            relative = descriptor.get("path")
            expected = descriptor.get("sha256")
            if not isinstance(relative, str):
                raise CiError("package manifest artifact has no path")
            source = (context.paths.artifacts_dir / relative).resolve()
            if not is_within(source, context.paths.artifacts_dir) or not source.is_file():
                raise CiError(f"packaged artifact is missing or unsafe: {relative}")
            digest, size = file_sha256(source)
            if isinstance(expected, str) and digest != expected:
                raise CiError(f"packaged artifact changed after validation: {relative}")
            name = source.name
            if name in used_names:
                raise CiError(
                    f"two package artifacts share the name {name!r}; release layout is flat",
                    remediation="Give each package output a unique file name.",
                )
            used_names.add(name)
            shutil.copy2(source, temp / name)
            released_artifacts.append(
                {
                    "name": name,
                    "path": name,
                    "sha256": digest,
                    "size": size,
                    "media_type": descriptor.get("media_type", "application/octet-stream"),
                }
            )

        extra_files: list[Path] = []
        for extra_name in ("sbom.cdx.json", "provenance.json"):
            candidate = context.paths.artifacts_dir / extra_name
            if candidate.is_file():
                shutil.copy2(candidate, temp / extra_name)
                extra_files.append(temp / extra_name)

        manifest_copy = json.loads(json.dumps(manifest))
        manifest_copy["artifacts"] = released_artifacts
        manifest_copy["checksums"] = "SHA256SUMS"
        manifest_copy["release"] = {
            "version": resolved,
            "created_at": isoformat(utc_now()),
            "run_id": context.run_id,
        }
        manifest_out = temp / "artifact-manifest.json"
        write_json(manifest_out, manifest_copy)
        notes_out = temp / "RELEASE_NOTES.md"
        write_text_atomic(notes_out, notes_text if notes_text.endswith("\n") else notes_text + "\n")

        created_at = isoformat(utc_now())
        (
            signer_kind,
            effective_sign_command,
            effective_verify_command,
            signing_identity,
            signing_issuer,
        ) = _effective_signer(config)
        release_metadata: dict[str, Any] = {
            "schema_version": 1,
            "project": config.project.name,
            "version": resolved,
            "created_at": created_at,
            "run_id": context.run_id,
            "revision": context.git.revision,
            "short_revision": context.git.short_revision,
            "dirty": context.git.dirty,
            "source_date_epoch": context.source_date_epoch,
            "profile": config.policy.profile,
            "artifact_sha256": released_artifacts[0]["sha256"],
            "artifact_names": [item["name"] for item in released_artifacts],
            "notes_source": notes_source,
            "generated_notes": generated_notes,
            "tag": f"{config.release.tag_prefix}{resolved}" if tag else None,
            "signing": (
                "signed (external command)"
                if signer_kind == "external"
                else "signed (cosign keyless)"
                if signer_kind == "cosign-keyless"
                else "none (no signer configured)"
            ),
            "signer": signer_kind,
            "attestation": (
                "provenance.json (covered by signed SHA256SUMS)"
                if effective_sign_command
                else "provenance.json (listed in SHA256SUMS; no signer configured)"
            ),
            "signing_bundle": (
                "SHA256SUMS.sigstore.json" if signer_kind == "cosign-keyless" else None
            ),
            "signing_identity": (signing_identity if signer_kind == "cosign-keyless" else None),
            "signing_issuer": (signing_issuer if signer_kind == "cosign-keyless" else None),
            "verify_command": (
                effective_verify_command if signer_kind == "cosign-keyless" else None
            ),
        }
        write_json(temp / "release.json", release_metadata)

        checksum_files = [
            *(temp / item["name"] for item in released_artifacts),
            *extra_files,
            manifest_out,
            notes_out,
            temp / "release.json",
        ]
        checksums_out = temp / "SHA256SUMS"
        write_checksums(checksum_files, checksums_out)
        errors = verify_checksums(checksums_out, temp)
        if errors:
            raise CiError("new release bundle failed checksum verification: " + "; ".join(errors))

        # Bring-your-own signing: run inside the finished bundle, then prove the
        # signer did not touch the checksummed content, then self-test the
        # signature with verify_command before anything is published.
        if effective_sign_command:
            echo("  signing release bundle")
            sign_log = context.paths.logs_dir / "release-sign-01.log"
            sign_result = run_command(
                context.render(effective_sign_command, extra={"bundle_dir": str(temp)}),
                cwd=temp,
                env=dict(context.base_env),
                timeout=300,
                log_path=sign_log,
            )
            if not sign_result.ok:
                raise CiError(
                    f"release signing failed: {sign_result.summary()}",
                    remediation=(
                        "Fix the [release].sign_command; a release without a valid "
                        "signature is never published. Log: " + relpath(sign_log, context.root)
                        if signer_kind == "external"
                        else (
                            "cosign keyless signing failed. Ensure an ambient OIDC "
                            "identity is available (GitHub Actions, Google, etc.) or "
                            "export SIGSTORE_ID_TOKEN. Log: " + relpath(sign_log, context.root)
                        )
                    ),
                )
            drifted = verify_checksums(checksums_out, temp)
            if drifted:
                raise CiError(
                    "the sign command modified checksummed bundle content: "
                    + "; ".join(drifted[:3]),
                    remediation=(
                        "Signers must add files only; the signature must cover "
                        "the exact checksummed content."
                    ),
                )
            if effective_verify_command:
                verify_log = context.paths.logs_dir / "release-verify-01.log"
                verify_result = run_command(
                    context.render(effective_verify_command, extra={"bundle_dir": str(temp)}),
                    cwd=temp,
                    env=dict(context.base_env),
                    timeout=300,
                    log_path=verify_log,
                )
                if not verify_result.ok:
                    raise CiError(
                        "release signature failed verification at release time: "
                        f"{verify_result.summary()}",
                        remediation=(
                            "Fix the sign/verify command pair; a signature that does not "
                            "verify is worse than none. Log: " + relpath(verify_log, context.root)
                        ),
                    )

        tag_name: str | None = None
        if tag:
            tag_name = f"{config.release.tag_prefix}{resolved}"
            if not context.git.available:
                raise CiError("cannot tag a release without a Git repository")
            check = run_git(root, ["rev-parse", "-q", "--verify", f"refs/tags/{tag_name}"])
            if check.returncode == 0:
                raise CiError(
                    f"tag already exists: {tag_name}",
                    remediation="Release tags are immutable; choose a new version.",
                )
            created = run_git(
                root,
                ["tag", "-a", tag_name, "-m", f"Release {config.project.name} {resolved}"],
            )
            if created.returncode != 0:
                raise CiError(
                    f"could not create tag {tag_name}: "
                    f"{(created.stderr or created.stdout).strip()}",
                    remediation="Check git identity (user.name/user.email) and tag permissions.",
                )

        if target.exists():
            backup = releases / f".{resolved}.old.{token}"
            target.rename(backup)
        try:
            temp.rename(target)
        except BaseException:
            if backup is not None and not target.exists():
                backup.rename(target)
            raise
        if backup is not None:
            with contextlib.suppress(OSError):
                remove_path(backup)

        pointer = releases / "latest.json"
        write_json(
            pointer,
            {
                "version": resolved,
                "updated_at": created_at,
                "tag": tag_name,
                "artifact_sha256": released_artifacts[0]["sha256"],
            },
        )
        _append_release_log(releases, context, resolved, tag_name)
        echo(f"  released {config.project.name} {resolved} -> {relpath(target, root)}")
        return ReleaseRecord(
            version=resolved,
            directory=target,
            manifest_path=target / "artifact-manifest.json",
            checksums_path=target / "SHA256SUMS",
            artifacts=released_artifacts,
            tag=tag_name,
            notes_source=notes_source,
            generated_notes=generated_notes,
            created_at=created_at,
        )
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _append_release_log(
    releases: Path, context: Context, version: str, tag_name: str | None
) -> None:
    line = json.dumps(
        {
            "at": isoformat(utc_now()),
            "version": version,
            "tag": tag_name,
            "revision": context.git.revision,
            "run_id": context.run_id,
        },
        sort_keys=True,
    )
    log = releases / "history.jsonl"
    ensure_dir(log.parent)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def verify_release(root: Path, config: Config, version: str | None = None) -> ReleaseBundle:
    return resolve_release(root, config, version)


def latest_release(root: Path, config: Config) -> ReleaseBundle | None:
    version = _latest_version(root, config)
    if version is None:
        return None
    return resolve_release(root, config, version)


def release_artifact_path(bundle: ReleaseBundle, name: str) -> Path:
    path = (bundle.directory / name).resolve()
    if not is_within(path, bundle.directory):
        raise CiError(f"release artifact escapes the release directory: {name}")
    if not path.is_file():
        raise CiError(f"release artifact not found: {name}")
    return path
