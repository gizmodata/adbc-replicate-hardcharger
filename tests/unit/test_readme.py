"""Every YAML example in the README must be a valid config (or a valid fragment of one)."""

import re
from pathlib import Path

import pytest
import yaml

from adbc_replicate_hardcharger.config import parse_config

README = Path(__file__).resolve().parents[2] / "README.md"
BLOCKS = re.findall(r"```yaml\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.DOTALL)
BASE = {
    "version": 1,
    "source": {"type": "oracle", "uri": "oracle://h:1521/S"},
    "target": {"type": "hardcharger", "uri": "hardcharger://h"},
    "objects": [{"db_schemas": ["HR"]}],
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("PGPASSWORD", "PGUSER", "ORACLE_USERNAME",
                 "ORACLE_PASSWORD", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PRIVATE_KEY", "MASK_SALT", "SLACK_WEBHOOK_URL",
                 "OPS_WEBHOOK_URL", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "HARDCHARGER_USERNAME", "STAGING_USERNAME", "STAGING_PASSWORD",
                 "HARDCHARGER_PASSWORD"):
        monkeypatch.setenv(name, "x")


def test_readme_has_yaml_examples():
    assert len(BLOCKS) >= 8


@pytest.mark.parametrize("block", BLOCKS, ids=[f"block{i}" for i in range(len(BLOCKS))])
def test_readme_yaml_block_is_valid(block):
    data = yaml.safe_load(block)
    if block.startswith("# helm"):
        data = data["config"]                 # the replication config inside Helm values
    elif "version" not in data:
        data = {**BASE, **data}               # a fragment: check it inside a minimal config
    parse_config(data=data)
