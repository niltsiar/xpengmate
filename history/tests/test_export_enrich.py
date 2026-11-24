import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone

import export_enrich


def samples(*timers):
    return iter([(timer, {"odometer_km": float(timer)}) for timer in timers])


def pairs(positions, stream):
    return [(position_id, sample[0]) for position_id, sample in export_enrich.nearest_samples(positions, stream)]


class NearestSampleTest(unittest.TestCase):
    def test_position_takes_the_sample_at_its_own_second(self):
        self.assertEqual(pairs([(1, 1000)], samples(*range(995, 1006))), [(1, 1000)])

    def test_position_between_samples_takes_the_closer_one(self):
        self.assertEqual(pairs([(1, 1000)], samples(997, 1002)), [(1, 1002)])
        self.assertEqual(pairs([(1, 1000)], samples(999, 1004)), [(1, 999)])

    def test_position_with_no_sample_within_five_seconds_is_left_out(self):
        self.assertEqual(pairs([(1, 1000), (2, 2000)], samples(994, 1006, 2003)), [(2, 2003)])

    def test_positions_in_one_gap_each_take_their_own_nearest_sample(self):
        self.assertEqual(
            pairs([(1, 1000), (2, 1004), (3, 1009)], samples(999, 1005, 1010)),
            [(1, 999), (2, 1005), (3, 1010)],
        )


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def export_zip(directory, name, rows):
    """A minimal export with (timer, odometer, soc, range) rows."""
    path = os.path.join(directory, name)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "car_driving_operation_di.csv",
            "timer,cdcu_totalodometer\n" + "".join(f"{t},{odo}\n" for t, odo, _soc, _rng in rows),
        )
        archive.writestr(
            "car_driving_power_energy_di.csv",
            "timer,ldcu_bms_soc_disp,ldcu_dstbatdisp_dynamic\n"
            + "".join(f"{t},{soc},{rng}\n" for t, _odo, soc, rng in rows),
        )
    return path


class SamplesAtTest(unittest.TestCase):
    def test_each_moment_takes_its_nearest_sample_from_the_first_zip_that_has_one(self):
        first_moment = utc(1000)
        second_moment = utc(5000)
        missing = utc(9000)
        with tempfile.TemporaryDirectory() as directory:
            older = export_zip(directory, "older.zip", [(999, 100.04, 80, 300.0), (1001, 100.1, 79, 299.0)])
            newer = export_zip(directory, "newer.zip", [(1000, 555.0, 1, 1.0), (5002, 120.0, 70, 250.555)])

            found = export_enrich.samples_at([older, newer], [second_moment, first_moment, missing])

        self.assertEqual(found, {first_moment: (80, 100.0, 300.0), second_moment: (70, 120.0, 250.56)})



class IterSamplesTest(unittest.TestCase):
    def test_a_day_block_out_of_place_in_one_dataset_still_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "export.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "car_driving_operation_di.csv",
                    "timer,cdcu_totalodometer\n3,12.0\n4,13.0\n1,10.0\n2,11.0\n",
                )
                archive.writestr(
                    "car_driving_power_energy_di.csv",
                    "timer,ldcu_bms_soc_disp,ldcu_dstbatdisp_dynamic\n1,80,300\n2,79,299\n3,78,298\n4,77,\n",
                )

            joined = [(timer, sample["odometer_km"], sample["soc"], sample["range_km"]) for timer, sample in export_enrich.iter_samples(path)]

        self.assertEqual(joined, [(1, 10.0, 80.0, 300.0), (2, 11.0, 79.0, 299.0), (3, 12.0, 78.0, 298.0), (4, 13.0, 77.0, None)])


if __name__ == "__main__":
    unittest.main()
