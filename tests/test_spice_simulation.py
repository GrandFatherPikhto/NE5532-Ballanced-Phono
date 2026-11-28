"""Configuration, cross-platform execution and numerical AC regression checks."""

import contextlib
import io
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import simulate_qspice
from ngspice_common import NgspiceRunner, make_runner, ngspice_deck, read_ac_table
from spice_common import load_config, load_plot, netlist, write_csv, write_report

CONFIG = """[analysis]
frequency_min_hz = 10.0
frequency_max_hz = 10000.0
reference_frequency_hz = 1000.0
points = 100
external_load_ohm = 47000.0
[opamp]
open_loop_gain = 100000.0
gain_bandwidth_hz = 12000000.0
[riaa]
low_frequency_pole_s = 0.003180
zero_s = 0.000318
high_frequency_pole_s = 0.000075
"""


class SimulationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="spice-test-", dir=ROOT / "build")
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def config(self, extra=""):
        path = self.directory / "settings.toml"
        path.write_text(CONFIG + extra)
        return path

    def test_ngspice_overrides_do_not_use_qspice_overrides(self):
        path = self.config("""[qspice]
external_load_ohm = 1000.0
[ngspice]
external_load_ohm = 22000.0
reference_frequency_hz = 500.0
write_plots = false
""")
        cfg = load_config(path, "ngspice")
        self.assertEqual(cfg["analysis"]["external_load_ohm"], 22000)
        self.assertEqual(cfg["analysis"]["reference_frequency_hz"], 500)
        self.assertIsNone(load_plot(cfg["ngspice"]))
        self.assertEqual(load_config(path, "qspice")["analysis"]["external_load_ohm"], 1000)

    def test_rejects_invalid_settings_before_running(self):
        for setting in ("points = true", "points = 0", "write_plots = 1",
                        "frequency_min_hz = 10001", "reference_frequency_hz = 1",
                        "external_load_ohm = nan"):
            with self.subTest(setting=setting):
                with self.assertRaises(ValueError):
                    load_config(self.config("[ngspice]\n" + setting + "\n"), "ngspice")

    def test_parser_preserves_complex_samples_and_rejects_bad_tables(self):
        samples = read_ac_table("ngspice banner\n1 3 4\n10 -2 -1\n")
        self.assertEqual(samples, [(1., 3 + 4j), (10., -2 - 1j)])
        for text in ("1 1 0\n", "1 1 0\n1 2 0\n", "1 nan 0\n2 1 0\n",
                     "1 0 0\n2 1 0\n", "-1 1 0\n2 1 0\n"):
            with self.subTest(table=text), self.assertRaises(ValueError):
                read_ac_table(text)

    def test_wsl_transports_results_without_windows_paths_in_deck(self):
        runner = NgspiceRunner(("wsl.exe", "-d", "Test", "--exec", "ngspice"), "wsl", 12, 1e12)
        response = subprocess.CompletedProcess(runner.command, 0,
                                               stdout=b"banner\n10 1 -1\n100 2 -3\n", stderr=b"")
        with patch("ngspice_common.subprocess.run", return_value=response) as run:
            samples = runner.run("test\n.end\n", "l", self.directory)
        self.assertIn(b"wrdata /dev/stdout", run.call_args.kwargs["input"])
        self.assertNotIn(str(self.directory).encode(), run.call_args.kwargs["input"])
        self.assertEqual(samples[-1], (100, 2 - 3j))
        self.assertEqual(read_ac_table((self.directory / "preamp_l.dat").read_text()), samples)

    def test_backend_selection_and_cli_precedence(self):
        options = {"backend": "wsl", "wsl_distro": "Default", "ngspice": "configured-ng"}
        with patch("ngspice_common.find_program", return_value="wsl.exe"):
            runner = make_runner(options, ngspice="override-ng", distro="Override")
        self.assertIn("override-ng", runner.command)
        self.assertIn("Override", runner.command)
        for options in ({"backend": "bad"}, {"timeout_s": 0}, {"rshunt_ohm": float("nan")}):
            with self.assertRaises(ValueError):
                make_runner(options)

    def test_failure_writes_log_instead_of_accepting_old_results(self):
        runner = NgspiceRunner(("ngspice",), "native", 1, 1e12)
        table = self.directory / "preamp_l.dat"
        table.write_text("10 1 0\n100 1 0\n")
        failed = subprocess.CompletedProcess(runner.command, 1, b"", b"circuit error")
        with patch("ngspice_common.subprocess.run", return_value=failed), self.assertRaises(RuntimeError):
            runner.run("test\n.end\n", "l", self.directory)
        self.assertFalse(table.exists())
        self.assertIn("circuit error", (self.directory / "preamp_l.log").read_text())

    def test_comparison_does_not_overwrite_original_circuit(self):
        original = self.directory / "preamp_l.cir"
        original.write_text("original\n.end\n")
        runner = NgspiceRunner(("wsl.exe",), "wsl", 1, 1e12)
        response = subprocess.CompletedProcess(runner.command, 0, b"10 1 0\n100 1 0\n", b"")
        with patch("ngspice_common.subprocess.run", return_value=response):
            runner.run(original, "l", self.directory, stem="ngspice_compare_l")
        self.assertEqual(original.read_text(), "original\n.end\n")
        self.assertTrue((self.directory / "ngspice_compare_l.cir").is_file())

    def test_report_names_engine_and_normalizes_at_configured_reference(self):
        cfg = load_config(self.config("[ngspice]\nwrite_plots = false\n"), "ngspice")
        with contextlib.redirect_stdout(io.StringIO()):
            result = write_csv(self.directory / "preamp_l.csv",
                               [(10, 1+0j), (1000, 2+0j), (10000, 3+0j)], cfg)
        self.assertEqual(result[-1], 2)
        self.assertAlmostEqual(result[3][1], 0)
        write_report(self.directory, cfg, {"l": result}, "ngspice", "ngspice")
        text = (self.directory / "report.md").read_text()
        self.assertIn("ngspice", text)
        self.assertNotIn("response.png", text)
        self.assertIn("THD", text)

    @unittest.skipUnless(shutil.which("ngspice"), "native ngspice is not installed")
    def test_native_rc_response_matches_analytic_solution(self):
        deck = ("RC lowpass\nVin input 0 AC 1\nR1 input line_l 1000\n"
                "C1 line_l 0 1u\n.ac dec 10 10 10000\n.end\n")
        runner = make_runner({"backend": "native"})
        samples = runner.run(deck, "l", self.directory)
        self.assertGreater(len(samples), 20)
        for f, value in samples:
            expected = 1 / (1 + 2j * math.pi * f * .001)
            self.assertLess(abs(value / expected - 1), 1e-7)

    def test_qspice_command_still_reaches_plotting_and_reporting(self):
        # Exercise the original CLI, including its parser and plot call, without
        # requiring a Windows executable. The raw data emulate QSPICE's format.
        cfg = self.config('[qspice]\nwrite_plots = true\noutput_directory = "' +
                          self.directory.as_posix() + '/qspice"\n')
        xml = ('<export><nets><net code="1" name="GND"/>'
               '<net code="2" name="in_lp"><node ref="J1" pin="1"/></net>'
               '<net code="3" name="in_ln"><node ref="J1" pin="3"/></net>'
               '<net code="4" name="in_rp"><node ref="J1" pin="5"/></net>'
               '<net code="5" name="in_rn"><node ref="J1" pin="7"/></net>'
               '<net code="6" name="out_l"><node ref="J2" pin="2"/></net>'
               '<net code="7" name="out_r"><node ref="J2" pin="4"/></net>'
               '</nets><components/></export>')
        raw = ("No. Points: 2\nFlags: complex\nVariables:\n"
               "0 frequency frequency\n1 v(line_l) voltage\n2 v(line_r) voltage\n"
               "Values:\n0 10\n1,0\n1,0\n1 10000\n2,0\n2,0\n")
        def fake_run(command, **kwargs):
            if "sch" in command:
                Path(command[command.index("-o")+1]).write_text(xml)
            else:
                (kwargs["cwd"] / command[command.index("-r")+1]).write_text(raw)
        with patch.object(sys, "argv", ["simulate_qspice.py", "--config", str(cfg)]), \
             patch.object(simulate_qspice, "executable", return_value=Path("fake-program")), \
             patch.object(simulate_qspice, "load_plot", return_value=object()), \
             patch.object(simulate_qspice.subprocess, "run", side_effect=fake_run), \
             patch.object(simulate_qspice, "plot_response") as plot, \
             contextlib.redirect_stdout(io.StringIO()):
            simulate_qspice.main()
        self.assertEqual(plot.call_args.args[2], 1000)
        self.assertTrue(next((self.directory / "qspice").glob("run-*/report.md")).is_file())


if __name__ == "__main__":
    unittest.main()
