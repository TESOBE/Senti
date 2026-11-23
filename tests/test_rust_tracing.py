"""The rust-tracing kind: the tracing crate's JSON lines, read by their fields."""

import pytest

from senti.kinds import KINDS, kind_of
from senti.signatures import parse_line, signature_of

RUST = kind_of("rust-tracing")


def warn(currency: str, ts="2026-11-06T16:31:27.357523Z") -> str:
    return ('{"timestamp":"' + ts + '","level":"WARN","fields":{"message":"USD/fiat leg fell back","currency":"'
            + currency + '","used":"er-api","failed":"pyth: HTTP status client error (401 Unauthorized)"},'
            '"target":"my_service::fx"}')


def test_the_kind_is_known():
    assert "rust-tracing" in KINDS and RUST.toolchain is None


def test_a_line_is_read_by_its_fields():
    parsed = parse_line(warn("KES"), RUST)
    assert (parsed.logger, parsed.thread, parsed.message) == ("my_service::fx", "", "USD/fiat leg fell back")
    assert parsed.ts.isoformat() == "2026-11-06T16:31:27.357523+00:00"
    assert parsed.detail.splitlines() == ["currency=KES", "used=er-api",
                                          "failed=pyth: HTTP status client error (401 Unauthorized)"]


def test_the_error_field_comes_first_in_the_detail():
    raw = ('{"timestamp":"2026-11-06T16:49:10.154360Z","level":"WARN","fields":{"message":"registry refresh failed",'
           '"attempt":3,"error":"upstream returned 401 Unauthorized"},"target":"my_service::routing"}')
    assert parse_line(raw, RUST).detail.splitlines() == ["error=upstream returned 401 Unauthorized", "attempt=3"]


def test_lines_that_differ_only_in_values_are_one_signature():
    a, b = parse_line(warn("KES"), RUST), parse_line(warn("TZS", ts="2026-11-06T17:00:00.000000Z"), RUST)
    assert signature_of("warning", a, RUST).id == signature_of("warning", b, RUST).id
    sig = signature_of("warning", a, RUST)
    assert (sig.template, sig.exception, sig.endpoint) == ("USD/fiat leg fell back", "", "")


@pytest.mark.parametrize("raw", ['{"not": "a log line"}', '{"level":"INFO","fields":{"x":1}}', '{broken', '[1, 2]',
                                 "thread 'main' panicked at src/main.rs:12:5"])
def test_what_is_not_a_tracing_line_is_a_plain_message(raw):
    parsed = parse_line(raw, RUST)
    assert parsed.ts is None and parsed.message == raw
