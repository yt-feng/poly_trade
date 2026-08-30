from __future__ import annotations

import copy
import hashlib
import json
import os
import grp
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bounded_horizon_canary_v275 as horizon
import one_window_canary_v274 as frozen_v274
import test_near_settlement_reversal_v271 as v271_fixture
import test_one_window_canary_v274 as v274_fixture


START = 1_900_000_000 - (1_900_000_000 % 300)
SIGNER = "0x" + "1" * 40
FUNDER = "0x" + "2" * 40
PUBLIC_IDENTITY = horizon.build_public_execution_identity(
    expected_signer=SIGNER,
    expected_funder=FUNDER,
    signature_type=0,
)
PUBLIC_IDENTITY_SHA = hashlib.sha256(
    horizon.canonical_bytes(PUBLIC_IDENTITY, newline=True)
).hexdigest()


def complete_v274_live_row(fixture) -> dict:
    """Build one sealed v271 row that passes the real factor/tail/quote verifier."""

    target = v274_fixture.TARGET
    tail_frozen = target + 240.02
    with (
        patch.object(v271_fixture, "START", target),
        patch.object(v271_fixture, "TAIL_FROZEN_AT", tail_frozen),
        patch.object(v271_fixture, "FACTOR_READY_AT", target + 240.03),
        patch.object(v271_fixture, "IDENTITY_FROZEN_AT", target + 240.04),
        patch.object(v271_fixture, "MARKET", v274_fixture.MARKET),
        patch.object(v271_fixture, "TOKENS", dict(v274_fixture.TOKENS)),
    ):
        panel = v271_fixture.make_panel(selected_ask=0.20)
    # binance_point's default observed_at is captured when the helper module is
    # imported, so relocate those two point clocks explicitly and re-seal them.
    for point in (panel["factor"]["baseline"], panel["factor"]["end"]):
        point["consumer_observed_at"] = tail_frozen
        point["normalized_sha256"] = (
            frozen_v274.producer.normalized_evidence_sha256(point)
        )
    panel["frozen_alpha_sha256"] = frozen_v274.producer.frozen_alpha_sha256(
        window_start=target,
        feature=panel["factor"],
        signal_identity=panel["signal_identity"],
        frozen_endpoint_identity=panel["frozen_endpoint_identity"],
    )
    definition = frozen_v274.producer.experiment_definition(
        fixture.live_definition["forward_cutoff"]
    )
    unsealed = {
        key: value for key, value in panel.items() if key != "record_sha256"
    }
    return frozen_v274.producer.seal_record(
        {
            **frozen_v274.producer.base_panel(
                target,
                definition,
                target + 242.0,
            ),
            **unsealed,
            "experiment_id": definition["experiment_id"],
            "forward_cutoff": definition["forward_cutoff"],
            "window_start": target,
            "slug": frozen_v274.producer.expected_slug(target),
            "event": "near_settlement_reversal_panel",
            "panel_valid": True,
            "support_reason": "supported",
            "record_ordinal": 1,
            "previous_record_sha256": None,
        }
    )


class BoundedHorizonContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schedule = horizon.build_schedule(START)
        self.authorization = horizon.build_authorization(
            schedule=self.schedule,
            authorized_at_epoch_millis=START * 1000 - 300_000,
            expected_signer=SIGNER,
            expected_funder=FUNDER,
            signature_type=0,
            public_execution_identity_artifact_sha256=PUBLIC_IDENTITY_SHA,
            qualification_manifest_sha256="a" * 64,
            v275_release_sha256="b" * 64,
        )
        self.authorization_sha = horizon.canonical_sha256(self.authorization)
        self.manifest = horizon.build_manifest(
            self.authorization,
            self.authorization_sha,
            self.schedule,
            created_at_epoch_millis=START * 1000 - 240_000,
        )
        self.manifest_sha = horizon.canonical_sha256(self.manifest)
        self.horizon_receipt = horizon.build_horizon_receipt(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=horizon.canonical_sha256(self.schedule),
            manifest_sha256=self.manifest_sha,
            release_contract_sha256=self.authorization["release_contract_sha256"],
            public_execution_identity_artifact_sha256=PUBLIC_IDENTITY_SHA,
            shared_execution_lock_device=11,
            shared_execution_lock_inode=22,
            shared_execution_lock_group_gid=1000,
            effective_systemd_contract_sha256="c" * 64,
            registered_at_epoch_millis=START * 1000 - 180_000,
        )
        self.horizon_receipt_sha = horizon.canonical_sha256(self.horizon_receipt)

    def test_schedule_is_exact_144_contiguous_windows_without_extension(self) -> None:
        self.assertEqual(len(self.schedule["windows"]), 144)
        self.assertEqual(self.schedule["horizon_end_epoch"] - START, 43_200)
        for index, item in enumerate(self.schedule["windows"]):
            self.assertEqual(item["window_index"], index)
            self.assertEqual(item["exact_window_start_epoch"], START + index * 300)
            self.assertEqual(item["exact_window_end_epoch"], START + (index + 1) * 300)
        self.assertFalse(self.schedule["window_145_permitted"])

    def test_manifest_seals_144_chained_child_commitments(self) -> None:
        horizon.validate_manifest(
            self.manifest,
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
        )
        self.assertEqual(len(self.manifest["children"]), 144)
        previous = None
        for entry in self.manifest["children"]:
            self.assertEqual(entry["previous_child_commitment_sha256"], previous)
            previous = entry["child_commitment_sha256"]
        self.assertEqual(previous, self.manifest["final_child_commitment_sha256"])

    def test_hash_dag_has_no_manifest_child_cycle(self) -> None:
        child = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=0,
        )
        self.assertEqual(
            child["child_commitment_sha256"],
            horizon.canonical_sha256(child["child_commitment_core"]),
        )
        self.assertNotEqual(horizon.canonical_sha256(child), child["child_commitment_sha256"])
        self.assertEqual(child["manifest_sha256"], self.manifest_sha)
        self.assertEqual(child["horizon_receipt_sha256"], self.horizon_receipt_sha)

    def test_child_transplant_changes_raw_child_identity(self) -> None:
        first = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=0,
        )
        second = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=1,
        )
        self.assertNotEqual(horizon.canonical_sha256(first), horizon.canonical_sha256(second))
        self.assertEqual(second["previous_child_commitment_sha256"], first["child_commitment_sha256"])

    def test_persisted_projection_passes_frozen_v274_receipt_validator(self) -> None:
        child = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=0,
        )
        child_sha = horizon.canonical_sha256(child)
        live_definition = frozen_v274.build_live_signal_definition(START)
        fields = horizon._projection_fields(frozen_v274, child, live_definition)
        receipt = horizon._projection_receipt(
            frozen_v274,
            fields,
            child_sha,
            START * 1000 - 120_000,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "v274_projection_receipt.json"
            raw_sha = horizon._write_exclusive_json(path, receipt)
            reread, reread_sha = frozen_v274.read_canonical_object(path)
            self.assertEqual(raw_sha, reread_sha)
            self.assertEqual(reread, receipt)
            frozen_v274.validate_receipt(
                reread,
                authorization_sha256=child_sha,
                authorization_fields=fields,
            )

    def test_derived_raw_child_axis_passes_complete_frozen_v274_verifier(self) -> None:
        fixture = v274_fixture.OneWindowCanaryV274Test(
            methodName="test_candidate_and_permit_dually_bind_and_keep_fixed_five_contract"
        )
        fixture.setUp()
        try:
            anchor, anchor_original_sha = fixture.anchor()
            row = complete_v274_live_row(fixture)
            live_artifact, live_artifact_sha = fixture.live_artifact(row)
            candidate, _candidate_sha, permit, _permit_sha = (
                frozen_v274.materialize_candidate_and_permit(
                    paths=fixture.paths,
                    row=row,
                    live_definition=fixture.live_definition,
                    live_signal_artifact=live_artifact,
                    live_signal_row_sha256=live_artifact_sha,
                    qualification_anchor=anchor,
                    qualification_anchor_sha256=anchor_original_sha,
                    authorization_fields=fixture.fields,
                    authorization_sha256=fixture.authorization_sha256,
                    receipt=fixture.receipt,
                    receipt_sha256=fixture.receipt_sha256,
                    materialized_at_epoch_millis=(
                        v274_fixture.TARGET * 1000 + 242_100
                    ),
                )
            )
            child_sha = "7" * 64
            fields = {
                **fixture.fields,
                "schema": "btc5m-v275-v274-verifier-projection-v1",
                "public_execution_identity_artifact_sha256": (
                    PUBLIC_IDENTITY_SHA
                ),
            }
            receipt = horizon._projection_receipt(
                frozen_v274,
                fields,
                child_sha,
                v274_fixture.TARGET * 1000 - 110_000,
            )
            receipt_sha = hashlib.sha256(
                frozen_v274.canonical_bytes(receipt, newline=True)
            ).hexdigest()
            derived_anchor = {
                **anchor,
                "authorization_sha256": child_sha,
                "conditional_receipt_sha256": receipt_sha,
                "parent_horizon_authorization_sha256": "8" * 64,
                "horizon_manifest_sha256": "9" * 64,
                "public_execution_identity_artifact_sha256": (
                    PUBLIC_IDENTITY_SHA
                ),
            }
            anchor_sha = hashlib.sha256(
                frozen_v274.canonical_bytes(derived_anchor, newline=True)
            ).hexdigest()
            derived_candidate = {
                **candidate,
                "authorization_sha256": child_sha,
                "conditional_receipt_sha256": receipt_sha,
                "qualification_anchor_sha256": anchor_sha,
                "parent_horizon_authorization_sha256": "8" * 64,
                "horizon_manifest_sha256": "9" * 64,
                "public_execution_identity_artifact_sha256": (
                    PUBLIC_IDENTITY_SHA
                ),
            }
            candidate_artifact_sha = hashlib.sha256(
                frozen_v274.canonical_bytes(derived_candidate, newline=True)
            ).hexdigest()
            derived_permit = {
                **permit,
                "authorization_sha256": child_sha,
                "conditional_receipt_sha256": receipt_sha,
                "qualification_anchor_sha256": anchor_sha,
                "candidate_sha256": frozen_v274.canonical_sha256(derived_candidate),
                "candidate_artifact_sha256": candidate_artifact_sha,
                "parent_horizon_authorization_sha256": "8" * 64,
                "horizon_manifest_sha256": "9" * 64,
            }
            frozen_v274.verify_candidate_and_permit(
                authorization_fields=fields,
                authorization_sha256=child_sha,
                receipt=receipt,
                receipt_sha256=receipt_sha,
                qualification_anchor=derived_anchor,
                qualification_anchor_sha256=anchor_sha,
                live_definition=fixture.live_definition,
                live_signal_artifact=live_artifact,
                live_signal_row_sha256=live_artifact_sha,
                candidate=derived_candidate,
                candidate_artifact_sha256=candidate_artifact_sha,
                permit=derived_permit,
            )
        finally:
            fixture.tearDown()

    def test_complete_live_row_factor_or_tail_tamper_is_real_v274_cash(self) -> None:
        fixture = v274_fixture.OneWindowCanaryV274Test(
            methodName=(
                "test_candidate_and_permit_dually_bind_and_keep_fixed_five_contract"
            )
        )
        fixture.setUp()
        try:
            row = complete_v274_live_row(fixture)
            for field in ("factor", "binance_tail"):
                attacked = copy.deepcopy(row)
                if field == "factor":
                    attacked[field]["return_bps"] += 1.0
                else:
                    attacked[field]["valid_quote_suffix_rows"] += 1
                attacked = frozen_v274.producer.seal_record(
                    {
                        key: value
                        for key, value in attacked.items()
                        if key not in {"schema_version", "record_sha256"}
                    }
                )
                with self.subTest(field=field), self.assertRaises(
                    frozen_v274.CashWindow
                ):
                    frozen_v274._recompute_live_execution_fields(
                        row=attacked,
                        live_definition=fixture.live_definition,
                    )
        finally:
            fixture.tearDown()

    def test_manifest_rejects_145th_or_mutated_child(self) -> None:
        mutated = dict(self.manifest)
        mutated["children"] = [*self.manifest["children"], dict(self.manifest["children"][-1])]
        with self.assertRaises(horizon.HorizonRefusal):
            horizon.validate_manifest(
                mutated,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
            )

    def test_parent_rejects_account_and_release_contract_mutation(self) -> None:
        for field, value in (
            ("expected_funder", "0x" + "3" * 40),
            ("expected_signer", "0x" + "4" * 40),
            ("v274_release_sha256", "5" * 64),
        ):
            mutated = copy.deepcopy(self.authorization)
            mutated[field] = value
            with self.subTest(field=field), self.assertRaises(
                horizon.HorizonRefusal
            ):
                horizon.validate_authorization(
                    mutated,
                    schedule=self.schedule,
                    expected_release_sha256="b" * 64,
                )
        nested = copy.deepcopy(self.authorization)
        nested["release_contract"]["execute_unit_sha256"] = "6" * 64
        nested["release_contract_sha256"] = horizon.canonical_sha256(
            nested["release_contract"]
        )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon.validate_authorization(
                nested,
                schedule=self.schedule,
                expected_release_sha256="b" * 64,
            )

    def test_rehashed_account_parent_cannot_transplant_external_anchor_or_child(self) -> None:
        mutated = copy.deepcopy(self.authorization)
        changed_signer = "0x" + "4" * 40
        changed_identity = horizon.build_public_execution_identity(
            expected_signer=changed_signer,
            expected_funder=FUNDER,
            signature_type=0,
        )
        changed_identity_sha = hashlib.sha256(
            horizon.canonical_bytes(changed_identity, newline=True)
        ).hexdigest()
        mutated["expected_signer"] = changed_signer
        mutated["public_execution_identity"] = changed_identity
        mutated[
            "public_execution_identity_artifact_sha256"
        ] = changed_identity_sha
        horizon.validate_authorization(
            mutated,
            schedule=self.schedule,
            expected_release_sha256="b" * 64,
        )
        mutated_sha = horizon.canonical_sha256(mutated)
        with tempfile.TemporaryDirectory() as temporary:
            identity_path = Path(temporary) / "public-identity.json"
            horizon._write_exclusive_json(identity_path, PUBLIC_IDENTITY)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_installed_public_execution_identity(
                    mutated,
                    path=identity_path,
                    require_root_owned=False,
                    require_canonical_path=False,
                )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon.validate_manifest(
                self.manifest,
                authorization=mutated,
                authorization_sha256=mutated_sha,
                schedule=self.schedule,
            )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon.build_child(
                authorization=mutated,
                authorization_sha256=mutated_sha,
                schedule=self.schedule,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                index=0,
            )

    def test_coherent_pre_auth_identity_and_regenerated_chain_still_refuse_provenance(self) -> None:
        changed_signer = "0x" + "8" * 40
        changed_identity = horizon.build_public_execution_identity(
            expected_signer=changed_signer,
            expected_funder=FUNDER,
            signature_type=0,
        )
        changed_identity_sha = hashlib.sha256(
            horizon.canonical_bytes(changed_identity, newline=True)
        ).hexdigest()
        changed_authorization = horizon.build_authorization(
            schedule=self.schedule,
            authorized_at_epoch_millis=START * 1000 - 300_000,
            expected_signer=changed_signer,
            expected_funder=FUNDER,
            signature_type=0,
            public_execution_identity_artifact_sha256=changed_identity_sha,
            qualification_manifest_sha256="a" * 64,
            v275_release_sha256="b" * 64,
        )
        changed_authorization_sha = horizon.canonical_sha256(
            changed_authorization
        )
        changed_manifest = horizon.build_manifest(
            changed_authorization,
            changed_authorization_sha,
            self.schedule,
            created_at_epoch_millis=START * 1000 - 240_000,
        )
        changed_receipt = horizon.build_horizon_receipt(
            authorization_sha256=changed_authorization_sha,
            schedule_sha256=horizon.canonical_sha256(self.schedule),
            manifest_sha256=horizon.canonical_sha256(changed_manifest),
            release_contract_sha256=changed_authorization[
                "release_contract_sha256"
            ],
            public_execution_identity_artifact_sha256=changed_identity_sha,
            shared_execution_lock_device=11,
            shared_execution_lock_inode=22,
            shared_execution_lock_group_gid=1000,
            effective_systemd_contract_sha256="c" * 64,
            registered_at_epoch_millis=START * 1000 - 180_000,
        )
        self.assertEqual(
            changed_receipt["public_execution_identity_artifact_sha256"],
            changed_identity_sha,
        )
        exact_v274_provenance = {
            "schema": horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            identity_directory = Path(temporary) / "identity"
            identity_directory.mkdir(mode=0o700)
            identity_path = identity_directory / "public-execution-identity.json"
            horizon._write_exclusive_json(identity_path, changed_identity)
            with patch.object(
                horizon,
                "CANONICAL_PUBLIC_IDENTITY_DIRECTORY",
                identity_directory,
            ), patch.object(
                horizon,
                "CANONICAL_PUBLIC_IDENTITY",
                identity_path,
            ), patch.object(
                horizon,
                "_read_v274_identity_provenance",
                return_value=(
                    exact_v274_provenance,
                    horizon.V274_PROVENANCE_AUTHORIZATION_SHA256,
                ),
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon._read_public_execution_identity(
                    identity_path,
                    require_root_owned=False,
                    require_canonical_path=True,
                )

    def test_account_identity_propagates_parent_child_candidate_and_permit(self) -> None:
        child = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=0,
        )
        execution_identity = {
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
            "public_execution_identity_artifact_sha256": PUBLIC_IDENTITY_SHA,
        }
        candidate = dict(execution_identity)
        permit = dict(execution_identity)
        horizon._validate_account_identity_chain(
            authorization=self.authorization,
            child=child,
            candidate=candidate,
            permit=permit,
        )
        self.assertEqual(
            child["child_commitment_core"][
                "public_execution_identity_artifact_sha256"
            ],
            PUBLIC_IDENTITY_SHA,
        )
        for target, field, value in (
            (candidate, "expected_signer", "0x" + "5" * 40),
            (permit, "public_execution_identity_artifact_sha256", "6" * 64),
        ):
            attacked_candidate = dict(candidate)
            attacked_permit = dict(permit)
            attacked = (
                attacked_candidate if target is candidate else attacked_permit
            )
            attacked[field] = value
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_account_identity_chain(
                    authorization=self.authorization,
                    child=child,
                    candidate=attacked_candidate,
                    permit=attacked_permit,
                )

    def test_public_identity_provisioner_is_credentialless_exact_and_immutable(self) -> None:
        source = {
            "schema": horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
            "sealed_extra": "the exact raw source SHA binds all other fields",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "v274-auth.json"
            identity_directory = root / "v275-public-identity"
            identity_directory.mkdir(mode=0o700)
            output_path = identity_directory / "public-execution-identity.json"
            source_sha = horizon._write_exclusive_json(source_path, source)
            with patch.object(
                horizon,
                "V274_PROVENANCE_AUTHORIZATION_SHA256",
                source_sha,
            ):
                identity, identity_sha = (
                    horizon.provision_public_execution_identity_from_v274(
                        source_path=source_path,
                        output_path=output_path,
                        require_root=False,
                    )
                )
                self.assertEqual(identity["expected_signer"], SIGNER)
                self.assertEqual(identity["expected_funder"], FUNDER)
                self.assertEqual(identity["signature_type"], 0)
                self.assertEqual(
                    identity_sha,
                    hashlib.sha256(
                        horizon.canonical_bytes(identity, newline=True)
                    ).hexdigest(),
                )
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon.provision_public_execution_identity_from_v274(
                        source_path=source_path,
                        output_path=output_path,
                        require_root=False,
                    )

    def test_public_identity_output_directory_and_existing_inode_attacks_refuse(self) -> None:
        source = {
            "schema": horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "v274-auth.json"
            source_sha = horizon._write_exclusive_json(source_path, source)
            identity_directory = root / "identity"
            identity_directory.mkdir(mode=0o700)
            seed = root / "seed.json"
            horizon._write_exclusive_json(seed, {"seed": True})
            outputs: list[Path] = []
            symlink = identity_directory / "symlink.json"
            symlink.symlink_to(seed)
            outputs.append(symlink)
            hardlink = identity_directory / "hardlink.json"
            os.link(seed, hardlink)
            outputs.append(hardlink)
            wrong_mode = identity_directory / "wrong-mode.json"
            horizon._write_exclusive_json(wrong_mode, {"seed": True})
            wrong_mode.chmod(0o644)
            outputs.append(wrong_mode)
            with patch.object(
                horizon,
                "V274_PROVENANCE_AUTHORIZATION_SHA256",
                source_sha,
            ):
                for output_path in outputs:
                    with self.subTest(output=output_path.name), self.assertRaises(
                        horizon.HorizonRefusal
                    ):
                        horizon.provision_public_execution_identity_from_v274(
                            source_path=source_path,
                            output_path=output_path,
                            require_root=False,
                        )
            identity_directory.chmod(0o770)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_public_identity_directory(
                    identity_directory,
                    require_root_owned=False,
                    require_canonical_path=False,
                )

    def test_public_identity_directory_symlink_gid_and_ancestry_attacks_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_directory = root / "identity"
            identity_directory.mkdir(mode=0o700)
            linked_directory = root / "identity-link"
            linked_directory.symlink_to(identity_directory, target_is_directory=True)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_public_identity_directory(
                    linked_directory,
                    require_root_owned=False,
                    require_canonical_path=False,
                )
            if os.geteuid() == 0:
                os.chown(identity_directory, 0, 65534)
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon._validate_public_identity_directory(
                        identity_directory,
                        require_root_owned=True,
                        require_canonical_path=False,
                    )
                os.chown(identity_directory, 0, 0)
                root.chmod(0o777)
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon._validate_public_identity_directory(
                        identity_directory,
                        require_root_owned=True,
                        require_canonical_path=False,
                    )

    def test_post_provision_identity_replacement_is_detected(self) -> None:
        source = {
            "schema": horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "v274-auth.json"
            source_sha = horizon._write_exclusive_json(source_path, source)
            identity_directory = root / "identity"
            identity_directory.mkdir(mode=0o700)
            output_path = identity_directory / "public-execution-identity.json"
            with patch.object(
                horizon,
                "V274_PROVENANCE_AUTHORIZATION_SHA256",
                source_sha,
            ):
                identity, identity_sha = (
                    horizon.provision_public_execution_identity_from_v274(
                        source_path=source_path,
                        output_path=output_path,
                        require_root=False,
                    )
                )
                authorization = {
                    "public_execution_identity": identity,
                    "public_execution_identity_artifact_sha256": identity_sha,
                    "expected_signer": SIGNER,
                    "expected_funder": FUNDER,
                    "signature_type": 0,
                }
                output_path.rename(identity_directory / "original.json")
                replacement = dict(identity)
                replacement["expected_signer"] = "0x" + "7" * 40
                horizon._write_exclusive_json(output_path, replacement)
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon._validate_installed_public_execution_identity(
                        authorization,
                        path=output_path,
                        require_root_owned=False,
                        require_canonical_path=False,
                    )

    def test_public_identity_provenance_and_inode_attacks_are_rejected(self) -> None:
        source = {
            "schema": horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
            "expected_signer": SIGNER,
            "expected_funder": FUNDER,
            "signature_type": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "source.json"
            source_sha = horizon._write_exclusive_json(original, source)
            attacks = []
            symlink = root / "source-symlink.json"
            symlink.symlink_to(original)
            attacks.append(symlink)
            hardlink = root / "source-hardlink.json"
            os.link(original, hardlink)
            attacks.append(hardlink)
            wrong_mode = root / "source-mode.json"
            horizon._write_exclusive_json(wrong_mode, source)
            wrong_mode.chmod(0o644)
            attacks.append(wrong_mode)
            if os.geteuid() == 0:
                wrong_owner = root / "source-owner.json"
                horizon._write_exclusive_json(wrong_owner, source)
                os.chown(wrong_owner, 65534, 65534)
                with patch.object(
                    horizon,
                    "_require_trusted_directory_chain",
                ), self.assertRaises(horizon.HorizonRefusal):
                    horizon._read_secure_bytes(
                        wrong_owner,
                        require_root_owned=True,
                        require_mode_0600=True,
                        require_root_group=True,
                    )
                wrong_identity_group = root / "public-identity-group.json"
                horizon._write_exclusive_json(
                    wrong_identity_group,
                    PUBLIC_IDENTITY,
                )
                os.chown(wrong_identity_group, 0, 65534)
                with patch.object(
                    horizon,
                    "_validate_public_identity_directory",
                ), patch.object(
                    horizon,
                    "_require_trusted_directory_chain",
                ), self.assertRaises(horizon.HorizonRefusal):
                    horizon._read_public_execution_identity(
                        wrong_identity_group,
                        require_root_owned=True,
                        require_canonical_path=False,
                    )
                wrong_group = root / "source-group.json"
                horizon._write_exclusive_json(wrong_group, source)
                os.chown(wrong_group, 0, 65534)
                with patch.object(
                    horizon,
                    "_require_trusted_directory_chain",
                ), self.assertRaises(horizon.HorizonRefusal):
                    horizon._read_secure_bytes(
                        wrong_group,
                        require_root_owned=True,
                        require_mode_0600=True,
                        require_root_group=True,
                    )
            for index, attacked_source in enumerate(attacks):
                with self.subTest(attacked_source=attacked_source.name), patch.object(
                    horizon,
                    "V274_PROVENANCE_AUTHORIZATION_SHA256",
                    source_sha,
                ), self.assertRaises(horizon.HorizonRefusal):
                    horizon.provision_public_execution_identity_from_v274(
                        source_path=attacked_source,
                        output_path=root / f"identity-{index}.json",
                        require_root=False,
                    )

    def test_release_contract_binds_all_runtime_units_and_exact_env(self) -> None:
        contract = self.authorization["release_contract"]
        self.assertIs(contract["identity_provisioner_loads_live_env"], False)
        self.assertIs(contract["coordinator_loads_live_env"], False)
        self.assertIs(contract["prepare_loads_live_env"], False)
        self.assertIs(contract["execute_loads_live_env"], True)
        self.assertIs(
            contract["runtime_revalidates_public_identity_provenance"],
            True,
        )
        self.assertEqual(
            contract["public_identity_provenance_authorization_path"],
            str(horizon.CANONICAL_V274_PROVENANCE_AUTHORIZATION),
        )
        self.assertEqual(
            contract["public_identity_provenance_authorization_schema"],
            horizon.V274_PROVENANCE_AUTHORIZATION_SCHEMA,
        )
        self.assertEqual(
            contract["public_identity_provenance_authorization_sha256"],
            horizon.V274_PROVENANCE_AUTHORIZATION_SHA256,
        )
        self.assertEqual(
            contract["nonsecret_environment_sha256"],
            hashlib.sha256(horizon.NONSECRET_ENVIRONMENT_BYTES).hexdigest(),
        )
        for field in (
            "v274_release_sha256",
            "v271_collector_release_sha256",
            "v271_forward_release_sha256",
            "v2733_validator_release_sha256",
            "v273_runtime_release_sha256",
            "v273_preflight_release_sha256",
            "identity_provisioner_unit_sha256",
            "coordinator_unit_sha256",
            "prepare_unit_sha256",
            "execute_unit_sha256",
        ):
            self.assertRegex(contract[field], r"^[0-9a-f]{64}$")

    def test_root_receipt_cannot_be_backdated_before_manifest(self) -> None:
        valid = dict(self.horizon_receipt)
        valid["registered_at_epoch_millis"] = self.manifest[
            "manifest_created_at_epoch_millis"
        ]
        horizon._validate_horizon_receipt_clock(
            valid,
            authorization=self.authorization,
            manifest=self.manifest,
        )
        backdated = dict(valid)
        backdated["registered_at_epoch_millis"] -= 1
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._validate_horizon_receipt_clock(
                backdated,
                authorization=self.authorization,
                manifest=self.manifest,
            )


class DurableStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.schedule = horizon.build_schedule(START)
        self.schedule_sha = horizon.canonical_sha256(self.schedule)
        self.authorization = horizon.build_authorization(
            schedule=self.schedule,
            authorized_at_epoch_millis=START * 1000 - 300_000,
            expected_signer=SIGNER,
            expected_funder=FUNDER,
            signature_type=0,
            public_execution_identity_artifact_sha256=PUBLIC_IDENTITY_SHA,
            qualification_manifest_sha256="a" * 64,
            v275_release_sha256="b" * 64,
        )
        self.authorization_sha = horizon.canonical_sha256(self.authorization)
        self.paths = horizon.horizon_paths(self.root, self.authorization_sha)
        self.manifest = horizon.build_manifest(
            self.authorization,
            self.authorization_sha,
            self.schedule,
            created_at_epoch_millis=START * 1000 - 240_000,
        )
        self.manifest_sha = horizon.canonical_sha256(self.manifest)
        self.horizon_receipt_sha = "e" * 64
        self.claim = horizon._claim_payload(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            claimed_at_epoch_millis=START * 1000 - 120_000,
            boot_id=horizon._boot_id(),
        )
        self.claim_sha = horizon._write_exclusive_json(
            self.paths.claim,
            self.claim,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exclusive_claim_rejects_concurrent_second_writer(self) -> None:
        path = self.paths.run_dir / "separate-exclusive-claim.json"
        horizon._write_exclusive_json(path, {"first": True})
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._write_exclusive_json(path, {"second": True})

    def test_receipt_gap_rejects_arbitrary_skip(self) -> None:
        later = horizon.window_paths(self.paths, 1, START + 300)
        horizon._write_exclusive_json(later.window_receipt, {"later": True})
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )

    def test_buy_artifact_cannot_be_relabeled_cash(self) -> None:
        window = horizon.window_paths(self.paths, 0, START)
        request = horizon._request_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            claim_sha256=self.claim_sha,
            index=0,
            start=START,
            previous_window_receipt_sha256=None,
            requested_at_epoch_millis=START * 1000 - 60_000,
        )
        request_sha = horizon._write_exclusive_json(
            window.request,
            request,
        )
        child = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=0,
        )
        child_sha = horizon._write_exclusive_json(window.child_authorization, child)
        receipt = {
            **horizon._receipt_common(
                authorization_sha256=self.authorization_sha,
                schedule_sha256=self.schedule_sha,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                request_sha256=request_sha,
                child_sha256=child_sha,
                child=child,
                previous_window_receipt_sha256=None,
            ),
            "completed": True,
            "action": "cash",
            "cash_reason": "threshold_not_crossed",
            "qualification_anchor_sha256": "1" * 64,
            "live_signal_row_artifact_sha256": "2" * 64,
            "live_panel_record_sha256": "3" * 64,
            "completed_at_epoch_millis": START * 1000 + 242_000,
            "preauthorization_consumed": False,
            "signed": False,
            "post_attempts": 0,
            "orders": "none",
        }
        horizon._write_exclusive_json(window.window_receipt, receipt)
        horizon._write_exclusive_json(window.permit, {"buy": True})
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )

    def test_broken_symlink_cannot_hide_global_or_cash_artifact(self) -> None:
        self.paths.execute_invocation.parent.mkdir(parents=True, exist_ok=True)
        self.paths.execute_invocation.symlink_to(
            self.paths.run_dir / "missing-invocation-target.json"
        )
        terminal = horizon._finalize_after_delegation(
            paths=self.paths,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            completed_windows=0,
            clock=lambda: START * 1000 + 250_000,
            require_root_owned=False,
        )
        self.assertEqual(terminal["post_attempts"], 1)
        self.assertEqual(terminal["orders"], "one_fok_submission_unknown")

        with tempfile.TemporaryDirectory() as temporary:
            original_paths = self.paths
            original_claim_sha = self.claim_sha
            self.paths = horizon.horizon_paths(
                Path(temporary),
                self.authorization_sha,
            )
            try:
                self.claim_sha = horizon._write_exclusive_json(
                    self.paths.claim,
                    self.claim,
                )
                self._write_complete_window(0, None, action="cash")
                cash = horizon.window_paths(self.paths, 0, START)
                cash.candidate.symlink_to(cash.run_dir / "missing-candidate.json")
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon._scan_receipts(
                        self.paths,
                        authorization=self.authorization,
                        authorization_sha256=self.authorization_sha,
                        schedule=self.schedule,
                        schedule_sha256=self.schedule_sha,
                        manifest=self.manifest,
                        manifest_sha256=self.manifest_sha,
                        horizon_receipt_sha256=self.horizon_receipt_sha,
                        claim_sha256=self.claim_sha,
                        require_root_owned=False,
                    )
            finally:
                self.paths = original_paths
                self.claim_sha = original_claim_sha

    def test_eligible_later_stage_broken_links_force_unknown_without_global_markers(self) -> None:
        for stage_name in ("intent", "transport_started", "execution_result"):
            for broken_link in (False, True):
                with self.subTest(
                    stage_name=stage_name,
                    broken_link=broken_link,
                ), tempfile.TemporaryDirectory() as temporary:
                    self._assert_eligible_later_stage_is_unknown(
                        Path(temporary),
                        stage_name=stage_name,
                        broken_link=broken_link,
                    )

    def _assert_eligible_later_stage_is_unknown(
        self,
        temporary: Path,
        *,
        stage_name: str,
        broken_link: bool,
    ) -> None:
        original_paths = self.paths
        original_claim_sha = self.claim_sha
        self.paths = horizon.horizon_paths(
            temporary,
            self.authorization_sha,
        )
        try:
            self.claim_sha = horizon._write_exclusive_json(
                self.paths.claim,
                self.claim,
            )
            self._write_complete_window(0, None, action="eligible")
            window = horizon.window_paths(self.paths, 0, START)
            stage_path = getattr(window, stage_name)
            if broken_link:
                stage_path.symlink_to(
                    window.run_dir / f"missing-{stage_name}.json"
                )
            else:
                horizon._write_exclusive_json(
                    stage_path,
                    {"unexpected_stage": stage_name},
                )
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._scan_receipts(
                    self.paths,
                    authorization=self.authorization,
                    authorization_sha256=self.authorization_sha,
                    schedule=self.schedule,
                    schedule_sha256=self.schedule_sha,
                    manifest=self.manifest,
                    manifest_sha256=self.manifest_sha,
                    horizon_receipt_sha256=self.horizon_receipt_sha,
                    claim_sha256=self.claim_sha,
                    require_root_owned=False,
                )
            terminal = horizon._finalize_window_execution_stage_if_present(
                paths=self.paths,
                schedule=self.schedule,
                authorization_sha256=self.authorization_sha,
                schedule_sha256=self.schedule_sha,
                manifest_sha256=self.manifest_sha,
                completed_windows=1,
                clock=lambda: START * 1000 + 250_000,
                require_root_owned=False,
            )
            self.assertIsNotNone(terminal)
            self.assertEqual(terminal["post_attempts"], 1)
            self.assertEqual(
                terminal["orders"],
                "one_fok_submission_unknown",
            )
            self.assertFalse(self.paths.delegation.exists())
            self.assertFalse(self.paths.attempt.exists())
        finally:
            self.paths = original_paths
            self.claim_sha = original_claim_sha

    def _write_complete_window(
        self,
        index: int,
        previous_receipt_sha: str | None,
        *,
        action: str,
        request_claim_sha: str | None = None,
        child_index: int | None = None,
        requested_at_epoch_millis: int | None = None,
    ) -> tuple[dict, str]:
        start = START + index * 300
        window = horizon.window_paths(self.paths, index, start)
        request = horizon._request_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            claim_sha256=request_claim_sha or self.claim_sha,
            index=index,
            start=start,
            previous_window_receipt_sha256=previous_receipt_sha,
            requested_at_epoch_millis=(
                start * 1000 - 1_000
                if requested_at_epoch_millis is None
                else requested_at_epoch_millis
            ),
        )
        request_sha = horizon._write_exclusive_json(window.request, request)
        child = horizon.build_child(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            index=index if child_index is None else child_index,
        )
        child_sha = horizon._write_exclusive_json(
            window.child_authorization,
            child,
        )
        horizon._write_exclusive_json(
            window.v274_projection_receipt,
            {"authorization_sha256": child_sha},
        )
        anchor_sha = horizon._write_exclusive_json(
            window.qualification_anchor,
            {"authorization_sha256": child_sha},
        )
        record_sha = f"{index + 1:064x}"
        live_sha = horizon._write_exclusive_json(
            window.live_signal_row,
            {"panel_record_sha256": record_sha},
        )
        common = horizon._receipt_common(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            request_sha256=request_sha,
            child_sha256=child_sha,
            child=child,
            previous_window_receipt_sha256=previous_receipt_sha,
        )
        receipt = {
            **common,
            "completed": True,
            "action": action,
            "cash_reason": "threshold_not_crossed" if action == "cash" else None,
            "qualification_anchor_sha256": anchor_sha,
            "live_signal_row_artifact_sha256": live_sha,
            "live_panel_record_sha256": record_sha,
            "completed_at_epoch_millis": start * 1000 + 242_000,
            "preauthorization_consumed": False,
            "signed": False,
            "post_attempts": 0,
            "orders": "none",
        }
        if action == "eligible":
            account = {
                "expected_signer": self.authorization["expected_signer"],
                "expected_funder": self.authorization["expected_funder"],
                "signature_type": self.authorization["signature_type"],
                "public_execution_identity_artifact_sha256": self.authorization[
                    "public_execution_identity_artifact_sha256"
                ],
            }
            candidate_sha = horizon._write_exclusive_json(
                window.candidate,
                {"authorization_sha256": child_sha, **account},
            )
            permit_sha = horizon._write_exclusive_json(
                window.permit,
                {
                    "authorization_sha256": child_sha,
                    "candidate_artifact_sha256": candidate_sha,
                    **account,
                },
            )
            receipt.update(
                {
                    "candidate_artifact_sha256": candidate_sha,
                    "permit_artifact_sha256": permit_sha,
                    "limit_price": "0.30",
                }
            )
        receipt_sha = horizon._write_exclusive_json(
            window.window_receipt,
            receipt,
        )
        return receipt, receipt_sha

    def test_backdated_first_request_before_claim_is_rejected(self) -> None:
        self._write_complete_window(
            0,
            None,
            action="cash",
            requested_at_epoch_millis=self.claim["claimed_at_epoch_millis"] - 1,
        )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )

    def test_backdated_next_request_before_previous_cash_completion_is_rejected(self) -> None:
        first, previous_sha = self._write_complete_window(
            0,
            None,
            action="cash",
        )
        self._write_complete_window(
            1,
            previous_sha,
            action="cash",
            requested_at_epoch_millis=first["completed_at_epoch_millis"] - 1,
        )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )

    def test_all_144_cash_boundaries_are_exact_and_no_window_145_exists(self) -> None:
        previous = None
        for index in range(horizon.HORIZON_WINDOWS):
            _receipt, previous = self._write_complete_window(
                index,
                previous,
                action="cash",
            )
        next_index, final_sha, eligible = horizon._scan_receipts(
            self.paths,
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule=self.schedule,
            schedule_sha256=self.schedule_sha,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.horizon_receipt_sha,
            claim_sha256=self.claim_sha,
            require_root_owned=False,
        )
        self.assertEqual(next_index, 144)
        self.assertEqual(final_sha, previous)
        self.assertIsNone(eligible)
        self.assertFalse(
            (self.paths.run_dir / "windows" / f"144-{START + 43_200}").exists()
        )
        self.assertFalse(self.paths.delegation.exists())
        self.assertFalse(self.paths.attempt.exists())

    def test_first_eligible_rejects_even_empty_window_143_preseed(self) -> None:
        self._write_complete_window(0, None, action="eligible")
        future = horizon.window_paths(
            self.paths,
            143,
            START + 143 * 300,
        )
        future.run_dir.mkdir(parents=True)
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )

    def test_forged_request_and_child_transplant_are_rejected(self) -> None:
        self._write_complete_window(
            0,
            None,
            action="cash",
            request_claim_sha="9" * 64,
        )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._scan_receipts(
                self.paths,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha,
                manifest=self.manifest,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.horizon_receipt_sha,
                claim_sha256=self.claim_sha,
                require_root_owned=False,
            )
        with tempfile.TemporaryDirectory() as temporary:
            original_paths = self.paths
            self.paths = horizon.horizon_paths(
                Path(temporary),
                self.authorization_sha,
            )
            try:
                self.claim_sha = horizon._write_exclusive_json(
                    self.paths.claim,
                    self.claim,
                )
                self._write_complete_window(
                    0,
                    None,
                    action="cash",
                    child_index=1,
                )
                with self.assertRaises(horizon.HorizonRefusal):
                    horizon._scan_receipts(
                        self.paths,
                        authorization=self.authorization,
                        authorization_sha256=self.authorization_sha,
                        schedule=self.schedule,
                        schedule_sha256=self.schedule_sha,
                        manifest=self.manifest,
                        manifest_sha256=self.manifest_sha,
                        horizon_receipt_sha256=self.horizon_receipt_sha,
                        claim_sha256=self.claim_sha,
                        require_root_owned=False,
                    )
            finally:
                self.paths = original_paths

    def _delegation(
        self,
        *,
        window_receipt_sha256: str = "1" * 64,
        child_sha256: str = "2" * 64,
        permit_sha256: str = "3" * 64,
        delegated_at_epoch_millis: int = START * 1000 + 242_000,
    ) -> dict:
        return {
            "schema": horizon.DELEGATION_SCHEMA,
            "parent_horizon_authorization_sha256": self.authorization_sha,
            "schedule_sha256": self.schedule_sha,
            "manifest_sha256": self.manifest_sha,
            "window_index": 0,
            "exact_window_start_epoch": START,
            "exact_window_end_epoch": START + 300,
            "window_receipt_sha256": window_receipt_sha256,
            "derived_child_authorization_sha256": child_sha256,
            "permit_artifact_sha256": permit_sha256,
            "execute_unit_invocations": 1,
            "maximum_execute_unit_invocations": 1,
            "delegated_at_epoch_millis": delegated_at_epoch_millis,
            "retry_permitted": False,
            "global_horizon_consumed": True,
            "horizon_terminal_on_creation": True,
            "possible_post_attempts_if_process_state_unknown": 1,
            "orders_if_process_state_unknown": "one_fok_submission_unknown",
        }

    def test_consuming_delegation_without_stage_is_unknown_not_zero(self) -> None:
        horizon._write_exclusive_json(self.paths.delegation, self._delegation())
        terminal = horizon._finalize_after_delegation(
            paths=self.paths,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            completed_windows=1,
            clock=lambda: START * 1000 + 250_000,
            require_root_owned=False,
        )
        self.assertEqual(terminal["post_attempts"], 1)
        self.assertEqual(terminal["orders"], "one_fok_submission_unknown")

    def test_forged_delegation_is_rejected_before_child_path_selection(self) -> None:
        valid = self._delegation()
        index, start = horizon._validate_delegation(
            valid,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            expected_window_receipt_sha256="1" * 64,
            expected_window_receipt_completed_at_epoch_millis=(
                START * 1000 + 242_000
            ),
            expected_child_sha256="2" * 64,
            expected_permit_sha256="3" * 64,
        )
        self.assertEqual((index, start), (0, START))
        backdated = dict(valid)
        backdated["delegated_at_epoch_millis"] -= 1
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._validate_delegation(
                backdated,
                authorization_sha256=self.authorization_sha,
                schedule_sha256=self.schedule_sha,
                manifest_sha256=self.manifest_sha,
                schedule=self.schedule,
                expected_window_receipt_completed_at_epoch_millis=(
                    START * 1000 + 242_000
                ),
            )
        for field, value in (
            ("parent_horizon_authorization_sha256", "9" * 64),
            ("window_index", 143),
            ("retry_permitted", True),
            ("execute_unit_invocations", 2),
        ):
            forged = dict(valid)
            forged[field] = value
            with self.subTest(field=field), self.assertRaises(
                horizon.HorizonRefusal
            ):
                horizon._validate_delegation(
                    forged,
                    authorization_sha256=self.authorization_sha,
                    schedule_sha256=self.schedule_sha,
                    manifest_sha256=self.manifest_sha,
                    schedule=self.schedule,
                )

    def test_attempt_without_readable_delegation_still_durable_unknown(self) -> None:
        horizon._write_exclusive_json(self.paths.attempt, {"attempt": True})
        terminal = horizon._finalize_after_delegation(
            paths=self.paths,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            completed_windows=0,
            clock=lambda: START * 1000 + 250_000,
            require_root_owned=False,
        )
        self.assertEqual(terminal["post_attempts"], 1)
        self.assertEqual(terminal["orders"], "one_fok_submission_unknown")
        self.assertTrue(self.paths.terminal.exists())

    def test_malformed_delegation_writes_unknown_terminal_not_restart_loop(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        self.paths.delegation.write_text("{not-canonical", encoding="utf-8")
        self.paths.delegation.chmod(0o600)
        terminal = horizon._finalize_after_delegation(
            paths=self.paths,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            completed_windows=0,
            clock=lambda: START * 1000 + 250_000,
            require_root_owned=False,
        )
        self.assertEqual(terminal["post_attempts"], 1)
        self.assertEqual(terminal["orders"], "one_fok_submission_unknown")
        self.assertTrue(self.paths.terminal.exists())

    def test_submission_unknown_and_truncated_response_are_unknown_no_retry(self) -> None:
        for truncated_response in (False, True):
            with self.subTest(truncated_response=truncated_response):
                with tempfile.TemporaryDirectory() as temporary:
                    paths = horizon.horizon_paths(
                        Path(temporary),
                        self.authorization_sha,
                    )
                    horizon._write_exclusive_json(paths.delegation, self._delegation())
                    window = horizon.window_paths(paths, 0, START)
                    if truncated_response:
                        horizon._write_exclusive_json(
                            window.execution_result,
                            {
                                "schema": horizon.V274_EXECUTION_RESULT_SCHEMA,
                                "execution_state": "response_recorded",
                                "post_attempts": 1,
                                "orders": "one_fok_attempt",
                                "retry_permitted": False,
                            },
                        )
                    else:
                        horizon._write_exclusive_json(
                            window.submission_unknown,
                            {"submission_unknown": True},
                        )
                    terminal = horizon._finalize_after_delegation(
                        paths=paths,
                        authorization_sha256=self.authorization_sha,
                        schedule_sha256=self.schedule_sha,
                        manifest_sha256=self.manifest_sha,
                        schedule=self.schedule,
                        completed_windows=1,
                        clock=lambda: START * 1000 + 250_000,
                        require_root_owned=False,
                    )
                    self.assertEqual(terminal["post_attempts"], 1)
                    self.assertEqual(
                        terminal["orders"],
                        "one_fok_submission_unknown",
                    )
                    self.assertFalse(terminal["retry_permitted"])

    def test_only_full_v275_and_v274_bound_response_is_recorded(self) -> None:
        paths = self.paths
        window = horizon.window_paths(paths, 0, START)
        child_sha = "2" * 64
        candidate_sha = "4" * 64
        shared = {
            "qualification_experiment_id": "q-v275-test",
            "qualification_report_sha256": "5" * 64,
            "live_signal_run_id": "live-v275-test",
            "live_signal_definition_sha256": "6" * 64,
            "exact_window_start_epoch": START,
        }
        permit = {
            **shared,
            "authorization_sha256": child_sha,
            "candidate_artifact_sha256": candidate_sha,
            "side": "BUY",
            "order_type": "FOK",
            "exact_taker_amount_micro": 5_000_000,
            "maximum_total_reserve_micro": 2_500_000,
            "maximum_live_attempts": 1,
            "transport_retry_enabled": False,
        }
        permit_sha = horizon._write_exclusive_json(window.permit, permit)
        receipt = {
            "schema": horizon.WINDOW_RECEIPT_SCHEMA,
            "parent_horizon_authorization_sha256": self.authorization_sha,
            "schedule_sha256": self.schedule_sha,
            "manifest_sha256": self.manifest_sha,
            "horizon_receipt_sha256": self.horizon_receipt_sha,
            "strict_next_request_sha256": "7" * 64,
            "derived_child_authorization_sha256": child_sha,
            "child_commitment_sha256": "8" * 64,
            "authorization_state": "derived_from_explicit_bounded_horizon",
            "window_index": 0,
            "exact_window_start_epoch": START,
            "exact_window_end_epoch": START + 300,
            "previous_window_receipt_sha256": None,
            "maximum_post_attempts_global": 1,
            "transport_retry_enabled": False,
            "completed": True,
            "action": "eligible",
            "cash_reason": None,
            "qualification_anchor_sha256": "9" * 64,
            "live_signal_row_artifact_sha256": "a" * 64,
            "live_panel_record_sha256": "b" * 64,
            "candidate_artifact_sha256": candidate_sha,
            "permit_artifact_sha256": permit_sha,
            "limit_price": "0.30",
            "completed_at_epoch_millis": START * 1000 + 242_000,
            "preauthorization_consumed": False,
            "signed": False,
            "post_attempts": 0,
            "orders": "none",
        }
        receipt_sha = horizon._write_exclusive_json(window.window_receipt, receipt)
        delegation = self._delegation(
            window_receipt_sha256=receipt_sha,
            child_sha256=child_sha,
            permit_sha256=permit_sha,
            delegated_at_epoch_millis=START * 1000 + 242_001,
        )
        delegation_sha = horizon._write_exclusive_json(paths.delegation, delegation)
        invocation = {
            "schema": horizon.EXECUTE_INVOCATION_SCHEMA,
            "parent_horizon_authorization_sha256": self.authorization_sha,
            "schedule_sha256": self.schedule_sha,
            "manifest_sha256": self.manifest_sha,
            "consuming_delegation_sha256": delegation_sha,
            "window_receipt_sha256": receipt_sha,
            "derived_child_authorization_sha256": child_sha,
            "permit_artifact_sha256": permit_sha,
            "window_index": 0,
            "exact_window_start_epoch": START,
            "exact_window_end_epoch": START + 300,
            "execute_unit_invocation": 1,
            "maximum_execute_unit_invocations": 1,
            "invoked_at_epoch_millis": START * 1000 + 242_002,
            "retry_permitted": False,
            "horizon_terminal_on_creation": True,
        }
        backdated_invocation = dict(invocation)
        backdated_invocation["invoked_at_epoch_millis"] = delegation[
            "delegated_at_epoch_millis"
        ] - 1
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._validate_execute_invocation(
                backdated_invocation,
                authorization_sha256=self.authorization_sha,
                schedule_sha256=self.schedule_sha,
                manifest_sha256=self.manifest_sha,
                delegation_sha256=delegation_sha,
                window_receipt_sha256=receipt_sha,
                child_sha256=child_sha,
                permit_sha256=permit_sha,
                window_index=0,
                exact_window_start_epoch=START,
                exact_window_end_epoch=START + 300,
                delegated_at_epoch_millis=delegation[
                    "delegated_at_epoch_millis"
                ],
            )
        invocation_sha = horizon._write_exclusive_json(
            paths.execute_invocation,
            invocation,
        )
        attempt = {
            "schema": horizon.ATTEMPT_SCHEMA,
            "parent_horizon_authorization_sha256": self.authorization_sha,
            "derived_child_authorization_sha256": child_sha,
            "window_index": 0,
            "permit_artifact_sha256": permit_sha,
            "execute_invocation_sha256": invocation_sha,
            "maximum_post_attempts": 1,
            "execute_once_invocations": 1,
            "transport_retry_enabled": False,
            "horizon_terminal_on_creation": True,
            "claimed_at_epoch_millis": START * 1000 + 242_003,
        }
        backdated_attempt = dict(attempt)
        backdated_attempt["claimed_at_epoch_millis"] = invocation[
            "invoked_at_epoch_millis"
        ] - 1
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._validate_global_attempt(
                backdated_attempt,
                authorization_sha256=self.authorization_sha,
                child_sha256=child_sha,
                window_index=0,
                permit_sha256=permit_sha,
                invocation_sha256=invocation_sha,
                invoked_at_epoch_millis=invocation[
                    "invoked_at_epoch_millis"
                ],
            )
        horizon._write_exclusive_json(paths.attempt, attempt)
        intent = {
            "schema": horizon.V274_INTENT_SCHEMA,
            "orchestrator_version": horizon.V274_VERSION,
            "permit_artifact_sha256": permit_sha,
            **shared,
            "maximum_post_attempts": 1,
            "transport_retry_enabled": False,
            "preauthorization_consumed": True,
            "signed": True,
            "execution_state": "pre_submit_verified",
            "post_attempts": 0,
            "reserved_at_epoch_millis": START * 1000 + 242_004,
        }
        intent_sha = horizon._write_exclusive_json(window.intent, intent)
        transport = {
            "schema": horizon.V274_TRANSPORT_STARTED_SCHEMA,
            "orchestrator_version": horizon.V274_VERSION,
            "permit_artifact_sha256": permit_sha,
            "intent_sha256": intent_sha,
            **shared,
            "execution_state": "transport_started_response_not_yet_durable",
            "preauthorization_consumed": True,
            "intent_reserved": True,
            "signed": True,
            "post_attempts": 1,
            "transport_retry_enabled": False,
            "retry_permitted": False,
            "orders": "one_fok_submission_unknown",
            "started_at_epoch_millis": START * 1000 + 242_005,
        }
        transport_sha = horizon._write_exclusive_json(
            window.transport_started,
            transport,
        )
        result = {
            "schema": horizon.V274_EXECUTION_RESULT_SCHEMA,
            "orchestrator_version": horizon.V274_VERSION,
            "permit_artifact_sha256": permit_sha,
            "intent_sha256": intent_sha,
            "transport_started_sha256": transport_sha,
            **shared,
            "execution_state": "response_recorded",
            "order_contract_sha256": "c" * 64,
            "maker_amount_micro": 2_000_000,
            "taker_amount_micro": 5_000_000,
            "fee_reserve_micro": 100_000,
            "preauthorization_consumed": True,
            "intent_reserved": True,
            "signed": True,
            "post_attempts": 1,
            "transport_retry_enabled": False,
            "retry_permitted": False,
            "orders": "one_fok_attempt",
            "outcome": {"status": "filled"},
            "completed_at_epoch_millis": START * 1000 + 242_006,
        }
        horizon._write_exclusive_json(window.execution_result, result)
        terminal = horizon._finalize_after_delegation(
            paths=paths,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            schedule=self.schedule,
            completed_windows=1,
            clock=lambda: START * 1000 + 250_000,
            require_root_owned=False,
        )
        self.assertEqual(terminal["post_attempts"], 1)
        self.assertEqual(terminal["orders"], "one_fok_attempt")
        self.assertFalse(terminal["retry_permitted"])

    def test_existing_terminal_identity_is_revalidated(self) -> None:
        bad = horizon._terminal_payload(
            authorization_sha256="9" * 64,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            decision="bad",
            completed_windows=0,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=START * 1000,
        )
        horizon._write_exclusive_json(self.paths.terminal, bad)
        expected = horizon._terminal_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            decision="expected",
            completed_windows=0,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=START * 1000,
        )
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._write_terminal(self.paths, expected, require_root_owned=False)

    def test_terminal_decision_counts_and_orders_are_exact(self) -> None:
        valid_no_trade = horizon._terminal_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            decision="horizon_completed_144_cash_no_trade",
            completed_windows=144,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=START * 1000 + 43_200_000,
        )
        horizon._validate_terminal(
            valid_no_trade,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
        )
        for mutation in (
            {"completed_windows": 143},
            {"post_attempts": 1, "orders": "one_fok_attempt"},
            {"decision": "unbounded_arbitrary_terminal"},
        ):
            invalid = {**valid_no_trade, **mutation}
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_terminal(
                    invalid,
                    authorization_sha256=self.authorization_sha,
                    schedule_sha256=self.schedule_sha,
                    manifest_sha256=self.manifest_sha,
                )
        unknown = horizon._terminal_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            decision="consuming_delegation_process_state_unknown_terminal",
            completed_windows=1,
            post_attempts=1,
            orders="one_fok_submission_unknown",
            completed_at_epoch_millis=START * 1000 + 250_000,
        )
        horizon._validate_terminal(
            unknown,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
        )

    def test_terminal_oxcl_race_consumes_the_validated_winner(self) -> None:
        requested = horizon._terminal_payload(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            decision="strict_next_ledger_refusal_terminal",
            completed_windows=0,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=START * 1000,
        )
        winner = {
            **requested,
            "decision": "launch_window_missed_terminal",
        }
        original = horizon._write_exclusive_json

        def simultaneous_winner(path, _payload):
            original(path, winner)
            raise horizon.HorizonRefusal(
                "immutable_output_exists",
                "simulated simultaneous terminal winner",
            )

        with patch.object(
            horizon,
            "_write_exclusive_json",
            side_effect=simultaneous_winner,
        ):
            accepted = horizon._write_terminal(
                self.paths,
                requested,
                require_root_owned=False,
            )
        self.assertEqual(accepted, winner)


class CoordinatorBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schedule = horizon.build_schedule(START)
        self.schedule_sha = horizon.canonical_sha256(self.schedule)
        self.authorization = horizon.build_authorization(
            schedule=self.schedule,
            authorized_at_epoch_millis=START * 1000 - 300_000,
            expected_signer=SIGNER,
            expected_funder=FUNDER,
            signature_type=0,
            public_execution_identity_artifact_sha256=PUBLIC_IDENTITY_SHA,
            qualification_manifest_sha256="a" * 64,
            v275_release_sha256="b" * 64,
        )
        self.authorization_sha = horizon.canonical_sha256(self.authorization)
        self.manifest = horizon.build_manifest(
            self.authorization,
            self.authorization_sha,
            self.schedule,
            created_at_epoch_millis=START * 1000 - 240_000,
        )
        self.manifest_sha = horizon.canonical_sha256(self.manifest)
        self.receipt = horizon.build_horizon_receipt(
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            release_contract_sha256=self.authorization["release_contract_sha256"],
            public_execution_identity_artifact_sha256=PUBLIC_IDENTITY_SHA,
            shared_execution_lock_device=11,
            shared_execution_lock_inode=22,
            shared_execution_lock_group_gid=1000,
            effective_systemd_contract_sha256="c" * 64,
            registered_at_epoch_millis=START * 1000 - 180_000,
        )
        self.receipt_sha = horizon.canonical_sha256(self.receipt)

    def _loaded(self):
        return (
            self.authorization,
            self.authorization_sha,
            self.schedule,
            self.schedule_sha,
            self.manifest,
            self.manifest_sha,
            self.receipt,
            self.receipt_sha,
        )

    def test_coordinator_never_wraps_orphan_window_stage_as_zero(self) -> None:
        for stage_name in ("intent", "transport_started", "execution_result"):
            with self.subTest(stage_name=stage_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "v275"
                paths = horizon.horizon_paths(root, self.authorization_sha)
                window = horizon.window_paths(paths, 0, START)
                window.run_dir.mkdir(parents=True)
                getattr(window, stage_name).symlink_to(
                    window.run_dir / f"missing-{stage_name}.json"
                )
                with patch.object(
                    horizon,
                    "load_contract",
                    return_value=self._loaded(),
                ):
                    terminal = horizon.coordinate(
                        root=root,
                        systemctl_path=Path("/bin/true"),
                        now_epoch_millis=lambda: START * 1000 - 60_000,
                        require_root=False,
                    )
                self.assertEqual(terminal["post_attempts"], 1)
                self.assertEqual(
                    terminal["orders"],
                    "one_fok_submission_unknown",
                )
                self.assertFalse(paths.claim.exists())
                self.assertFalse(paths.delegation.exists())

    def test_cli_failure_recovery_scans_all_window_stages_and_broken_links(self) -> None:
        stage_names = (
            "completion",
            "intent",
            "transport_started",
            "submission_unknown",
            "execution_result",
            "execute_wrapper_result",
        )
        for stage_name in stage_names:
            for broken_link in (False, True):
                with self.subTest(
                    stage_name=stage_name,
                    broken_link=broken_link,
                ), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "v275"
                    paths = horizon.horizon_paths(root, self.authorization_sha)
                    window = horizon.window_paths(paths, 143, START + 143 * 300)
                    stage_path = getattr(window, stage_name)
                    if broken_link:
                        stage_path.parent.mkdir(parents=True)
                        stage_path.symlink_to(
                            window.run_dir / f"missing-{stage_name}.json"
                        )
                    else:
                        horizon._write_exclusive_json(
                            stage_path,
                            {"orphan_stage": stage_name},
                        )
                    with patch.object(
                        horizon,
                        "load_contract",
                        return_value=self._loaded(),
                    ), patch.object(horizon, "CANONICAL_ROOT", root):
                        status = horizon._durable_cli_failure_status(
                            "coordinate",
                            horizon.HorizonRefusal(
                                "forced_before_stage_aware_coordinate",
                                "forced",
                            ),
                        )
                    self.assertEqual(status["post_attempts"], 1)
                    self.assertEqual(
                        status["orders"],
                        "one_fok_submission_unknown",
                    )
                    self.assertEqual(status["window_index"], 143)
                    self.assertEqual(
                        status["execution_stage_artifact"],
                        stage_name,
                    )

    def test_main_coordinate_failure_uses_stage_aware_cli_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "v275"
            paths = horizon.horizon_paths(root, self.authorization_sha)
            window = horizon.window_paths(paths, 0, START)
            horizon._write_exclusive_json(
                window.intent,
                {"orphan_stage": "intent"},
            )
            with patch.object(
                horizon,
                "coordinate",
                side_effect=horizon.HorizonRefusal(
                    "forced_before_stage_aware_coordinate",
                    "forced",
                ),
            ), patch.object(
                horizon,
                "load_contract",
                return_value=self._loaded(),
            ), patch.object(
                horizon,
                "CANONICAL_ROOT",
                root,
            ), patch("builtins.print") as output:
                return_code = horizon.main(["coordinate"])
            self.assertEqual(return_code, 2)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["post_attempts"], 1)
            self.assertEqual(
                payload["orders"],
                "one_fok_submission_unknown",
            )
            self.assertEqual(payload["execution_stage_artifact"], "intent")

    def test_initial_claim_exact_two_minute_launch_boundaries(self) -> None:
        cases = (
            (
                self.authorization[
                    "coordinator_launch_not_before_epoch_millis"
                ]
                - 1,
                "too_early",
            ),
            (
                self.authorization[
                    "coordinator_launch_not_before_epoch_millis"
                ],
                "accepted",
            ),
            (
                self.authorization[
                    "coordinator_launch_not_after_epoch_millis"
                ],
                "accepted",
            ),
            (START * 1000, "missed_terminal"),
        )
        for now_ms, outcome in cases:
            with self.subTest(now_ms=now_ms), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "v275"
                with patch.object(
                    horizon,
                    "load_contract",
                    return_value=self._loaded(),
                ), patch.object(
                    horizon,
                    "_boot_id",
                    return_value="11111111-1111-1111-1111-111111111111",
                ), patch.object(
                    horizon,
                    "_coordinate_under_claim_lease",
                    return_value={"accepted": True},
                ):
                    if outcome == "accepted":
                        result = horizon.coordinate(
                            root=root,
                            systemctl_path=Path("/bin/true"),
                            now_epoch_millis=lambda: now_ms,
                            require_root=False,
                        )
                        self.assertTrue(result["accepted"])
                        paths = horizon.horizon_paths(
                            root,
                            self.authorization_sha,
                        )
                        self.assertTrue(paths.claim.exists())
                    elif outcome == "too_early":
                        with self.assertRaises(horizon.HorizonRefusal):
                            horizon.coordinate(
                                root=root,
                                systemctl_path=Path("/bin/true"),
                                now_epoch_millis=lambda: now_ms,
                                require_root=False,
                            )
                    else:
                        terminal = horizon.coordinate(
                            root=root,
                            systemctl_path=Path("/bin/true"),
                            now_epoch_millis=lambda: now_ms,
                            require_root=False,
                        )
                        self.assertEqual(
                            terminal["decision"],
                            "launch_window_missed_terminal",
                        )
                        self.assertEqual(
                            (terminal["post_attempts"], terminal["orders"]),
                            (0, "none"),
                        )
                        self.assertEqual(
                            horizon.coordinate(
                                root=root,
                                systemctl_path=Path("/bin/true"),
                                now_epoch_millis=lambda: now_ms + 2_000,
                                require_root=False,
                            ),
                            terminal,
                        )

    def test_claim_validation_rejects_forged_time_outside_launch_band(self) -> None:
        not_before = self.authorization[
            "coordinator_launch_not_before_epoch_millis"
        ]
        not_after = self.authorization[
            "coordinator_launch_not_after_epoch_millis"
        ]
        for claimed_at, accepted in (
            (not_before - 1, False),
            (not_before, True),
            (not_after, True),
            (START * 1000, False),
        ):
            with self.subTest(claimed_at=claimed_at):
                claim = horizon._claim_payload(
                    authorization=self.authorization,
                    authorization_sha256=self.authorization_sha,
                    schedule_sha256=self.schedule_sha,
                    manifest_sha256=self.manifest_sha,
                    horizon_receipt_sha256=self.receipt_sha,
                    claimed_at_epoch_millis=claimed_at,
                    boot_id="55555555-5555-5555-5555-555555555555",
                )
                if accepted:
                    horizon._validate_claim(
                        claim,
                        authorization=self.authorization,
                        authorization_sha256=self.authorization_sha,
                        schedule_sha256=self.schedule_sha,
                        manifest_sha256=self.manifest_sha,
                        horizon_receipt_sha256=self.receipt_sha,
                        enforce_current_boot=False,
                    )
                else:
                    with self.assertRaises(horizon.HorizonRefusal):
                        horizon._validate_claim(
                            claim,
                            authorization=self.authorization,
                            authorization_sha256=self.authorization_sha,
                            schedule_sha256=self.schedule_sha,
                            manifest_sha256=self.manifest_sha,
                            horizon_receipt_sha256=self.receipt_sha,
                            enforce_current_boot=False,
                        )

    def _durable_resume_terminal(
        self,
        *,
        claim_boot: str,
        observed_boot: str,
        claimed_at: int,
        observed_at: int,
    ) -> dict:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "v275"
        paths = horizon.horizon_paths(root, self.authorization_sha)
        claim = horizon._claim_payload(
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha,
            schedule_sha256=self.schedule_sha,
            manifest_sha256=self.manifest_sha,
            horizon_receipt_sha256=self.receipt_sha,
            claimed_at_epoch_millis=claimed_at,
            boot_id=claim_boot,
        )
        horizon._write_exclusive_json(paths.claim, claim)
        with patch.object(
            horizon,
            "load_contract",
            return_value=self._loaded(),
        ), patch.object(
            horizon,
            "_boot_id",
            return_value=observed_boot,
        ):
            return horizon.coordinate(
                root=root,
                systemctl_path=Path("/bin/true"),
                now_epoch_millis=lambda: observed_at,
                require_root=False,
            )

    def test_boot_and_wall_clock_drift_write_durable_terminals(self) -> None:
        boot = self._durable_resume_terminal(
            claim_boot="11111111-1111-1111-1111-111111111111",
            observed_boot="22222222-2222-2222-2222-222222222222",
            claimed_at=START * 1000 - 60_000,
            observed_at=START * 1000 + 1_000,
        )
        self.assertEqual(boot["decision"], "boot_drift_terminal")
        self.assertEqual((boot["post_attempts"], boot["orders"]), (0, "none"))
        rollback = self._durable_resume_terminal(
            claim_boot="33333333-3333-3333-3333-333333333333",
            observed_boot="33333333-3333-3333-3333-333333333333",
            claimed_at=START * 1000 - 60_000,
            observed_at=START * 1000 - 60_001,
        )
        self.assertEqual(rollback["decision"], "wall_clock_rollback_terminal")

    def test_restart_with_existing_eligible_receipt_never_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "v275"
            paths = horizon.horizon_paths(root, self.authorization_sha)
            boot_id = "44444444-4444-4444-4444-444444444444"
            claim = horizon._claim_payload(
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha,
                schedule_sha256=self.schedule_sha,
                manifest_sha256=self.manifest_sha,
                horizon_receipt_sha256=self.receipt_sha,
                claimed_at_epoch_millis=START * 1000 - 60_000,
                boot_id=boot_id,
            )
            horizon._write_exclusive_json(paths.claim, claim)
            eligible = {"window_index": 0, "action": "eligible"}
            with patch.object(
                horizon,
                "load_contract",
                return_value=self._loaded(),
            ), patch.object(
                horizon,
                "_boot_id",
                return_value=boot_id,
            ), patch.object(
                horizon,
                "_scan_receipts",
                return_value=(0, "f" * 64, eligible),
            ), patch.object(
                horizon,
                "_systemctl_start",
            ) as start_unit:
                terminal = horizon.coordinate(
                    root=root,
                    systemctl_path=Path("/bin/true"),
                    now_epoch_millis=lambda: START * 1000 + 243_000,
                    require_root=False,
                )
            self.assertEqual(
                terminal["decision"],
                "eligible_boundary_without_same_process_delegation_terminal",
            )
            self.assertEqual(
                (terminal["post_attempts"], terminal["orders"]),
                (0, "none"),
            )
            self.assertFalse(paths.delegation.exists())
            start_unit.assert_not_called()

    def test_claim_inode_lease_rejects_concurrent_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            claim_path = Path(temporary) / "claim.json"
            claim_sha = horizon._write_exclusive_json(
                claim_path,
                {"claim": True},
            )
            with horizon._exclusive_claim_lease(
                claim_path,
                expected_claim_sha256=claim_sha,
                require_root_owned=False,
            ):
                with self.assertRaises(horizon.HorizonRefusal):
                    with horizon._exclusive_claim_lease(
                        claim_path,
                        expected_claim_sha256=claim_sha,
                        require_root_owned=False,
                    ):
                        self.fail("second coordinator acquired the claim lease")


class ExecuteOrderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.authorization_sha = "a" * 64
        self.paths = horizon.horizon_paths(self.root, self.authorization_sha)
        self.window = horizon.window_paths(self.paths, 0, START)
        self.child = {"window_index": 0}
        self.permit = {
            "execute_not_before_epoch_millis": START * 1000 + 240_000,
            "execute_not_after_epoch_millis": START * 1000 + 253_000,
        }
        self.permit_sha = "b" * 64

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_identity_replacement_before_invocation_never_consumes_invocation(self) -> None:
        schedule = {
            "windows": [
                {
                    "exact_window_start_epoch": START,
                    "exact_window_end_epoch": START + 300,
                }
            ]
        }
        authorization: dict = {}
        child: dict = {}
        receipt = {
            "action": "eligible",
            "completed_at_epoch_millis": START * 1000 + 243_000,
        }
        delegation = {
            "delegated_at_epoch_millis": START * 1000 + 243_500,
        }

        def read(path, **_kwargs):
            if path == self.paths.claim:
                return {}, "d" * 64
            if path == self.window.child_authorization:
                return child, "c" * 64
            if path == self.window.permit:
                return {}, self.permit_sha
            if path == self.paths.delegation:
                return delegation, "e" * 64
            raise AssertionError(f"unexpected read: {path}")

        with patch.object(
            horizon,
            "load_contract",
            return_value=(
                authorization,
                self.authorization_sha,
                schedule,
                "f" * 64,
                {},
                "1" * 64,
                {},
                "2" * 64,
            ),
        ), patch.object(
            horizon,
            "read_canonical_object",
            side_effect=read,
        ), patch.object(
            horizon,
            "_validate_claim",
        ), patch.object(
            horizon,
            "_scan_receipts",
            return_value=(0, "3" * 64, receipt),
        ), patch.object(
            horizon,
            "build_child",
            return_value=child,
        ), patch.object(
            horizon,
            "_validate_delegation",
        ), patch.object(
            horizon,
            "_revalidate_execution_public_identity",
            side_effect=horizon.HorizonRefusal(
                "installed_public_execution_identity_mismatch",
                "injected pre-invocation replacement",
            ),
        ):
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._claim_execute_invocation(
                    files=horizon.ContractFiles(
                        Path("authorization"),
                        Path("schedule"),
                        Path("manifest"),
                        Path("receipt"),
                    ),
                    root=self.root,
                    invoked_at_epoch_millis=START * 1000 + 244_000,
                    require_root_owned=False,
                )
        self.assertFalse(self.paths.execute_invocation.exists())

    def test_full_verify_then_attempt_fsync_then_same_execute_once(self) -> None:
        events: list[str] = []
        original_write = horizon._write_exclusive_json

        def claimed(**_kwargs):
            events.append("execute_invocation_claim")
            invocation = {
                "schema": horizon.EXECUTE_INVOCATION_SCHEMA,
                "invoked_at_epoch_millis": START * 1000 + 244_000,
            }
            invocation_sha = horizon._write_exclusive_json(
                self.paths.execute_invocation,
                invocation,
            )
            return invocation, invocation_sha

        def loaded(**_kwargs):
            events.append("full_verify")
            return (
                fake_v274,
                {},
                self.authorization_sha,
                self.paths,
                self.window,
                self.child,
                "c" * 64,
                self.permit,
                self.permit_sha,
            )

        def execute_once(**kwargs):
            self.assertTrue(self.paths.attempt.exists())
            self.assertIs(kwargs["permit"], self.permit)
            self.assertEqual(kwargs["permit_artifact_sha256"], self.permit_sha)
            events.append("execute_once")
            return {"ok": True}

        def recording_write(path, payload):
            if path == self.paths.attempt:
                events.append("global_attempt_claim")
            return original_write(path, payload)

        identity_rechecks = iter(
            ("identity_pre_attempt", "identity_post_attempt")
        )

        def identity_recheck(*_args, **_kwargs):
            events.append(next(identity_rechecks))

        fake_v274 = SimpleNamespace(
            V274Paths=lambda **kwargs: SimpleNamespace(**kwargs),
            execute_once=execute_once,
            durable_execution_stage=lambda _paths: {
                "execution_state": "pre_submit_verified",
                "preauthorization_consumed": True,
                "intent_reserved": True,
                "signed": True,
                "post_attempts": 0,
                "orders": "none",
                "submission_unknown": False,
            },
            canonical_bytes=horizon.canonical_bytes,
        )
        with patch.object(
            horizon,
            "_claim_execute_invocation",
            side_effect=claimed,
        ), patch.object(
            horizon,
            "_load_v275_bundle_for_execute",
            side_effect=loaded,
        ), patch.object(
            horizon,
            "_write_exclusive_json",
            side_effect=recording_write,
        ), patch.object(
            horizon,
            "_revalidate_execution_public_identity",
            side_effect=identity_recheck,
        ):
            result = horizon.execute_first_eligible(
                root=self.root,
                now_epoch_millis=lambda: START * 1000 + 245_000,
                require_root=False,
            )
        self.assertEqual(
            events,
            [
                "execute_invocation_claim",
                "full_verify",
                "identity_pre_attempt",
                "global_attempt_claim",
                "identity_post_attempt",
                "execute_once",
            ],
        )
        self.assertTrue(self.paths.execute_invocation.exists())
        self.assertTrue(self.paths.attempt.exists())
        self.assertEqual(result["retry_permitted"], False)

    def test_identity_replacement_before_attempt_never_creates_attempt(self) -> None:
        invocation = {
            "invoked_at_epoch_millis": START * 1000 + 244_000,
        }
        invocation_sha = horizon._write_exclusive_json(
            self.paths.execute_invocation,
            invocation,
        )
        fake_v274 = SimpleNamespace()
        loaded = (
            fake_v274,
            {},
            self.authorization_sha,
            self.paths,
            self.window,
            self.child,
            "c" * 64,
            self.permit,
            self.permit_sha,
        )
        with patch.object(
            horizon,
            "_claim_execute_invocation",
            return_value=(invocation, invocation_sha),
        ), patch.object(
            horizon,
            "_load_v275_bundle_for_execute",
            return_value=loaded,
        ), patch.object(
            horizon,
            "_revalidate_execution_public_identity",
            side_effect=horizon.HorizonRefusal(
                "installed_public_execution_identity_mismatch",
                "injected pre-attempt replacement",
            ),
        ):
            with self.assertRaises(horizon.HorizonRefusal):
                horizon.execute_first_eligible(
                    root=self.root,
                    now_epoch_millis=lambda: START * 1000 + 245_000,
                    require_root=False,
                )
        self.assertFalse(self.paths.attempt.exists())

    def test_identity_replacement_after_attempt_refuses_before_execute_once(self) -> None:
        invocation = {
            "invoked_at_epoch_millis": START * 1000 + 244_000,
        }
        invocation_sha = horizon._write_exclusive_json(
            self.paths.execute_invocation,
            invocation,
        )
        execute_once = Mock()
        fake_v274 = SimpleNamespace(
            V274Paths=lambda **kwargs: SimpleNamespace(**kwargs),
            execute_once=execute_once,
            durable_execution_stage=lambda _paths: {
                "execution_state": "pre_submit_verified",
                "preauthorization_consumed": True,
                "intent_reserved": False,
                "signed": False,
                "post_attempts": 0,
                "orders": "none",
                "submission_unknown": False,
            },
            canonical_bytes=horizon.canonical_bytes,
        )
        loaded = (
            fake_v274,
            {},
            self.authorization_sha,
            self.paths,
            self.window,
            self.child,
            "c" * 64,
            self.permit,
            self.permit_sha,
        )
        rechecks = iter(
            (
                None,
                horizon.HorizonRefusal(
                    "installed_public_execution_identity_mismatch",
                    "injected post-attempt replacement",
                ),
            )
        )

        def recheck(*_args, **_kwargs):
            outcome = next(rechecks)
            if outcome is not None:
                raise outcome

        with patch.object(
            horizon,
            "_claim_execute_invocation",
            return_value=(invocation, invocation_sha),
        ), patch.object(
            horizon,
            "_load_v275_bundle_for_execute",
            return_value=loaded,
        ), patch.object(
            horizon,
            "_revalidate_execution_public_identity",
            side_effect=recheck,
        ):
            wrapper = horizon.execute_first_eligible(
                root=self.root,
                now_epoch_millis=lambda: START * 1000 + 245_000,
                require_root=False,
            )
        self.assertTrue(self.paths.attempt.exists())
        self.assertEqual(
            wrapper["refusal"],
            "installed_public_execution_identity_mismatch",
        )
        execute_once.assert_not_called()
        terminal_status = horizon._durable_cli_failure_status(
            "execute-first-eligible",
            horizon.HorizonRefusal(
                wrapper["refusal"],
                "post-attempt identity replacement",
            ),
        )
        self.assertEqual(terminal_status["post_attempts"], 1)
        self.assertEqual(
            terminal_status["orders"],
            "one_fok_submission_unknown",
        )
        self.assertIs(terminal_status["submission_unknown"], True)

    def test_wrapper_write_failure_after_attempt_reports_unknown_one(self) -> None:
        original_write = horizon._write_exclusive_json

        def loaded(**_kwargs):
            return (
                fake_v274,
                {},
                self.authorization_sha,
                self.paths,
                self.window,
                self.child,
                "c" * 64,
                self.permit,
                self.permit_sha,
            )

        def selective_write(path, payload):
            if path == self.window.execute_wrapper_result:
                raise horizon.HorizonRefusal(
                    "wrapper_fsync_injected",
                    "injected wrapper failure",
                )
            return original_write(path, payload)

        fake_v274 = SimpleNamespace(
            V274Paths=lambda **kwargs: SimpleNamespace(**kwargs),
            execute_once=lambda **_kwargs: {"response": "cancelled_fok"},
            durable_execution_stage=lambda _paths: {
                "execution_state": "response_recorded",
                "preauthorization_consumed": True,
                "intent_reserved": True,
                "signed": True,
                "post_attempts": 1,
                "orders": "one_fok_attempt",
                "submission_unknown": False,
            },
            canonical_bytes=horizon.canonical_bytes,
        )
        invocation = {
            "invoked_at_epoch_millis": START * 1000 + 244_000,
        }
        invocation_sha = horizon._write_exclusive_json(
            self.paths.execute_invocation,
            invocation,
        )
        with patch.object(
            horizon,
            "_claim_execute_invocation",
            return_value=(invocation, invocation_sha),
        ), patch.object(
            horizon,
            "_load_v275_bundle_for_execute",
            side_effect=loaded,
        ), patch.object(
            horizon,
            "_write_exclusive_json",
            side_effect=selective_write,
        ):
            with self.assertRaises(horizon.HorizonRefusal) as failure:
                horizon.execute_first_eligible(
                    root=self.root,
                    now_epoch_millis=lambda: START * 1000 + 245_000,
                    require_root=False,
                )
        self.assertTrue(self.paths.attempt.exists())
        status = horizon._durable_cli_failure_status(
            "execute-first-eligible",
            failure.exception,
        )
        self.assertEqual(status["post_attempts"], 1)
        self.assertEqual(status["orders"], "one_fok_submission_unknown")

    def test_existing_attempt_prevents_second_execute(self) -> None:
        self.paths.run_dir.mkdir(parents=True)
        invocation = {
            "invoked_at_epoch_millis": START * 1000 + 244_000,
        }
        invocation_sha = horizon._write_exclusive_json(
            self.paths.execute_invocation,
            invocation,
        )
        horizon._write_exclusive_json(self.paths.attempt, {"existing": True})
        fake_v274 = SimpleNamespace()
        loaded = (
            fake_v274, {}, self.authorization_sha, self.paths, self.window,
            self.child, "c" * 64, self.permit, self.permit_sha,
        )
        with patch.object(
            horizon,
            "_claim_execute_invocation",
            return_value=(invocation, invocation_sha),
        ), patch.object(
            horizon,
            "_load_v275_bundle_for_execute",
            return_value=loaded,
        ):
            with self.assertRaises(horizon.HorizonRefusal):
                horizon.execute_first_eligible(
                    root=self.root,
                    now_epoch_millis=lambda: START * 1000 + 245_000,
                    require_root=False,
                )

    def test_execute_invocation_is_immutable_before_global_attempt(self) -> None:
        self.paths.run_dir.mkdir(parents=True)
        first_sha = horizon._write_exclusive_json(
            self.paths.execute_invocation,
            {
                "schema": horizon.EXECUTE_INVOCATION_SCHEMA,
                "execute_unit_invocation": 1,
            },
        )
        self.assertRegex(first_sha, r"^[0-9a-f]{64}$")
        self.assertFalse(self.paths.attempt.exists())
        with self.assertRaises(horizon.HorizonRefusal):
            horizon._write_exclusive_json(
                self.paths.execute_invocation,
                {
                    "schema": horizon.EXECUTE_INVOCATION_SCHEMA,
                    "execute_unit_invocation": 2,
                },
            )


class StaticIsolationTest(unittest.TestCase):
    def test_only_exact_root_ubuntu_01770_data_root_gets_ancestry_exception(self) -> None:
        accepted_gid = 1000

        def metadata_for(
            path: Path,
            *,
            data_mode: int = 0o1770,
            data_uid: int = 0,
            data_gid: int = accepted_gid,
        ):
            normalized = Path(path)
            return SimpleNamespace(
                st_mode=(
                    stat.S_IFDIR
                    | (
                        data_mode
                        if normalized == horizon.CANONICAL_DATA_DIR
                        else 0o755
                    )
                ),
                st_uid=(
                    data_uid
                    if normalized == horizon.CANONICAL_DATA_DIR
                    else 0
                ),
                st_gid=(
                    data_gid
                    if normalized == horizon.CANONICAL_DATA_DIR
                    else 0
                ),
            )

        target = horizon.CANONICAL_DATA_DIR / "canary" / "v275"
        with patch("os.lstat", side_effect=lambda path: metadata_for(path)), patch(
            "grp.getgrnam",
            return_value=SimpleNamespace(gr_gid=accepted_gid),
        ):
            horizon._require_trusted_directory_chain(target)
        for data_mode, data_uid, data_gid in (
            (0o770, 0, accepted_gid),
            (0o1770, 1000, accepted_gid),
            (0o1770, 0, accepted_gid + 1),
            (0o1777, 0, accepted_gid),
        ):
            with self.subTest(
                data_mode=oct(data_mode),
                data_uid=data_uid,
                data_gid=data_gid,
            ), patch(
                "os.lstat",
                side_effect=lambda path, mode=data_mode, uid=data_uid, gid=data_gid: metadata_for(
                    path,
                    data_mode=mode,
                    data_uid=uid,
                    data_gid=gid,
                ),
            ), patch(
                "grp.getgrnam",
                return_value=SimpleNamespace(gr_gid=accepted_gid),
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon._require_trusted_directory_chain(target)

    def test_coordinator_source_has_no_top_level_v274_or_broker_import(self) -> None:
        source = Path(horizon.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import one_window_canary_v274", source)
        self.assertNotIn("import live_broker", source)

    def test_only_execute_unit_loads_live_env(self) -> None:
        deploy = Path(horizon.__file__).with_name("deploy")
        provisioner = (deploy / "poly-bot-bounded-horizon-canary-v275-provision-identity.service").read_text()
        coordinator = (deploy / "poly-bot-bounded-horizon-canary-v275-coordinator.service").read_text()
        prepare = (deploy / "poly-bot-bounded-horizon-canary-v275-prepare.service").read_text()
        execute = (deploy / "poly-bot-bounded-horizon-canary-v275-execute.service").read_text()
        self.assertIn("InaccessiblePaths=/etc/poly-bot-btc5m/live.env", provisioner)
        self.assertIn("InaccessiblePaths=/etc/poly-bot-btc5m/live.env", coordinator)
        self.assertIn("InaccessiblePaths=/etc/poly-bot-btc5m/live.env", prepare)
        self.assertNotIn("EnvironmentFile=/etc/poly-bot-btc5m/live.env", provisioner)
        self.assertNotIn("EnvironmentFile=/etc/poly-bot-btc5m/live.env", coordinator)
        self.assertNotIn("EnvironmentFile=/etc/poly-bot-btc5m/live.env", prepare)
        self.assertIn("EnvironmentFile=/etc/poly-bot-btc5m/live.env", execute)
        self.assertIn("PrivateNetwork=true", provisioner)
        self.assertIn("PrivateNetwork=true", coordinator)
        self.assertIn("InaccessiblePaths=/opt/poly-bot-btc5m", provisioner)
        self.assertIn("InaccessiblePaths=/opt/poly-bot-btc5m", execute)
        self.assertIn("InaccessiblePaths=/opt/poly-bot-btc5m", coordinator)
        self.assertIn("InaccessiblePaths=/opt/poly-bot-btc5m", prepare)
        self.assertIn("CPUQuota=25%", coordinator)
        self.assertIn("RuntimeMaxSec=12h15min", coordinator)
        self.assertIn("Restart=on-failure", coordinator)
        self.assertIn("RestartSec=10s", coordinator)
        self.assertIn("StartLimitIntervalSec=120", coordinator)
        self.assertIn("StartLimitBurst=3", coordinator)
        self.assertIn("Restart=no", prepare)
        self.assertIn("Restart=no", execute)
        self.assertIn("TasksMax=32", execute)
        self.assertIn(
            "ReadWritePaths=/etc/poly-bot-btc5m/v275-public-identity",
            provisioner,
        )
        self.assertNotIn("ReadWritePaths=/etc/poly-bot-btc5m\n", provisioner)
        for unit_text in (provisioner, coordinator, prepare, execute):
            self.assertIn("PYTHONUSERBASE", unit_text)
            self.assertIn("PYTHONWARNINGS", unit_text)
            self.assertIn("PYTHONPLATLIBDIR", unit_text)
            self.assertIn("LD_AUDIT", unit_text)
            self.assertIn(
                "ReadOnlyPaths=/etc/poly-bot-btc5m/v274-conditional-preauthorization.json",
                unit_text,
            )
            self.assertIn(
                "ConditionPathExists=/etc/poly-bot-btc5m/v274-conditional-preauthorization.json",
                unit_text,
            )
        for unit_text in (coordinator, prepare, execute):
            self.assertIn(
                "ReadOnlyPaths=/etc/poly-bot-btc5m/v275-public-identity\n",
                unit_text,
            )
            self.assertIn(
                "ReadOnlyPaths=/etc/poly-bot-btc5m/v275-public-identity/public-execution-identity.json",
                unit_text,
            )

    def test_nonsecret_environment_is_exact_four_keys_without_comments(self) -> None:
        deploy = Path(horizon.__file__).with_name("deploy")
        example = (deploy / "v275-bounded-horizon.env.example").read_bytes()
        self.assertEqual(example, horizon.NONSECRET_ENVIRONMENT_BYTES)
        self.assertEqual(example.count(b"\n"), 4)
        self.assertNotIn(b"PYTHONPATH", example)
        self.assertNotIn(b"BTC5M_POLYMARKET_PRIVATE_KEY", example)

    def test_loader_and_credential_environment_are_fail_closed(self) -> None:
        base = dict(horizon.CONTRACT_ENVIRONMENT)
        for key in (
            "PYTHONPATH",
            "PYTHONUSERBASE",
            "PYTHONWARNINGS",
            "PYTHONPLATLIBDIR",
            "LD_AUDIT",
        ):
            with self.subTest(key=key), patch.dict(
                os.environ,
                {**base, key: "/tmp/substitute"},
                clear=True,
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_process_environment(credentials_permitted=True)
        with patch.dict(
            os.environ,
            {**base, "BTC5M_POLYMARKET_PRIVATE_KEY": "not-a-real-key"},
            clear=True,
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon._validate_process_environment(credentials_permitted=False)

    def test_execute_live_environment_identity_is_metadata_only_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "live.env"
            live.write_bytes(b"opaque-secret-bytes\n")
            live.chmod(0o600)
            horizon._validate_live_environment_identity(
                live,
                require_canonical_path=False,
                require_root_owned=False,
            )
            live.chmod(0o644)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_live_environment_identity(
                    live,
                    require_canonical_path=False,
                    require_root_owned=False,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "live.env"
            live.write_bytes(b"opaque-secret-bytes\n")
            live.chmod(0o600)
            os.link(live, root / "hardlink.env")
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_live_environment_identity(
                    live,
                    require_canonical_path=False,
                    require_root_owned=False,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.env"
            target.write_bytes(b"opaque-secret-bytes\n")
            target.chmod(0o600)
            live = root / "live.env"
            live.symlink_to(target)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._validate_live_environment_identity(
                    live,
                    require_canonical_path=False,
                    require_root_owned=False,
                )

    def test_artifact_symlink_hardlink_mode_and_directory_symlink_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.json"
            horizon._write_exclusive_json(original, {"value": 1})
            symbolic = root / "symbolic.json"
            symbolic.symlink_to(original)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon.read_canonical_object(
                    symbolic,
                    require_root_owned=False,
                )
            hard = root / "hard.json"
            os.link(original, hard)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon.read_canonical_object(
                    original,
                    require_root_owned=False,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writable = root / "writable.json"
            horizon._write_exclusive_json(writable, {"value": 2})
            writable.chmod(0o660)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon.read_canonical_object(
                    writable,
                    require_root_owned=False,
                )
            target = root / "target"
            target.mkdir()
            link = root / "directory-link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(horizon.HorizonRefusal):
                horizon._ensure_exact_directory(
                    link,
                    require_root_owned=False,
                )

    def _systemd_show_result(
        self,
        command,
        *,
        forged_drop_in: bool = False,
        forged_unset_environment: bool = False,
        omitted_property: str | None = None,
        forged_reverse_dependency: str | None = None,
        environment_file_lines_by_unit: (
            dict[str, tuple[str, ...]] | None
        ) = None,
        duplicate_property: str | None = None,
    ):
        unit = command[2]
        expected = horizon._expected_effective_systemd_units()[unit]
        formatted_timespans = {
            10_000_000: "10s",
            60_000_000: "1min",
            90_000_000: "1min 30s",
            120_000_000: "2min",
            480_000_000: "8min",
            44_100_000_000: "12h 15min",
            "infinity": "infinity",
        }
        properties = {
            "LoadState": "loaded",
            "UnitFileState": "static",
            "FragmentPath": str(horizon.CANONICAL_UNIT_DIRECTORY / unit),
            "DropInPaths": (
                f"/etc/systemd/system/{unit}.d/override.conf"
                if forged_drop_in and unit.endswith("coordinator.service")
                else ""
            ),
            "ExecStart": (
                "{ path="
                + expected["command"][0]
                + " ; argv[]="
                + " ".join(expected["command"])
                + " ; ignore_errors=no ; }"
            ),
            "Environment": " ".join(sorted(expected["environment"])),
            "UnsetEnvironment": " ".join(
                sorted(
                    set(expected["unset_environment"])
                    - (
                        {"LD_AUDIT"}
                        if forged_unset_environment
                        and unit.endswith("execute.service")
                        else set()
                    )
                )
            ),
            "User": expected["user"],
            "Group": expected["group"],
            "Type": expected["type"],
            "PrivateNetwork": expected["private_network"],
            "Restart": expected["restart"],
            "RestartUSec": formatted_timespans.get(
                expected.get("restart_usec"),
                "100ms",
            ),
            "StartLimitIntervalUSec": formatted_timespans.get(
                expected.get("start_limit_interval_usec"),
                "10s",
            ),
            "StartLimitBurst": expected.get("start_limit_burst", "5"),
            "NoNewPrivileges": expected["no_new_privileges"],
            "CapabilityBoundingSet": expected["capability_bounding_set"],
            "ProtectSystem": expected["protect_system"],
            "ProtectHome": expected["protect_home"],
            "PrivateDevices": expected["private_devices"],
            "PrivateTmp": expected["private_tmp"],
            "UMask": expected["umask"],
            "TimeoutStartUSec": formatted_timespans[
                expected["timeout_start_usec"]
            ],
            "RuntimeMaxUSec": formatted_timespans[
                expected["runtime_max_usec"]
            ],
            "InaccessiblePaths": " ".join(
                sorted(expected["inaccessible_paths"])
            ),
            "ReadOnlyPaths": " ".join(sorted(expected["read_only_paths"])),
            "ReadWritePaths": " ".join(
                sorted(expected["read_write_paths"])
            ),
            "RestrictAddressFamilies": " ".join(
                sorted(expected["address_families"])
            ),
            "TriggeredBy": "",
            "Triggers": "",
            "WantedBy": "",
            "RequiredBy": "",
            "UpheldBy": "",
            "BoundBy": "",
            "OnFailureOf": "",
        }
        environment_file_lines = [
            f"{path} (ignore_errors=no)"
            for path in expected["environment_files"]
        ]
        if (
            environment_file_lines_by_unit is not None
            and unit in environment_file_lines_by_unit
        ):
            environment_file_lines = list(
                environment_file_lines_by_unit[unit]
            )
        if (
            forged_reverse_dependency is not None
            and unit.endswith("coordinator.service")
        ):
            properties[forged_reverse_dependency] = "rogue.service"
        output_lines: list[str] = []
        for name in horizon.EFFECTIVE_SYSTEMD_PROPERTIES:
            if name == omitted_property:
                continue
            if name == "EnvironmentFiles":
                output_lines.extend(
                    f"EnvironmentFiles={raw}\n"
                    for raw in environment_file_lines
                )
                continue
            output_lines.append(f"{name}={properties[name]}\n")
            if name == duplicate_property:
                output_lines.append(f"{name}={properties[name]}\n")
        stdout = "".join(output_lines).encode("utf-8")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    def test_effective_loaded_units_reject_dropins_and_bind_normalized_sha(self) -> None:
        self.assertEqual(len(horizon.EFFECTIVE_SYSTEMD_PROPERTIES), 36)
        self.assertEqual(
            horizon.EFFECTIVE_SYSTEMD_PROPERTIES.count("EnvironmentFiles"),
            1,
        )
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command
            ),
        ):
            contract = horizon.effective_systemd_contract()
        self.assertRegex(horizon.canonical_sha256(contract), r"^[0-9a-f]{64}$")
        coordinator = contract["units"][
            "poly-bot-bounded-horizon-canary-v275-coordinator.service"
        ]
        self.assertEqual(coordinator["restart"], "on-failure")
        self.assertEqual(coordinator["restart_usec"], 10_000_000)
        self.assertEqual(coordinator["start_limit_interval_usec"], 120_000_000)
        self.assertEqual(coordinator["start_limit_burst"], "3")
        self.assertEqual(coordinator["wanted_by"], "")
        self.assertEqual(coordinator["required_by"], "")
        self.assertEqual(coordinator["upheld_by"], "")
        self.assertEqual(coordinator["bound_by"], "")
        self.assertEqual(coordinator["on_failure_of"], "")
        execute_unit = (
            "poly-bot-bounded-horizon-canary-v275-execute.service"
        )
        self.assertEqual(
            contract["units"][execute_unit]["environment_files"],
            horizon._expected_effective_systemd_units()[execute_unit][
                "environment_files"
            ],
        )
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command,
                forged_drop_in=True,
            ),
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon.effective_systemd_contract()
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command,
                forged_unset_environment=True,
            ),
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon.effective_systemd_contract()

    def test_effective_systemd_normalizes_only_omitted_empty_environment_files(
        self,
    ) -> None:
        provisioner_unit = (
            "poly-bot-bounded-horizon-canary-v275-provision-identity.service"
        )
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command
            ),
        ):
            contract = horizon.effective_systemd_contract()
        provisioner = contract["units"][provisioner_unit]
        self.assertEqual(provisioner["environment_files"], [])

        for raw_value in ("", "garbage"):
            with self.subTest(raw_value=raw_value), patch.object(
                horizon.subprocess,
                "run",
                side_effect=lambda command, **_kwargs: self._systemd_show_result(
                    command,
                    environment_file_lines_by_unit={
                        provisioner_unit: (raw_value,)
                    },
                ),
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon.effective_systemd_contract()

        # The sole EnvironmentFiles omission is compatible.  The same
        # omission combined with any other missing property is not.
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command,
                omitted_property="PrivateNetwork",
            ),
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon.effective_systemd_contract()
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command,
                omitted_property="EnvironmentFiles",
            ),
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon.effective_systemd_contract()

    def test_effective_systemd_environment_file_lines_are_exact_and_ordered(
        self,
    ) -> None:
        expected_units = horizon._expected_effective_systemd_units()
        coordinator_unit = (
            "poly-bot-bounded-horizon-canary-v275-coordinator.service"
        )
        execute_unit = (
            "poly-bot-bounded-horizon-canary-v275-execute.service"
        )
        coordinator_line = (
            f"{expected_units[coordinator_unit]['environment_files'][0]} "
            "(ignore_errors=no)"
        )
        execute_lines = tuple(
            f"{path} (ignore_errors=no)"
            for path in expected_units[execute_unit]["environment_files"]
        )
        attacks = (
            (
                "relative_path",
                {coordinator_unit: ("relative.env (ignore_errors=no)",)},
            ),
            (
                "ignore_errors_yes",
                {
                    coordinator_unit: (
                        coordinator_line.replace(
                            "ignore_errors=no",
                            "ignore_errors=yes",
                        ),
                    )
                },
            ),
            ("blank_line", {coordinator_unit: ("",)}),
            (
                "extra_line",
                {
                    coordinator_unit: (
                        coordinator_line,
                        "/etc/poly-bot-btc5m/extra.env (ignore_errors=no)",
                    )
                },
            ),
            (
                "missing_execute_line",
                {execute_unit: (execute_lines[0],)},
            ),
            (
                "duplicate_wrong_execute_path",
                {execute_unit: (execute_lines[0], execute_lines[0])},
            ),
            (
                "wrong_execute_order",
                {execute_unit: tuple(reversed(execute_lines))},
            ),
        )
        for attack_name, overrides in attacks:
            with self.subTest(attack_name=attack_name), patch.object(
                horizon.subprocess,
                "run",
                side_effect=lambda command, **_kwargs: self._systemd_show_result(
                    command,
                    environment_file_lines_by_unit=overrides,
                ),
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon.effective_systemd_contract()

    def test_effective_systemd_rejects_duplicate_non_environment_property(
        self,
    ) -> None:
        with patch.object(
            horizon.subprocess,
            "run",
            side_effect=lambda command, **_kwargs: self._systemd_show_result(
                command,
                duplicate_property="PrivateNetwork",
            ),
        ), self.assertRaises(horizon.HorizonRefusal):
            horizon.effective_systemd_contract()

    def test_effective_systemd_rejects_nonempty_reverse_dependencies(self) -> None:
        for property_name in (
            "WantedBy",
            "RequiredBy",
            "UpheldBy",
            "BoundBy",
            "OnFailureOf",
        ):
            with self.subTest(property_name=property_name), patch.object(
                horizon.subprocess,
                "run",
                side_effect=lambda command, **_kwargs: self._systemd_show_result(
                    command,
                    forged_reverse_dependency=property_name,
                ),
            ), self.assertRaises(horizon.HorizonRefusal):
                horizon.effective_systemd_contract()


if __name__ == "__main__":
    unittest.main()
