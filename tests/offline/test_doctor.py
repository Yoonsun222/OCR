from dataclasses import replace

import pytest

from raglab.offline import doctor
from raglab.offline.bundle import BundleReport
from raglab.offline.config import load_profile_config
from raglab.offline.ports import EgressPolicy, SystemSnapshot


class StaticSystem:
    def __init__(self, snapshot):
        self.value = snapshot

    def snapshot(self):
        return self.value


class StaticEgress:
    def __init__(self, status):
        self.status = status

    def inspect(self):
        if isinstance(self.status, Exception):
            raise self.status
        return EgressPolicy(self.status)


def ready_snapshot(profile):
    physical, available = (16, 12) if profile == "local16" else (32, 26)
    return SystemSnapshot(True, (3, 11), physical, available, 80, True, True, True, True)


def context(profile="local16", egress="DENIED"):
    return doctor.DoctorContext(
        StaticSystem(ready_snapshot(profile)), StaticEgress(egress),
        BundleReport("PASS", "APR-TEST", (), "a" * 64), load_profile_config(profile),
        doctor.VerifiedBundleEvidence("a" * 64, 0, True, 0),
    )


@pytest.mark.parametrize("profile", ["local16", "local32"])
def test_ready_profiles_pass_in_check_order(profile):
    report = doctor.run_doctor(context(profile))
    assert report.overall_status == "PASS"
    assert report.profile == profile
    assert report.generation_fingerprint == "a" * 64
    assert [check.check_id for check in report.checks] == [
        "BUNDLE_APPROVAL", "OS_PYTHON", "RESOURCE_PROFILE", "FREE_DISK",
        "WRITABLE_PATHS", "LONG_UNICODE_PATH", "CATALOG_SIGNER", "DLL_INVENTORY",
        "OFFLINE_EGRESS_POLICY", "MODEL_INVENTORY", "COORDINATE_ROUND_TRIP",
        "ATOMIC_WRITE_CLEANUP", "DERIVATIVE_DELETION",
    ]


@pytest.mark.parametrize("egress", ["UNKNOWN", "ALLOWED", OSError("private detail")])
def test_unproven_egress_blocks_without_echo(egress):
    report = doctor.run_doctor(context(egress=egress))
    assert report.overall_status == "BLOCK"
    assert next(item for item in report.checks if item.check_id == "OFFLINE_EGRESS_POLICY").status == "BLOCK"
    assert "private detail" not in repr(report)


@pytest.mark.parametrize("field,value,check_id", [
    ("available_ram_gib", 11, "RESOURCE_PROFILE"),
    ("free_disk_gib", 9, "FREE_DISK"),
    ("paths_writable", False, "WRITABLE_PATHS"),
    ("long_unicode_path", False, "LONG_UNICODE_PATH"),
    ("atomic_write_cleanup", False, "ATOMIC_WRITE_CLEANUP"),
    ("derivative_deleted", False, "DERIVATIVE_DELETION"),
])
def test_resource_and_smoke_check_failures_block(field, value, check_id):
    baseline = context()
    system = StaticSystem(replace(baseline.system.snapshot(), **{field: value}))
    report = doctor.run_doctor(replace(baseline, system=system))
    assert report.overall_status == "BLOCK"
    assert next(item for item in report.checks if item.check_id == check_id).status == "BLOCK"


def test_canary_requires_verified_metadata_and_propagates_failure():
    baseline = context()
    calls = []

    class Canary:
        def verify_denied(self, endpoint, approval_id):
            calls.append((endpoint, approval_id))
            return False

    request = doctor.CanaryRequest(True, "APR-CANARY", Canary())
    metadata = doctor.CanaryAuthorization("https://sink.example.test/canary", "APR-CANARY")
    signed = replace(baseline.evidence, canary=metadata)
    assert doctor.run_doctor(replace(baseline, canary=request)).overall_status == "BLOCK"
    assert doctor.run_doctor(replace(baseline, evidence=signed)).overall_status == "PASS"
    assert calls == []
    report = doctor.run_doctor(replace(baseline, evidence=signed, canary=request))
    assert report.overall_status == "BLOCK"
    assert calls == [(metadata.endpoint, metadata.approval_id)]
    assert metadata.endpoint not in repr(report)
