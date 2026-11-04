"""Repository integrity stage: tracked content, versions, links, generated drift."""

from __future__ import annotations

import time

from ..config import StageConfig
from ..context import Context
from ..gates import GateResult, StageResult, failed, passed, skipped, stage_result, warning
from ..repository import (
    check_lockfiles,
    check_markdown_links,
    check_release_notes,
    check_required_paths,
    check_tracked_files,
    check_versions,
)
from ..util import git_is_ignored, run_git, tracked_files
from .base import Echo, disabled_gate, disabled_skip_reason, run_stage_commands


def _repository_stage(context: Context) -> StageConfig:
    configured = context.config.stage("repository")
    return configured or StageConfig(name="repository", required=True, timeout_seconds=300)


def _license_gate(context: Context) -> GateResult:
    root = context.root
    candidates = ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md"]
    found = next((name for name in candidates if (root / name).is_file()), None)
    if found:
        return passed(
            "repository.license",
            "repository",
            "license file",
            required=False,
            summary=found,
        )
    return warning(
        "repository.license",
        "repository",
        "license file",
        summary="no LICENSE/COPYING file found",
        remediation="Add a license file so downstream users know the terms.",
    )


def _release_notes_gate(context: Context, required: bool) -> GateResult:
    """Refuse a changelog that lists the same release twice.

    The changelog is copied into every release bundle and checksummed, so a
    duplicated section is a defect in the shipped artifact. It is also the
    signature of a specific mistake: a hand-written prepend that re-emits the
    previous release's heading as the anchor for the new section. That mistake
    shipped in five of this repository's own releases before the gate existed
    (see decisions.md D16), which is why structure is a gate rather than a
    review note.
    """
    relative = context.config.release.changelog
    if not relative:
        return skipped(
            "repository.release-notes",
            "repository",
            "release notes",
            summary="no changelog configured",
        )
    if not (context.root / relative).is_file():
        # Existence is the release stage's business (`require_notes`); this
        # gate owns structure only, so a project that has not written notes
        # yet is not warned on every run.
        return skipped(
            "repository.release-notes",
            "repository",
            "release notes",
            summary=f"{relative} is not present",
        )

    headings, duplicates = check_release_notes(context.root, relative)
    if duplicates:
        return failed(
            "repository.release-notes",
            "repository",
            "release notes",
            required=required,
            summary="; ".join(f"line {item.line} repeats {item.text}" for item in duplicates[:5]),
            details={
                "changelog": relative,
                "duplicates": [{"line": item.line, "heading": item.text} for item in duplicates],
            },
            remediation=(
                f"A changelog lists each release once: delete the repeated heading(s) in "
                f"{relative}. This is usually the previous release's heading re-emitted "
                "while prepending the new section."
            ),
        )
    return passed(
        "repository.release-notes",
        "repository",
        "release notes",
        required=False,
        summary=f"{len(headings)} release section(s); no duplicate headings",
        details={
            "changelog": relative,
            "newest": headings[0].text if headings else None,
        },
    )


def run_repository_stage(context: Context, echo: Echo) -> StageResult:
    started = time.monotonic()
    stage = _repository_stage(context)
    if not stage.enabled:
        return stage_result(
            "repository",
            [disabled_gate(stage=stage)],
            duration_seconds=time.monotonic() - started,
            skipped_reason=disabled_skip_reason(stage),
        )
    required = stage.required
    config = context.config
    gates: list[GateResult] = []
    tracked = tracked_files(context.root)
    if context.git.available and not tracked:
        gates.append(
            failed(
                "repository.tracked",
                "repository",
                "tracked files",
                required=required,
                summary="the Git repository contains no tracked files",
                remediation="Add and commit the project before running CI.",
            )
        )
    elif context.git.available:
        gates.append(
            passed(
                "repository.tracked",
                "repository",
                "tracked files",
                required=required,
                summary=f"{len(tracked)} tracked file(s)",
            )
        )
    else:
        gates.append(
            skipped(
                "repository.tracked",
                "repository",
                "tracked files",
                summary="not a Git repository (policy.allow_no_git=true)",
            )
        )

    if config.policy.require_clean_worktree == "always":
        gates.append(
            passed(
                "repository.clean",
                "repository",
                "clean working tree",
                required=True,
                summary="clean" if not context.git.dirty else "dirty (precheck reports details)",
            )
        )
    elif config.policy.require_clean_worktree == "release" and context.git.dirty:
        gates.append(
            warning(
                "repository.clean",
                "repository",
                "clean working tree",
                summary=(
                    f"dirty: {len(context.git.dirty_files)} path(s); release will refuse "
                    "until committed"
                ),
                details={"paths": context.git.dirty_files[:100]},
                remediation="Commit changes before `./ci release`.",
            )
        )
    else:
        gates.append(
            passed(
                "repository.clean",
                "repository",
                "clean working tree",
                required=False,
                summary="clean" if not context.git.dirty else "dirty allowed by policy",
            )
        )

    missing = check_required_paths(context.root, config.repository.required_paths)
    if missing:
        gates.append(
            failed(
                "repository.required-paths",
                "repository",
                "required paths",
                required=required,
                summary=f"missing: {', '.join(missing)}",
                details={"missing": missing},
                remediation="Create the required paths or correct [repository].required_paths.",
            )
        )
    else:
        gates.append(
            passed(
                "repository.required-paths",
                "repository",
                "required paths",
                required=required,
                summary=(
                    f"{len(config.repository.required_paths)} required path(s) present"
                    if config.repository.required_paths
                    else "no required paths declared"
                ),
            )
        )

    file_report = check_tracked_files(
        context.root,
        tracked,
        forbidden_patterns=config.repository.forbidden_paths,
        max_file_bytes=int(config.repository.max_tracked_file_mib * 1024 * 1024),
        conflict_markers=config.repository.check_conflict_markers,
    )
    forbidden = file_report["forbidden"]
    conflicts = file_report["conflicts"]
    large = file_report["large"]
    assert isinstance(forbidden, list)
    assert isinstance(conflicts, list)
    assert isinstance(large, list)
    if forbidden:
        gates.append(
            failed(
                "repository.forbidden-files",
                "repository",
                "forbidden tracked files",
                required=required,
                summary=(
                    f"{len(forbidden)} forbidden path(s): "
                    f"{', '.join(str(item) for item in forbidden[:10])}"
                ),
                details={"paths": forbidden},
                remediation=(
                    "Remove the files from Git (`git rm --cached <path>`), rotate any exposed "
                    "credential, and extend .gitignore."
                ),
            )
        )
    else:
        gates.append(
            passed(
                "repository.forbidden-files",
                "repository",
                "forbidden tracked files",
                required=required,
                summary="none",
            )
        )

    if conflicts:
        gates.append(
            failed(
                "repository.conflict-markers",
                "repository",
                "conflict markers",
                required=required,
                summary=f"{len(conflicts)} file(s) contain conflict markers",
                details={"paths": conflicts},
                remediation="Resolve the merge conflict and remove every marker.",
            )
        )
    else:
        gates.append(
            passed(
                "repository.conflict-markers",
                "repository",
                "conflict markers",
                required=required,
                summary="none",
            )
        )

    if large:
        gates.append(
            warning(
                "repository.large-files",
                "repository",
                "large tracked files",
                summary=(
                    f"{len(large)} file(s) exceed {config.repository.max_tracked_file_mib} MiB"
                ),
                details={
                    "files": [
                        {"path": name, "size_bytes": size}
                        for name, size in large[:50]
                        if isinstance(name, str)
                    ]
                },
                remediation=(
                    "Use Git LFS or keep large binaries out of the repository. This is a "
                    "warning because legitimate binaries exist; the threshold is configurable."
                ),
            )
        )
    else:
        gates.append(
            passed(
                "repository.large-files",
                "repository",
                "large tracked files",
                required=False,
                summary=f"none above {config.repository.max_tracked_file_mib} MiB",
            )
        )

    version_problems = check_versions(config)
    if version_problems:
        gates.append(
            failed(
                "repository.versions",
                "repository",
                "version consistency",
                required=required,
                summary="; ".join(
                    f"{problem.source}: {problem.reason}" for problem in version_problems[:8]
                ),
                details={
                    "problems": [
                        {
                            "source": problem.source,
                            "expected": problem.expected,
                            "actual": problem.actual,
                            "reason": problem.reason,
                        }
                        for problem in version_problems
                    ]
                },
                remediation=(
                    "Make every declared version source match project.version (or the "
                    "version_file). Release scripts depend on one unambiguous version."
                ),
            )
        )
    else:
        gates.append(
            passed(
                "repository.versions",
                "repository",
                "version consistency",
                required=required,
                summary=(
                    f"{len(config.repository.versions)} source(s) match {context.version}"
                    if config.repository.versions
                    else "no additional version sources"
                ),
            )
        )

    gates.append(_release_notes_gate(context, required))

    lock_report = check_lockfiles(context.root, config.repository.required_lockfiles, tracked)
    if lock_report["missing"] or lock_report["untracked"]:
        problems = [
            *(f"missing: {name}" for name in lock_report["missing"]),
            *(f"untracked: {name}" for name in lock_report["untracked"]),
        ]
        gates.append(
            failed(
                "repository.lockfiles",
                "repository",
                "lockfiles",
                required=required,
                summary="; ".join(problems),
                details=lock_report,
                remediation="Commit the lockfile; dependency resolution must be reproducible.",
            )
        )
    else:
        gates.append(
            passed(
                "repository.lockfiles",
                "repository",
                "lockfiles",
                required=required,
                summary=(
                    f"{len(config.repository.required_lockfiles)} lockfile(s) present"
                    if config.repository.required_lockfiles
                    else "no lockfiles required"
                ),
            )
        )

    if config.repository.check_links == "off":
        gates.append(
            skipped(
                "repository.links",
                "repository",
                "Markdown links",
                summary="check_links=off",
            )
        )
    else:
        checked, link_problems = check_markdown_links(
            context.root,
            tracked,
            globs=config.repository.link_globs,
            ignore=config.repository.link_ignore,
        )
        if link_problems:
            summary = f"{len(link_problems)} broken link(s) across {checked} Markdown file(s)"
            details = {
                "problems": [
                    {
                        "source": problem.source,
                        "line": problem.line,
                        "target": problem.target,
                        "reason": problem.reason,
                    }
                    for problem in link_problems[:100]
                ]
            }
            remediation = "Fix the relative link or remove it; generated placeholders are skipped."
            if config.repository.check_links == "block":
                gates.append(
                    failed(
                        "repository.links",
                        "repository",
                        "Markdown links",
                        required=required,
                        summary=summary,
                        details=details,
                        remediation=remediation,
                    )
                )
            else:
                gates.append(
                    warning(
                        "repository.links",
                        "repository",
                        "Markdown links",
                        summary=summary,
                        details=details,
                        remediation=remediation,
                    )
                )
        else:
            gates.append(
                passed(
                    "repository.links",
                    "repository",
                    "Markdown links",
                    required=required,
                    summary=f"{checked} Markdown file(s) checked; no broken relative links",
                )
            )

    if config.repository.check_license:
        gates.append(_license_gate(context))

    if config.repository.generated_checks:
        if not context.git.available:
            gates.append(
                warning(
                    "repository.generated",
                    "repository",
                    "generated file drift",
                    summary="generated_checks require Git to detect drift",
                )
            )
        else:
            head_status = run_git(context.root, ["status", "--porcelain=v1"])
            before = set(head_status.stdout.splitlines())
            check_stage = StageConfig(
                name="repository",
                required=False,
                timeout_seconds=stage.timeout_seconds,
                cwd=stage.cwd,
                env=stage.env,
            )
            command_gates, _ = run_stage_commands(
                context=context,
                stage=check_stage,
                echo=echo,
                commands=config.repository.generated_checks,
                gate_stage="repository-generated",
                stop_on_failure=True,
            )
            after = set(run_git(context.root, ["status", "--porcelain=v1"]).stdout.splitlines())
            changed = sorted(line[3:].strip() for line in after - before if line[3:].strip())
            if any(gate.failed for gate in command_gates):
                for gate in command_gates:
                    gate.id = "repository.generated"
                    gate.title = f"generated check: {gate.command or gate.title}"
                    gates.append(gate)
            elif changed:
                gates.append(
                    failed(
                        "repository.generated",
                        "repository",
                        "generated file drift",
                        required=required,
                        summary=(f"generation changed {len(changed)} path(s); commit the result"),
                        details={"changes": changed[:100]},
                        remediation=(
                            "Run the generator locally, review the diff, and commit it. "
                            "Generated output must match the committed source."
                        ),
                    )
                )
            else:
                gates.append(
                    passed(
                        "repository.generated",
                        "repository",
                        "generated file drift",
                        required=required,
                        summary=(
                            f"{len(config.repository.generated_checks)} generator(s) "
                            "produced no drift"
                        ),
                    )
                )
    else:
        gates.append(
            skipped(
                "repository.generated",
                "repository",
                "generated file drift",
                summary="no generated_checks configured",
            )
        )

    if context.git.available and not git_is_ignored(context.root, ".ci/"):
        gates.append(
            warning(
                "repository.state-ignored",
                "repository",
                "CI state is ignored",
                summary=".ci/ is not ignored by Git",
                remediation="Add `.ci/` and `.localci/` to .gitignore.",
            )
        )

    return stage_result(
        "repository",
        gates,
        duration_seconds=time.monotonic() - started,
    )
