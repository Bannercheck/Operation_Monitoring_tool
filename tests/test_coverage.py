"""Parser coverage must not regress: every corpus file keeps at least its baseline shares (samples/corpus/baseline.json)."""
from watchover import coverage


def test_corpus_coverage_does_not_regress():
    res = coverage.run()
    assert res["n"] >= 38
    base = coverage.load_baseline()
    assert base, "baseline missing: python -m watchover.coverage --update-baseline"
    reg = coverage.regressions(res, base)
    assert not reg, "\n".join(reg)
    csv = next(r for r in res["files"] if r["file"] == "csv_events.csv")
    assert csv["score"] == 100.0 and csv["events"] == 4                      # a fully structured file scores full marks
    assert 0 <= res["overall"]["score"] <= 100 and set(res["overall"]) == {"timestamp", "level", "host", "service", "score"}


def test_measure_file_level_rule():
    r = coverage.measure_file("x.log", "2026-09-22 09:15:03 ERROR payment failed\n2026-09-22 09:15:04 no level here\n")
    assert r["events"] == 2 and r["timestamp"] == 100.0 and r["level"] == 50.0      # the line without a level word has a guessed level: not counted as known
    r = coverage.measure_file("t.log", "2026-09-22 09:15:03 ERROR payment-api failed\n  at com.example.Charge.run(Charge.java:88)\n2026-09-22 09:15:04 INFO auth-api ok\n")
    assert r["events"] == 2 and r["level"] == 100.0 and r["service"] == 100.0        # the stack frame folds into the error; hyphenated app names become the service
