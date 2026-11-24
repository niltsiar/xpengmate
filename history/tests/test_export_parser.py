import os
import tempfile
import unittest
import zipfile

import export_parser


class ParseZipTest(unittest.TestCase):
    def test_a_day_block_out_of_place_in_one_dataset_keeps_its_drive_whole(self):
        def trip(start):
            """Two minutes at 60 km/h, then parked long enough to end the drive."""
            return [(t, 60) for t in range(start, start + 121)] + [(t, 0) for t in range(start + 140, start + 541, 20)]

        earlier, later = trip(10000), trip(20000)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "export.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "car_driving_operation_di.csv",
                    "vin,timer,esp_vehspd,cdcu_totalodometer\n"
                    + "".join(f"TESTVIN,{t},{speed},{5000 + (t - 10000) / 60:.1f}\n" for t, speed in [*later, *earlier]),
                )
                archive.writestr(
                    "car_driving_power_energy_di.csv",
                    "vin,timer,ldcu_bms_soc_disp,ldcu_dstbatdisp_dynamic,bms_battvolt,bms_battcurr\n"
                    + "".join(f"TESTVIN,{t},80,300,400,50\n" for t, _speed in earlier)
                    + "".join(f"TESTVIN,{t},70,250,400,50\n" for t, _speed in later),
                )

            data = export_parser.parse_zip(path)

        self.assertEqual(data.vin, "TESTVIN")
        self.assertEqual(
            [(drive.start, drive.end, drive.soc_start, drive.range_start) for drive in data.drives],
            [(10000, 10120, 80, 300.0), (20000, 20120, 70, 250.0)],
        )


if __name__ == "__main__":
    unittest.main()
