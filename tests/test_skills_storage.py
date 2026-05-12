"""Tests for skills.storage — YAML frontmatter, file I/O, runs listing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.skills.schema import Parameter, RequiredTool, Skill
from openclose.skills.storage import (
    _format_frontmatter,
    _parse_bool,
    _parse_frontmatter,
    _parse_int,
    _parse_sections,
    _unquote,
    _yaml_scalar,
    delete_skill,
    list_runs,
    list_skills,
    read_skill,
    reserve_skill_slug,
    skills_dir,
    slugify,
    write_skill,
)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


def _sample_skill(slug: str = "my-skill") -> Skill:
    return Skill(
        name="My Skill",
        slug=slug,
        version=1,
        created_at="2025-01-01T09:00:00+00:00",
        source_session="sess-123",
        parameters=[
            Parameter(name="url", type="string", required=True, default=""),
            Parameter(name="count", type="int", required=False, default="5"),
        ],
        required_tools=[
            RequiredTool(name="webfetch", sensitive=False),
            RequiredTool(name="bash", sensitive=True),
        ],
        goal="Do a thing",
        required_tools_prose="Need webfetch and bash",
        procedure="Step 1: fetch. Step 2: parse.",
        pitfalls="Don't loop forever.",
        verification="Check output non-empty",
    )


# ───────────────────────── slugify ──────────────────────────────

def test_slugify_basic() -> None:
    assert slugify("Hello World") == "hello-world"


def test_slugify_strips_punctuation() -> None:
    assert slugify("Daily PR Digest!") == "daily-pr-digest"


def test_slugify_collapses_whitespace() -> None:
    assert slugify("a   b   c") == "a-b-c"


def test_slugify_empty_fallback() -> None:
    assert slugify("") == "skill"
    assert slugify("!!!") == "skill"


def test_slugify_trims_edge_dashes() -> None:
    assert slugify("--hello--") == "hello"


# ───────────────────────── reserve_skill_slug ───────────────────

def test_reserve_skill_slug_unique(runtime: Path) -> None:
    first = reserve_skill_slug("My Skill")
    assert first == "my-skill"
    # File doesn't exist yet → same slug reused.
    assert reserve_skill_slug("My Skill") == "my-skill"


def test_reserve_skill_slug_collides(runtime: Path) -> None:
    write_skill(_sample_skill("my-skill"))
    # Now the next reservation should append -2.
    assert reserve_skill_slug("My Skill") == "my-skill-2"


def test_reserve_skill_slug_triple_collision(runtime: Path) -> None:
    write_skill(_sample_skill("thing"))
    write_skill(_sample_skill("thing-2"))
    assert reserve_skill_slug("thing") == "thing-3"


# ───────────────────────── _yaml_scalar ─────────────────────────

def test_yaml_scalar_bool() -> None:
    assert _yaml_scalar(True) == "true"
    assert _yaml_scalar(False) == "false"


def test_yaml_scalar_int() -> None:
    assert _yaml_scalar(42) == "42"


def test_yaml_scalar_float() -> None:
    assert _yaml_scalar(3.14) == "3.14"


def test_yaml_scalar_none_is_empty_quoted() -> None:
    assert _yaml_scalar(None) == '""'


def test_yaml_scalar_empty_string_is_quoted() -> None:
    assert _yaml_scalar("") == '""'


def test_yaml_scalar_plain_string_bare() -> None:
    assert _yaml_scalar("hello") == "hello"


def test_yaml_scalar_with_colon_quoted() -> None:
    assert _yaml_scalar("a:b") == '"a:b"'


def test_yaml_scalar_with_hash_quoted() -> None:
    assert _yaml_scalar("a#b") == '"a#b"'


def test_yaml_scalar_quotes_embedded_quote() -> None:
    out = _yaml_scalar('he said "hi"')
    assert out.startswith('"') and out.endswith('"')
    assert '\\"' in out


def test_yaml_scalar_quotes_if_leading_special() -> None:
    assert _yaml_scalar("-dash") == '"-dash"'


def test_yaml_scalar_quotes_if_internal_whitespace_padding() -> None:
    assert _yaml_scalar("  padded  ") == '"  padded  "'


# ───────────────────────── _format_frontmatter ──────────────────

def test_format_frontmatter_minimal() -> None:
    fm = _format_frontmatter(
        name="X", slug="x", version=1,
        created_at="2025-01-01",
        source_session="",
        parameters=[],
        required_tools=[],
    )
    assert fm.startswith("---")
    assert fm.endswith("---")
    assert "parameters: []" in fm
    assert "required_tools: []" in fm


def test_format_frontmatter_with_items() -> None:
    fm = _format_frontmatter(
        name="X", slug="x", version=1,
        created_at="",
        source_session="",
        parameters=[Parameter(name="p1", type="string", required=True, default="d")],
        required_tools=[RequiredTool(name="bash", sensitive=True)],
    )
    assert "- name: p1" in fm
    assert "type: string" in fm
    assert "required: true" in fm
    assert "- name: bash" in fm
    assert "sensitive: true" in fm


# ───────────────────────── _unquote / bools / ints ───────────────

def test_unquote_double() -> None:
    assert _unquote('"hello"') == "hello"


def test_unquote_single() -> None:
    assert _unquote("'hello'") == "hello"


def test_unquote_unquoted() -> None:
    assert _unquote("hello") == "hello"


def test_unquote_escape_sequences() -> None:
    assert _unquote('"she said \\"hi\\""') == 'she said "hi"'
    assert _unquote('"a\\\\b"') == "a\\b"


def test_parse_bool_true_variants() -> None:
    assert _parse_bool("true") is True
    assert _parse_bool("TRUE") is True
    assert _parse_bool("yes") is True
    assert _parse_bool("1") is True


def test_parse_bool_false_default() -> None:
    assert _parse_bool("false") is False
    assert _parse_bool("anything") is False


def test_parse_int_ok() -> None:
    assert _parse_int("42") == 42


def test_parse_int_fallback() -> None:
    assert _parse_int("abc", 99) == 99


# ───────────────────────── _parse_frontmatter ────────────────────

def test_parse_frontmatter_no_delimiters_returns_empty() -> None:
    fm, body = _parse_frontmatter("no frontmatter here")
    assert fm == {}
    assert body == "no frontmatter here"


def test_parse_frontmatter_flat_scalars() -> None:
    text = "---\nname: Hello\nversion: 3\n---\nbody here"
    fm, body = _parse_frontmatter(text)
    assert fm["name"] == "Hello"
    assert fm["version"] == "3"
    assert body == "body here"


def test_parse_frontmatter_empty_list() -> None:
    text = "---\nparameters: []\n---\n"
    fm, _ = _parse_frontmatter(text)
    assert fm["parameters"] == []


def test_parse_frontmatter_block_list() -> None:
    text = (
        "---\n"
        "parameters:\n"
        "  - name: a\n"
        "    type: string\n"
        "  - name: b\n"
        "    type: int\n"
        "---\n"
    )
    fm, _ = _parse_frontmatter(text)
    assert isinstance(fm["parameters"], list)
    assert fm["parameters"][0]["name"] == "a"
    assert fm["parameters"][1]["type"] == "int"


# ───────────────────────── _parse_sections ────────────────────────

def test_parse_sections_extracts_headings() -> None:
    body = "# Goal\nDo X\n\n# Procedure\nStep 1\nStep 2\n"
    sections = _parse_sections(body)
    assert sections["goal"] == "Do X"
    assert "Step 1" in sections["procedure"]


def test_parse_sections_case_folded() -> None:
    body = "# GOAL\nhi\n"
    sections = _parse_sections(body)
    assert "goal" in sections


# ───────────────────────── round-trip write/read ──────────────────

def test_write_and_read_skill_roundtrip(runtime: Path) -> None:
    original = _sample_skill()
    write_skill(original)
    loaded = read_skill("my-skill")
    assert loaded is not None
    assert loaded.name == original.name
    assert loaded.slug == original.slug
    assert len(loaded.parameters) == 2
    assert loaded.parameters[0].name == "url"
    assert loaded.parameters[0].required is True
    assert loaded.parameters[1].type == "int"
    assert len(loaded.required_tools) == 2
    assert loaded.required_tools[1].sensitive is True
    assert "fetch" in loaded.procedure.lower()


def test_write_skill_creates_runs_folder(runtime: Path) -> None:
    write_skill(_sample_skill())
    assert (skills_dir() / "my-skill").is_dir()


def test_read_missing_skill_none(runtime: Path) -> None:
    assert read_skill("nope") is None


def test_list_skills_empty(runtime: Path) -> None:
    assert list_skills() == []


def test_list_skills_ordered(runtime: Path) -> None:
    import os
    import time
    write_skill(_sample_skill("old"))
    old_path = skills_dir() / "old.md"
    past = time.time() - 60
    os.utime(old_path, (past, past))
    write_skill(_sample_skill("new"))
    skills = list_skills()
    slugs = [s.slug for s in skills]
    assert slugs.index("new") < slugs.index("old")


def test_delete_skill_removes_md_and_runs(runtime: Path) -> None:
    write_skill(_sample_skill())
    runs = skills_dir() / "my-skill"
    (runs / "probe.txt").write_text("x", encoding="utf-8")
    assert delete_skill("my-skill") is True
    assert not (skills_dir() / "my-skill.md").exists()
    assert not runs.exists()


def test_delete_missing_skill_returns_false(runtime: Path) -> None:
    assert delete_skill("ghost") is False


# ───────────────────────── parser resilience ──────────────────────

def test_read_skill_with_malformed_parameter_is_skipped(runtime: Path) -> None:
    """A parameter with an invalid type value should be dropped, not crash."""
    d = skills_dir()
    path = d / "broken-params.md"
    path.write_text(
        "---\nname: B\nslug: broken-params\nversion: 1\n"
        "parameters:\n"
        "  - name: bad\n"
        "    type: nonsense\n"  # not in Literal["string","int","bool"]
        "  - name: good\n"
        "    type: string\n"
        "required_tools: []\n"
        "---\n"
        "# Goal\ntest\n",
        encoding="utf-8",
    )
    loaded = read_skill("broken-params")
    assert loaded is not None
    params = [p.name for p in loaded.parameters]
    assert "bad" not in params
    assert "good" in params


# ───────────────────────── list_runs ──────────────────────────────

def test_list_runs_no_dir_returns_empty(runtime: Path) -> None:
    assert list_runs("ghost") == []


def test_list_runs_parses_metadata(runtime: Path) -> None:
    write_skill(_sample_skill())
    run_dir = skills_dir() / "my-skill"
    jsonl = run_dir / "2025-01-01T09-00-00-ABCDEFGHIJKLMNOPQRSTUVWXYZ.jsonl"
    jsonl.write_text(
        json.dumps({"type": "run_start", "timestamp": "2025-01-01T09:00:00"}) + "\n" +
        json.dumps({
            "type": "run_end",
            "timestamp": "2025-01-01T09:05:00",
            "status": "done",
        }) + "\n",
        encoding="utf-8",
    )
    out = run_dir / "2025-01-01T09-00-00-ABCDEFGHIJKLMNOPQRSTUVWXYZ.out.md"
    out.write_text("Summary line\nMore detail\n", encoding="utf-8")

    runs = list_runs("my-skill")
    assert len(runs) == 1
    r = runs[0]
    assert r["status"] == "done"
    assert r["started_at"] == "2025-01-01T09:00:00"
    assert r["finished_at"] == "2025-01-01T09:05:00"
    assert r["output_preview"] == "Summary line"
    assert r["run_id"].isalnum()


def test_list_runs_handles_missing_out_md(runtime: Path) -> None:
    write_skill(_sample_skill())
    run_dir = skills_dir() / "my-skill"
    jsonl = run_dir / "2025-run.jsonl"
    jsonl.write_text(
        json.dumps({"type": "run_start", "timestamp": "t"}) + "\n",
        encoding="utf-8",
    )
    runs = list_runs("my-skill")
    assert len(runs) == 1
    assert runs[0]["output_preview"] == ""


def test_list_runs_limit_honored(runtime: Path) -> None:
    write_skill(_sample_skill())
    run_dir = skills_dir() / "my-skill"
    for i in range(5):
        (run_dir / f"r{i}.jsonl").write_text(
            json.dumps({"type": "run_start", "timestamp": f"t{i}"}) + "\n",
            encoding="utf-8",
        )
    assert len(list_runs("my-skill", limit=2)) == 2


def test_list_runs_tolerates_bad_jsonl(runtime: Path) -> None:
    write_skill(_sample_skill())
    run_dir = skills_dir() / "my-skill"
    (run_dir / "bad.jsonl").write_text("not json\nstill not\n", encoding="utf-8")
    runs = list_runs("my-skill")
    # Should list the run with running status (no valid run_end found).
    assert len(runs) == 1
    assert runs[0]["status"] == "running"


def test_list_runs_handles_empty_file(runtime: Path) -> None:
    write_skill(_sample_skill())
    run_dir = skills_dir() / "my-skill"
    (run_dir / "empty.jsonl").write_text("", encoding="utf-8")
    runs = list_runs("my-skill")
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
