from datetime import timezone

from app.textutil import graph_iso, html_to_text, parse_dt, parse_report_args


def test_html_to_text() -> None:
    raw = "<p>Oi <at id=\"0\">Fulano</at>,&nbsp;tudo&amp;certo?</p><p>linha 2<br/>linha 3</p>"
    assert html_to_text(raw) == "Oi Fulano, tudo&certo?\nlinha 2\nlinha 3"


def test_parse_dt_graph_formats() -> None:
    assert parse_dt("2026-10-15T12:00:00.1234567Z").microsecond == 123456
    assert parse_dt("2026-10-15T12:00:00Z").tzinfo == timezone.utc
    assert parse_dt(None) is None


def test_graph_iso() -> None:
    dt = parse_dt("2026-10-15T09:00:00-03:00")
    assert dt is not None and graph_iso(dt) == "2026-10-15T12:00:00Z"


def test_parse_report_args() -> None:
    assert parse_report_args("3 7d") == (3, 7)
    assert parse_report_args("7d") == (None, 7)
    assert parse_report_args("") == (None, 1)


def test_split_helpers() -> None:
    from app.textutil import parse_ids, split_name_number, split_name_text

    assert split_name_text("Rafael Schmidt: cuida do S&OP") == ("Rafael Schmidt", "cuida do S&OP")
    assert split_name_text("Ana aprova deploys") == ("Ana", "aprova deploys")
    assert split_name_number("Rafael Schmidt 1") == ("Rafael Schmidt", 1)
    assert split_name_number("Rafael") == ("Rafael", None)
    assert parse_ids("12, #13 14") == [12, 13, 14]
    assert parse_ids("todas") is None
    assert parse_ids("abc") == []
