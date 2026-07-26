"""Central path and configuration resolution.

Every path in IntelligenceStack resolves relative to the repository root so the
project runs unchanged on any machine that clones it. The landing zone location
is read from config/pipeline_config.yaml, mirroring how a Databricks job reads
its storage location from the pipeline specification rather than hardcoding it.
"""

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = REPO_ROOT / "config" / "pipeline_config.yaml"
ARCHITECTURE_DIAGRAM = REPO_ROOT / "architecture_diagram.png"


def load_pipeline_config() -> dict:
    """Load the pipeline specification that drives catalog, schema and storage."""
    with open(CONFIG_PATH) as handle:
        return yaml.safe_load(handle)["pipeline"]


_CONFIG = load_pipeline_config()

CATALOG = _CONFIG["target_catalog"]
SCHEMA = _CONFIG["target_schema"]

# The config declares a DBFS-style absolute path (/mnt/...). Locally we anchor
# that same relative structure under the repository root.
LANDING_ZONE = REPO_ROOT / _CONFIG["storage_landing_zone"].lstrip("/")

# Corpus of governed enterprise documents exposed to retrieval-augmented
# answers. Tracked in git (unlike the generated telemetry) so the demo works on
# a fresh clone.
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"

# The API base the control plane calls. Overridable so the UI can point at a
# remote deployment without a code change.
API_BASE_URL = os.environ.get("INTELLIGENCESTACK_API", "http://localhost:8000")
