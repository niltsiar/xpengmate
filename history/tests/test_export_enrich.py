import unittest

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


if __name__ == "__main__":
    unittest.main()
