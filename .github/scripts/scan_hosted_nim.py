#!/usr/bin/env python3
"""
Scan hosted NIM endpoints in the repository and match with NVCF function IDs.

This script:
1. Clones the nvcf-api-gateway-config-prd repository
2. Parses the YAML config to build a model -> function_id mapping
3. Scans the current repository for hosted NIM references
4. Matches and generates a JSON report
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Scan hosted NIM endpoints and match with NVCF function IDs"
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
        default="hosted-nim-report.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--nvcf-config-repo",
        type=str,
        default="https://github.com/bp-cicd-org/nvcf-api-gateway-config-prd.git",
        help="NVCF API Gateway config repository URL",
    )
    parser.add_argument(
        "--nvcf-config-file",
        type=str,
        default="nvcf-api-gateway-prd.yaml",
        help="NVCF config YAML file name",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=os.environ.get("NVCF_CONFIG_TOKEN", ""),
        help="GitHub token for cloning NVCF config repo",
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


class HostedNIMReference:
    """Represents a hosted NIM reference found in the repository."""

    def __init__(
        self,
        model: str,
        base_url: str,
        source_file: str,
        source_line: int,
        context: str = "",
    ):
        self.model = model
        self.base_url = base_url
        self.source_file = source_file
        self.source_line = source_line
        self.context = context
        self.function_id: str | None = None
        self.matched: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "model": self.model,
            "base_url": self.base_url,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "function_id": self.function_id,
            "matched": self.matched,
        }
        if self.context:
            result["context"] = self.context
        return result


def clone_nvcf_config_repo(repo_url: str, token: str, target_dir: Path) -> bool:
    """Clone the NVCF config repository."""
    # Inject token into URL if provided
    if token:
        # Handle https://github.com/... format
        if repo_url.startswith("https://"):
            repo_url = repo_url.replace("https://", f"https://{token}@")

    print(f"Cloning NVCF config repository...", file=sys.stderr)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(target_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"Error cloning repository: {result.stderr}", file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("Timeout cloning repository", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error cloning repository: {e}", file=sys.stderr)
        return False


def parse_nvcf_config(config_path: Path) -> dict[str, dict[str, Any]]:
    """
    Parse the NVCF API Gateway config YAML and build a lookup table.

    Returns a dict mapping various keys to function info:
    - model name -> {functionid, modelname, ...}
    - endpoint path -> {functionid, path, ...}
    """
    lookup: dict[str, dict[str, Any]] = {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"Error parsing NVCF config: {e}", file=sys.stderr)
        return lookup

    if not config or "v2config" not in config:
        print("Invalid NVCF config format", file=sys.stderr)
        return lookup

    v2config = config["v2config"]

    # Parse openai section (chat completions, embeddings)
    if "openai" in v2config:
        openai_config = v2config["openai"]

        # chatCompletions section
        if "chatCompletions" in openai_config:
            for key, entry in openai_config["chatCompletions"].items():
                if isinstance(entry, dict) and "functionid" in entry:
                    model_name = entry.get("modelname", key)
                    function_id = entry["functionid"]

                    # Store by various keys for matching
                    lookup[key] = {
                        "functionid": function_id,
                        "modelname": model_name,
                        "type": "chatCompletions",
                    }
                    lookup[model_name] = lookup[key]

                    # Also store without stg/nvdev prefix for broader matching
                    for prefix in ["stg/", "nvdev/", "private/"]:
                        if model_name.startswith(prefix):
                            clean_name = model_name[len(prefix):]
                            if clean_name not in lookup:
                                lookup[clean_name] = lookup[key]

        # embeddings section
        if "embeddings" in openai_config:
            for key, entry in openai_config["embeddings"].items():
                if isinstance(entry, dict) and "functionid" in entry:
                    model_name = entry.get("modelname", key)
                    function_id = entry["functionid"]

                    lookup[key] = {
                        "functionid": function_id,
                        "modelname": model_name,
                        "type": "embeddings",
                    }
                    lookup[model_name] = lookup[key]

                    for prefix in ["stg/", "nvdev/", "private/"]:
                        if model_name.startswith(prefix):
                            clean_name = model_name[len(prefix):]
                            if clean_name not in lookup:
                                lookup[clean_name] = lookup[key]

    # Parse vanity section (custom endpoints like reranking)
    # Vanity has nested structure: vanity.ai_api.paths, vanity.health_api.paths, etc.
    if "vanity" in v2config:
        vanity_config = v2config["vanity"]

        for api_name, api_config in vanity_config.items():
            if isinstance(api_config, dict) and "paths" in api_config:
                for path, entry in api_config["paths"].items():
                    if isinstance(entry, dict) and "functionid" in entry:
                        function_id = entry["functionid"]

                        lookup[path] = {
                            "functionid": function_id,
                            "path": entry.get("path", path),
                            "type": "vanity",
                        }

                        # Extract model info from path for matching
                        # e.g., /v1/retrieval/nvidia/llama-3_2-nv-rerankqa-1b-v2/reranking
                        path_parts = path.strip("/").split("/")
                        if len(path_parts) >= 3:
                            # Try to construct model name from path
                            model_candidate = "/".join(path_parts[-3:-1])
                            if model_candidate not in lookup:
                                lookup[model_candidate] = lookup[path]

                            # Also add with normalized dots
                            model_with_dots = model_candidate.replace("_", ".")
                            if model_with_dots not in lookup:
                                lookup[model_with_dots] = lookup[path]

                            # Add just the model name part (last two segments)
                            if len(path_parts) >= 2:
                                simple_model = path_parts[-2]
                                # Construct full model name like nvidia/llama-3.2-nv-rerankqa-1b-v2
                                if "nvidia" in path_parts:
                                    nvidia_idx = path_parts.index("nvidia")
                                    if nvidia_idx < len(path_parts) - 1:
                                        full_model = f"nvidia/{path_parts[nvidia_idx + 1].replace('_', '.')}"
                                        if full_model not in lookup:
                                            lookup[full_model] = lookup[path]

    print(f"Loaded {len(lookup)} entries from NVCF config", file=sys.stderr)
    return lookup


def is_hosted_nim_url(url: str) -> bool:
    """Check if a URL is a hosted NIM endpoint."""
    hosted_patterns = [
        "integrate.api.nvidia.com",
        "ai.api.nvidia.com",
        "api.nvidia.com",
    ]
    return any(pattern in url for pattern in hosted_patterns)


def scan_yaml_files(
    repo_root: Path,
    exclude_dirs: list[str],
    from_workflow: bool = False,
) -> list[HostedNIMReference]:
    """Scan YAML files for hosted NIM references."""
    refs: list[HostedNIMReference] = []

    patterns = ["*.yaml", "*.yml"]

    for pattern in patterns:
        for yaml_file in repo_root.rglob(pattern):
            rel_path = str(yaml_file.relative_to(repo_root))

            # Check exclusions
            if any(rel_path.startswith(exc) for exc in exclude_dirs):
                continue

            # Skip workflow files if not scanning workflows
            is_workflow = ".github/workflows" in rel_path
            if is_workflow != from_workflow:
                continue

            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    lines = content.splitlines()

                # Try to parse as YAML
                try:
                    data = yaml.safe_load(content)
                    if data:
                        refs.extend(
                            extract_hosted_nim_from_data(data, rel_path, yaml_file)
                        )
                except yaml.YAMLError:
                    pass

                # Also do regex-based scanning for URLs
                for i, line in enumerate(lines, 1):
                    # Look for base_url patterns
                    url_match = re.search(
                        r'(?:base_url|url|endpoint)[:\s]+["\']?(https?://[^"\'\s]+)',
                        line,
                        re.IGNORECASE,
                    )
                    if url_match:
                        url = url_match.group(1)
                        if is_hosted_nim_url(url):
                            # Try to find associated model
                            model = extract_model_from_context(lines, i - 1)
                            if model:
                                refs.append(
                                    HostedNIMReference(
                                        model=model,
                                        base_url=url,
                                        source_file=rel_path,
                                        source_line=i,
                                    )
                                )

            except Exception as e:
                print(f"Error scanning {yaml_file}: {e}", file=sys.stderr)

    return refs


def extract_hosted_nim_from_data(
    data: Any, source_file: str, file_path: Path, path: str = ""
) -> list[HostedNIMReference]:
    """Recursively extract hosted NIM references from parsed YAML data."""
    refs: list[HostedNIMReference] = []

    if isinstance(data, dict):
        # Check for model + base_url combination
        model = data.get("model")
        base_url = data.get("base_url")

        if model and base_url and is_hosted_nim_url(str(base_url)):
            refs.append(
                HostedNIMReference(
                    model=str(model),
                    base_url=str(base_url),
                    source_file=source_file,
                    source_line=1,  # Line number not available from parsed data
                    context=path,
                )
            )

        # Recurse into nested structures
        for key, value in data.items():
            refs.extend(
                extract_hosted_nim_from_data(
                    value, source_file, file_path, f"{path}.{key}" if path else key
                )
            )

    elif isinstance(data, list):
        for i, item in enumerate(data):
            refs.extend(
                extract_hosted_nim_from_data(item, source_file, file_path, f"{path}[{i}]")
            )

    return refs


def extract_model_from_context(lines: list[str], current_idx: int) -> str | None:
    """Try to extract model name from surrounding context."""
    # Look for model definition in nearby lines
    start = max(0, current_idx - 10)
    end = min(len(lines), current_idx + 10)

    for i in range(start, end):
        line = lines[i]
        model_match = re.search(r'model[:\s]+["\']?([^"\'\s,}]+)', line, re.IGNORECASE)
        if model_match:
            return model_match.group(1)

    return None


def scan_python_files(
    repo_root: Path,
    exclude_dirs: list[str],
) -> list[HostedNIMReference]:
    """Scan Python files for hosted NIM references."""
    refs: list[HostedNIMReference] = []

    # Always exclude the scripts directory to avoid self-scanning
    always_exclude = [".github/scripts"]

    for py_file in repo_root.rglob("*.py"):
        rel_path = str(py_file.relative_to(repo_root))

        # Check exclusions
        if any(rel_path.startswith(exc) for exc in exclude_dirs + always_exclude):
            continue

        try:
            with open(py_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for i, line in enumerate(lines, 1):
                # Look for hosted NIM URLs
                if is_hosted_nim_url(line):
                    url_match = re.search(r'(https?://[^\s"\',]+)', line)
                    if url_match:
                        url = url_match.group(1).rstrip("'\")")
                        model = extract_model_from_context(lines, i - 1)
                        if model:
                            refs.append(
                                HostedNIMReference(
                                    model=model,
                                    base_url=url,
                                    source_file=rel_path,
                                    source_line=i,
                                )
                            )

        except Exception as e:
            print(f"Error scanning {py_file}: {e}", file=sys.stderr)

    return refs


def match_function_ids(
    refs: list[HostedNIMReference],
    lookup: dict[str, dict[str, Any]],
) -> None:
    """Match hosted NIM references with function IDs from lookup table."""
    for ref in refs:
        model = ref.model

        # Try exact match
        if model in lookup:
            ref.function_id = lookup[model]["functionid"]
            ref.matched = True
            continue

        # Try without leading slash
        if model.startswith("/"):
            clean_model = model[1:]
            if clean_model in lookup:
                ref.function_id = lookup[clean_model]["functionid"]
                ref.matched = True
                continue

        # Try normalized name (replace _ with . and vice versa)
        normalized = model.replace("_", ".")
        if normalized in lookup:
            ref.function_id = lookup[normalized]["functionid"]
            ref.matched = True
            continue

        normalized = model.replace(".", "_")
        if normalized in lookup:
            ref.function_id = lookup[normalized]["functionid"]
            ref.matched = True
            continue

        # Try extracting path from URL and matching
        if "/v1/" in ref.base_url:
            # e.g., https://ai.api.nvidia.com/v1/retrieval/nvidia/llama-3_2-nv-rerankqa-1b-v2/reranking
            path = ref.base_url.split("nvidia.com")[1] if "nvidia.com" in ref.base_url else ""
            if path in lookup:
                ref.function_id = lookup[path]["functionid"]
                ref.matched = True
                continue

            # Also try with normalized path (. to _)
            normalized_path = path.replace(".", "_")
            if normalized_path in lookup:
                ref.function_id = lookup[normalized_path]["functionid"]
                ref.matched = True
                continue

        # Try matching model name with normalized versions in lookup
        for key, entry in lookup.items():
            entry_model = entry.get("modelname", key)
            # Normalize both for comparison
            if normalize_model_name(model) == normalize_model_name(entry_model):
                ref.function_id = entry["functionid"]
                ref.matched = True
                break


def normalize_model_name(name: str) -> str:
    """Normalize model name for matching."""
    # Remove prefixes
    for prefix in ["stg/", "nvdev/", "private/", "nvidia/"]:
        if name.startswith(prefix):
            name = name[len(prefix):]

    # Replace . with _ and vice versa for consistent matching
    name = name.replace(".", "_").replace("-", "_").lower()
    return name


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


def deduplicate_refs(refs: list[HostedNIMReference]) -> list[HostedNIMReference]:
    """Remove duplicate references."""
    seen: set[tuple[str, str]] = set()
    unique: list[HostedNIMReference] = []

    for ref in refs:
        key = (ref.model, ref.base_url)
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    return unique


def main() -> None:
    """Main entry point."""
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()

    print(f"Scanning repository: {repo_root}", file=sys.stderr)
    print(f"Excluding directories: {args.exclude_dirs}", file=sys.stderr)

    # Step 1: Clone NVCF config repository
    with tempfile.TemporaryDirectory() as temp_dir:
        nvcf_dir = Path(temp_dir) / "nvcf-config"

        if not clone_nvcf_config_repo(args.nvcf_config_repo, args.github_token, nvcf_dir):
            print("Failed to clone NVCF config repository", file=sys.stderr)
            sys.exit(1)

        config_path = nvcf_dir / args.nvcf_config_file
        if not config_path.exists():
            print(f"NVCF config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)

        # Step 2: Parse NVCF config
        print("Parsing NVCF config...", file=sys.stderr)
        lookup = parse_nvcf_config(config_path)

        if not lookup:
            print("Warning: No entries found in NVCF config", file=sys.stderr)

    # Step 3: Scan for hosted NIM references in workflow files
    print("Scanning workflow files...", file=sys.stderr)
    workflow_refs = scan_yaml_files(repo_root, args.exclude_dirs, from_workflow=True)
    print(f"Found {len(workflow_refs)} references in workflow files", file=sys.stderr)

    # Step 4: Scan for hosted NIM references in other files
    print("Scanning repository files...", file=sys.stderr)
    repo_yaml_refs = scan_yaml_files(repo_root, args.exclude_dirs, from_workflow=False)
    repo_py_refs = scan_python_files(repo_root, args.exclude_dirs)
    repo_refs = repo_yaml_refs + repo_py_refs
    print(f"Found {len(repo_refs)} references in repository files", file=sys.stderr)

    # Step 5: Deduplicate
    workflow_refs = deduplicate_refs(workflow_refs)
    repo_refs = deduplicate_refs(repo_refs)

    # Remove workflow refs from repo refs
    workflow_models = {(r.model, r.base_url) for r in workflow_refs}
    repo_refs = [r for r in repo_refs if (r.model, r.base_url) not in workflow_models]

    # Step 6: Match function IDs
    print("Matching function IDs...", file=sys.stderr)
    match_function_ids(workflow_refs, lookup)
    match_function_ids(repo_refs, lookup)

    # Step 7: Generate report
    report = {
        "metadata": {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "repo_name": args.repo_name,
            "repo_root": str(repo_root),
            "commit": get_commit_sha(repo_root),
            "run_id": args.run_id,
            "run_number": args.run_number,
            "nvcf_config_repo": args.nvcf_config_repo,
            "exclude_dirs": args.exclude_dirs,
        },
        "actions_hosted_nim": [ref.to_dict() for ref in workflow_refs],
        "non_actions_hosted_nim": [ref.to_dict() for ref in repo_refs],
        "summary": {
            "total_actions_refs": len(workflow_refs),
            "matched_actions_refs": sum(1 for r in workflow_refs if r.matched),
            "total_repo_refs": len(repo_refs),
            "matched_repo_refs": sum(1 for r in repo_refs if r.matched),
        },
    }

    # Write report
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Report written to: {output_path}", file=sys.stderr)
    print(f"  - Actions hosted NIM: {len(workflow_refs)}", file=sys.stderr)
    print(f"  - Non-actions hosted NIM: {len(repo_refs)}", file=sys.stderr)


if __name__ == "__main__":
    main()
