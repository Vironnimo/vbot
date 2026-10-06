from core.projects.scan_report import FindingType, ScanFinding, ScanReport


def test_report_filters_and_extends_findings_without_changing_the_original():
    finding = ScanFinding(FindingType.INVALID_SOURCE, "Unreadable metadata.", "reviewer")
    original = ScanReport()
    report = original.with_findings([finding])
    assert original.is_clean
    assert not report.is_clean
    assert report.findings_of(FindingType.INVALID_SOURCE) == (finding,)
