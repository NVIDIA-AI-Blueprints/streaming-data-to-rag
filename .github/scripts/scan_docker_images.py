#!/usr/bin/env python3
"""
Scan Docker images used in the repository and generate a report.

This script scans:
1. GitHub Actions workflow files for docker image references
2. Dockerfiles for base images
3. Docker Compose files for service images

It generates a JSON report with:
- Images used in Actions workflows (with latest tag resolution)
- Images in repo but not used in Actions workflows
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Scan Docker images in repository"
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=".",
        help="Repository root directory",
    )
    parser.add_argument(
        "--workflow-dir",
        type=str,
        default=".github/workflows",
        help="Directory containing workflow files",
    )
    parser.add_argument(
        "--exclude-dirs",
        type=str,
        nargs="*",
        default=[],
        help="Directories to exclude from scanning",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="docker-image-report.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--resolve-latest",
        action="store_true",
        default=True,
        help="Resolve latest tags using docker pull",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=os.environ.get("GITHUB_RUN_ID", "local"),
        help="GitHub Actions run ID",
    )
    parser.add_argument(
        "--run-number",
        type=str,
        default=os.environ.get("GITHUB_RUN_NUMBER", "0"),
        help="GitHub Actions run number",
    )
    parser.add_argument(
        "--repo-name",
        type=str,
        default=os.environ.get("GITHUB_REPOSITORY", "unknown"),
        help="GitHub repository name (owner/repo)",
    )
    return parser.parse_args()


class DockerImage:
    """Represents a Docker image reference."""

    def __init__(
        self,
        image: str,
        tag: str,
        source_file: str,
        source_line: int,
        is_local_build: bool = False,
        build_context: str | None = None,
    ):
        self.image = image
        self.tag = tag
        self.source_file = source_file
        self.source_line = source_line
        self.is_local_build = is_local_build
        self.build_context = build_context
        self.latest_resolved: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "image": self.image,
            "tag": self.tag,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "is_local_build": self.is_local_build,
        }
        if self.build_context:
            result["build_context"] = self.build_context
        if self.latest_resolved:
            result["latest_resolved"] = self.latest_resolved
        return result

    def full_reference(self) -> str:
        """Return full image reference."""
        return f"{self.image}:{self.tag}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DockerImage):
            return False
        return self.image == other.image and self.tag == other.tag

    def __hash__(self) -> int:
        return hash((self.image, self.tag))


def read_file_content(file_path: Path) -> tuple[str, list[str]]:
    """Read file content and return as string and lines."""
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        return content, lines
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return "", []


def parse_image_reference(ref: str) -> tuple[str, str]:
    """
    Parse an image reference into image name and tag.

    Examples:
        nvcr.io/nvidia/pytorch:23.08-py3 -> (nvcr.io/nvidia/pytorch, 23.08-py3)
        ubuntu -> (ubuntu, latest)
        myimage:v1.0 -> (myimage, v1.0)
    """
    ref = ref.strip().strip('"').strip("'")

    if "@" in ref:
        # Handle digest references like image@sha256:...
        parts = ref.split("@")
        return parts[0], parts[1]

    if ":" in ref:
        # Find the last colon (to handle registry:port/image:tag)
        last_colon = ref.rfind(":")
        # Check if it looks like a port number
        after_colon = ref[last_colon + 1 :]
        if "/" in after_colon:
            # This colon is part of registry:port, not a tag
            return ref, "latest"
        return ref[:last_colon], after_colon

    return ref, "latest"


def scan_workflow_files(
    repo_root: Path, workflow_dir: str
) -> list[DockerImage]:
    """Scan GitHub Actions workflow files for Docker image references."""
    images: list[DockerImage] = []
    workflow_path = repo_root / workflow_dir

    if not workflow_path.exists():
        print(f"Warning: Workflow directory not found: {workflow_path}", file=sys.stderr)
        return images

    for workflow_file in workflow_path.glob("*.yml"):
        content, lines = read_file_content(workflow_file)
        rel_path = str(workflow_file.relative_to(repo_root))

        # Pattern 1: docker pull <image>
        for i, line in enumerate(lines, 1):
            match = re.search(r"docker\s+pull\s+([^\s]+)", line)
            if match:
                ref = match.group(1)
                image, tag = parse_image_reference(ref)
                images.append(
                    DockerImage(
                        image=image,
                        tag=tag,
                        source_file=rel_path,
                        source_line=i,
                    )
                )

        # Pattern 2: docker build -t <name>
        for i, line in enumerate(lines, 1):
            match = re.search(r"docker\s+build\s+.*-t\s+([^\s]+)", line)
            if match:
                ref = match.group(1)
                image, tag = parse_image_reference(ref)
                # Find the Dockerfile context
                dockerfile_match = re.search(r"-f\s+([^\s]+)", line)
                build_context = dockerfile_match.group(1) if dockerfile_match else None
                images.append(
                    DockerImage(
                        image=image,
                        tag=tag,
                        source_file=rel_path,
                        source_line=i,
                        is_local_build=True,
                        build_context=build_context,
                    )
                )

        # Pattern 3: docker compose -f <file> ... (we need to parse the compose file)
        for i, line in enumerate(lines, 1):
            match = re.search(r"docker\s+compose\s+-f\s+([^\s]+)", line)
            if match:
                compose_file = match.group(1)
                compose_path = repo_root / compose_file
                if compose_path.exists():
                    compose_images = scan_compose_file(
                        repo_root,
                        compose_path,
                        from_workflow=True,
                        workflow_file=rel_path,
                        workflow_line=i,
                    )
                    images.extend(compose_images)

    return images


def scan_compose_file(
    repo_root: Path,
    compose_path: Path,
    from_workflow: bool = False,
    workflow_file: str | None = None,
    workflow_line: int | None = None,
) -> list[DockerImage]:
    """Scan a Docker Compose file for image references."""
    images: list[DockerImage] = []
    content, lines = read_file_content(compose_path)
    rel_path = str(compose_path.relative_to(repo_root))

    # First pass: collect all service definitions with their properties
    services: dict[str, dict[str, Any]] = {}
    current_service: str | None = None
    current_indent = 0
    in_build_section = False

    for i, line in enumerate(lines, 1):
        # Skip empty lines and comments
        if not line.strip() or line.strip().startswith("#"):
            continue

        # Calculate indentation
        indent = len(line) - len(line.lstrip())

        # Detect top-level service definition (under services:)
        if indent == 2 and line.strip().endswith(":") and not line.strip().startswith("-"):
            service_name = line.strip().rstrip(":")
            if service_name and not service_name.startswith("#"):
                current_service = service_name
                services[current_service] = {
                    "image": None,
                    "image_line": None,
                    "build_context": None,
                    "build_dockerfile": None,
                }
                in_build_section = False
                current_indent = indent
                continue

        if current_service and indent > current_indent:
            stripped = line.strip()

            # Detect build section
            if stripped.startswith("build:"):
                in_build_section = True
                # Handle inline build context
                if ":" in stripped and not stripped.endswith(":"):
                    ctx = stripped.split(":", 1)[1].strip()
                    services[current_service]["build_context"] = ctx
                continue

            # Inside build section
            if in_build_section and indent >= current_indent + 4:
                if "context:" in stripped:
                    match = re.search(r"context:\s*(.+)", stripped)
                    if match:
                        services[current_service]["build_context"] = match.group(1).strip()
                elif "dockerfile:" in stripped:
                    match = re.search(r"dockerfile:\s*(.+)", stripped)
                    if match:
                        services[current_service]["build_dockerfile"] = match.group(1).strip()
            else:
                in_build_section = False

            # Detect image reference
            if "image:" in stripped and not stripped.startswith("#"):
                match = re.search(r"image:\s*[\"']?([^\"'\s]+)[\"']?", stripped)
                if match:
                    services[current_service]["image"] = match.group(1)
                    services[current_service]["image_line"] = i

    # Second pass: create DockerImage objects
    for service_name, props in services.items():
        if not props["image"]:
            continue

        ref = props["image"]
        image_name, tag = parse_image_reference(ref)

        # Determine if this is a local build
        is_local = props["build_context"] is not None

        # Construct build context path
        build_ctx = None
        if is_local and props["build_context"]:
            ctx = props["build_context"]
            dockerfile = props["build_dockerfile"] or "Dockerfile"
            build_ctx = f"{ctx}/{dockerfile}"

        source = workflow_file if from_workflow else rel_path
        line_num = workflow_line if from_workflow else props["image_line"]

        images.append(
            DockerImage(
                image=image_name,
                tag=tag,
                source_file=source or rel_path,
                source_line=line_num or 1,
                is_local_build=is_local,
                build_context=build_ctx,
            )
        )

    return images


def scan_dockerfiles(
    repo_root: Path, exclude_dirs: list[str]
) -> list[DockerImage]:
    """Scan Dockerfiles for base images."""
    images: list[DockerImage] = []

    for dockerfile in repo_root.rglob("Dockerfile"):
        rel_path = str(dockerfile.relative_to(repo_root))

        # Check if in excluded directory
        if any(rel_path.startswith(exc) for exc in exclude_dirs):
            continue

        content, lines = read_file_content(dockerfile)

        # Track ARG values for variable substitution
        arg_values: dict[str, str] = {}

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # Parse ARG definitions
            arg_match = re.match(r"ARG\s+(\w+)=(.+)", stripped, re.IGNORECASE)
            if arg_match:
                arg_name = arg_match.group(1)
                arg_value = arg_match.group(2).strip('"').strip("'")
                arg_values[arg_name] = arg_value

            # Parse FROM statements
            from_match = re.match(r"FROM\s+(.+?)(?:\s+AS\s+\w+)?$", stripped, re.IGNORECASE)
            if from_match:
                from_ref = from_match.group(1).strip()

                # Substitute ARG variables
                for arg_name, arg_value in arg_values.items():
                    from_ref = from_ref.replace(f"${{{arg_name}}}", arg_value)
                    from_ref = from_ref.replace(f"${arg_name}", arg_value)

                image_name, tag = parse_image_reference(from_ref)
                images.append(
                    DockerImage(
                        image=image_name,
                        tag=tag,
                        source_file=rel_path,
                        source_line=i,
                    )
                )

    return images


def scan_all_compose_files(
    repo_root: Path, exclude_dirs: list[str]
) -> list[DockerImage]:
    """Scan all Docker Compose files in the repository."""
    images: list[DockerImage] = []

    patterns = ["docker-compose*.yaml", "docker-compose*.yml", "compose.yaml", "compose.yml"]

    for pattern in patterns:
        for compose_file in repo_root.rglob(pattern):
            rel_path = str(compose_file.relative_to(repo_root))

            # Check if in excluded directory
            if any(rel_path.startswith(exc) for exc in exclude_dirs):
                continue

            compose_images = scan_compose_file(repo_root, compose_file)
            images.extend(compose_images)

    return images


def resolve_latest_tag(image: DockerImage) -> dict[str, Any] | None:
    """
    Resolve a latest tag to its actual digest using docker pull.

    Returns None if resolution fails or if not a latest tag.
    """
    if image.tag != "latest" or image.is_local_build:
        return None

    full_ref = image.full_reference()
    print(f"Resolving latest tag for: {full_ref}", file=sys.stderr)

    try:
        # Pull the image
        result = subprocess.run(
            ["docker", "pull", full_ref],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            print(f"Warning: docker pull failed for {full_ref}: {result.stderr}", file=sys.stderr)
            return None

        # Inspect the image to get digest
        inspect_result = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", full_ref],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if inspect_result.returncode == 0:
            digest = inspect_result.stdout.strip()
            return {
                "digest": digest,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }

    except subprocess.TimeoutExpired:
        print(f"Warning: Timeout resolving {full_ref}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Error resolving {full_ref}: {e}", file=sys.stderr)

    return None


def get_commit_sha(repo_root: Path) -> str:
    """Get current git commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return os.environ.get("GITHUB_SHA", "unknown")


def main() -> None:
    """Main entry point."""
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()

    print(f"Scanning repository: {repo_root}", file=sys.stderr)
    print(f"Excluding directories: {args.exclude_dirs}", file=sys.stderr)

    # Step 1: Scan workflow files for images used in Actions
    print("Scanning workflow files...", file=sys.stderr)
    actions_images = scan_workflow_files(repo_root, args.workflow_dir)
    print(f"Found {len(actions_images)} images in workflow files", file=sys.stderr)

    # Step 2: Scan all Dockerfiles (excluding specified dirs)
    print("Scanning Dockerfiles...", file=sys.stderr)
    dockerfile_images = scan_dockerfiles(repo_root, args.exclude_dirs)
    print(f"Found {len(dockerfile_images)} images in Dockerfiles", file=sys.stderr)

    # Step 3: Scan all compose files (excluding specified dirs)
    print("Scanning Docker Compose files...", file=sys.stderr)
    compose_images = scan_all_compose_files(repo_root, args.exclude_dirs)
    print(f"Found {len(compose_images)} images in Compose files", file=sys.stderr)

    # Step 4: Combine all non-workflow images
    all_repo_images = dockerfile_images + compose_images

    # Step 5: Determine which images are NOT in actions
    # Create a set of (image, tag) from actions for comparison
    actions_refs = {(img.image, img.tag) for img in actions_images}
    non_actions_images = [
        img for img in all_repo_images
        if (img.image, img.tag) not in actions_refs
    ]

    # Deduplicate non-actions images
    seen_refs: set[tuple[str, str]] = set()
    unique_non_actions: list[DockerImage] = []
    for img in non_actions_images:
        ref = (img.image, img.tag)
        if ref not in seen_refs:
            seen_refs.add(ref)
            unique_non_actions.append(img)

    # Step 6: Deduplicate actions images
    seen_actions_refs: set[tuple[str, str, bool]] = set()
    unique_actions: list[DockerImage] = []
    for img in actions_images:
        ref = (img.image, img.tag, img.is_local_build)
        if ref not in seen_actions_refs:
            seen_actions_refs.add(ref)
            unique_actions.append(img)
    actions_images = unique_actions

    # Step 7: Resolve latest tags for actions images (only remote images)
    if args.resolve_latest:
        print("Resolving latest tags...", file=sys.stderr)
        for img in actions_images:
            if img.tag == "latest" and not img.is_local_build:
                img.latest_resolved = resolve_latest_tag(img)

    # Step 8: Generate report
    report = {
        "metadata": {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "repo_name": args.repo_name,
            "repo_root": str(repo_root),
            "commit": get_commit_sha(repo_root),
            "run_id": args.run_id,
            "run_number": args.run_number,
            "exclude_dirs": args.exclude_dirs,
        },
        "actions_workflow_images": [img.to_dict() for img in actions_images],
        "non_actions_images": [img.to_dict() for img in unique_non_actions],
    }

    # Write report
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Report written to: {output_path}", file=sys.stderr)
    print(f"  - Actions workflow images: {len(actions_images)}", file=sys.stderr)
    print(f"  - Non-actions images: {len(unique_non_actions)}", file=sys.stderr)


if __name__ == "__main__":
    main()
