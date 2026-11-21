"""Checks for single-parameter EBS treatments, checkpointing and readiness."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

from evaluation.sql.postgres_volume import status, submit


class VolumeTreatmentTests(unittest.TestCase):
    def volume(self, throughput=125):
        return dict(VolumeId="vol-owned", VolumeType="gp3", Size=64, Iops=3000,
                    Throughput=throughput)

    def client(self):
        ec2 = Mock()
        ec2.describe_volumes_modifications.return_value = {"VolumesModifications": []}
        ec2.modify_volume.return_value = {"VolumeModification": {"ModificationState": "modifying"}}
        return ec2

    def test_records_before_sending_and_repeated_apply_does_not_resubmit(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, ec2 = Path(temp), self.client()
            def accepted(**kwargs):
                saved = json.loads((folder / "apply-request.json").read_text())
                self.assertEqual(saved["before"]["Throughput"], 125)
                self.assertEqual(kwargs, {"VolumeId": "vol-owned", "Throughput": 500})
                return {"VolumeModification": {"ModificationState": "modifying"}}
            ec2.modify_volume.side_effect = accepted
            submit(ec2, self.volume(), folder, 500)
            submit(ec2, self.volume(500), folder, 500)
            ec2.modify_volume.assert_called_once()

    def test_ambiguous_failure_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, ec2 = Path(temp), self.client()
            ec2.modify_volume.side_effect = TimeoutError("unknown server outcome")
            with self.assertRaises(TimeoutError):
                submit(ec2, self.volume(), folder, 500)
            submit(ec2, self.volume(), folder, 500)
            ec2.modify_volume.assert_called_once()

    def test_never_modified_volume_can_submit_first_treatment(self):
        with tempfile.TemporaryDirectory() as temp:
            ec2 = self.client()
            ec2.describe_volumes_modifications.side_effect = ClientError(
                {"Error": {"Code": "InvalidVolumeModification.NotFound",
                           "Message": "Modification does not exist"}},
                "DescribeVolumesModifications")
            submit(ec2, self.volume(), Path(temp), 500)
            ec2.modify_volume.assert_called_once_with(VolumeId="vol-owned", Throughput=500)

    def test_unrelated_history_errors_stop_before_recording_or_mutating(self):
        for code in ("UnauthorizedOperation", "AccessDenied", "InvalidVolume.NotFound"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                folder, ec2 = Path(temp), self.client()
                ec2.describe_volumes_modifications.side_effect = ClientError(
                    {"Error": {"Code": code, "Message": "stop"}},
                    "DescribeVolumesModifications")
                with self.assertRaises(ClientError):
                    submit(ec2, self.volume(), folder, 500)
                self.assertFalse((folder / "apply-request.json").exists())
                ec2.modify_volume.assert_not_called()

    def test_optimizing_is_not_ready_even_if_visible_throughput_matches(self):
        ec2 = self.client()
        ec2.describe_volumes.return_value = {"Volumes": [self.volume(500)]}
        for state, ready in (("optimizing", False), ("completed", True)):
            ec2.describe_volumes_modifications.return_value = {"VolumesModifications": [
                {"ModificationState": state, "TargetThroughput": 500}]}
            self.assertEqual(status(ec2, "vol-owned", 500)["ready_for_measurement"], ready)

    def test_wrong_baseline_or_excessive_target_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            ec2 = self.client()
            for volume, target in ((self.volume(250), 500), (self.volume(), 1000)):
                with self.assertRaises(ValueError):
                    submit(ec2, volume, Path(temp), target)
            ec2.modify_volume.assert_not_called()

    def test_restore_targets_original_throughput_without_resizing(self):
        with tempfile.TemporaryDirectory() as temp:
            ec2 = self.client()
            ec2.describe_volumes_modifications.return_value = {"VolumesModifications": [
                {"ModificationState": "completed", "TargetThroughput": 500}]}
            submit(ec2, self.volume(500), Path(temp), 125, action="restore", expected=500)
            ec2.modify_volume.assert_called_once_with(VolumeId="vol-owned", Throughput=125)


if __name__ == "__main__":
    unittest.main()
