from backend.routers import desk_reports as dr


def test_redact_removes_identity_and_secrets():
    text = "/Users/alice/secret a@b.com ghp_abcdefghijklmnop Bearer abc123 10.0.0.1"
    out = dr._redact(text)
    assert "alice" not in out
    assert "a@b.com" not in out
    assert "ghp_" not in out
    assert "abc123" not in out
    assert "10.0.0.1" not in out


def test_clean_caps_length():
    assert len(dr._clean("x" * 5000)) == dr.MAX_FIELD


def test_short_caps_to_40():
    assert len(dr._short("y" * 100)) == 40


def test_append_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "data_root", lambda: tmp_path)
    dr._append({"id": "abc123", "problem": "p", "solution": "s", "kind": "visual"})
    records = dr._load()
    assert records[0]["id"] == "abc123"
    assert records[0]["kind"] == "visual"
    assert (tmp_path / "desk_reports.jsonl").stat().st_mode & 0o077 == 0


def test_report_body_accepts_kind():
    body = dr.ReportBody(problem="x" * 10, solution="y" * 10, kind="missing-flow", channel="beta")
    assert body.kind == "missing-flow"
    assert body.channel == "beta"
    assert "visual" in dr.KINDS
