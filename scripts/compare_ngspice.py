"""Compare a saved QSPICE AC run with ngspice in WSL using the same netlists."""

import argparse
from bisect import bisect_left
import csv
import math
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def ngspice_deck(deck, channel, output="/dev/stdout"):
    body, end = deck.read_text(encoding="ascii").rstrip().rsplit("\n", 1)
    if end.strip().lower() != ".end":
        raise ValueError(f"{deck}: missing final .end")
    # Purely capacitive internal nodes have no DC path in the KiCad deck.
    return (
        f"{body}\n.options rshunt=1e12\n.control\nrun\noption numdgt=15\n"
        f"wrdata {output} v(line_{channel})\nquit\n.endc\n.end\n"
    )


def read_ngspice_table(output):
    points = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            frequency, real, imaginary = map(float, fields)
        except ValueError:
            continue
        voltage = complex(real, imaginary)
        if frequency <= 0 or not math.isfinite(frequency) or not 0 < abs(voltage) < math.inf:
            raise ValueError("Invalid ngspice AC sample")
        if points and frequency <= points[-1][0]:
            raise ValueError("ngspice AC frequencies must be strictly increasing")
        points.append((frequency, 20 * math.log10(abs(voltage)),
                       math.degrees(math.atan2(imaginary, real))))
    if len(points) < 2:
        raise ValueError(f"No ngspice AC table received:\n{output[-1500:]}")
    return points


def interpolate(points, frequencies, frequency):
    index = bisect_left(frequencies, frequency)
    for nearby in (index, index - 1):
        if 0 <= nearby < len(points) and math.isclose(
            frequency, frequencies[nearby], rel_tol=1e-10
        ):
            return points[nearby][1:]
    if not 0 < index < len(points):
        raise ValueError(f"{frequency:g} Hz falls outside the ngspice sweep")
    low, gain, phase = points[index - 1]
    high, next_gain, next_phase = points[index]
    weight = math.log(frequency / low) / math.log(high / low)
    return gain + weight * (next_gain - gain), phase + weight * math.remainder(next_phase - phase, 360)


def compare(qspice_csv, points):
    frequencies = [sample[0] for sample in points]
    rows = []
    with qspice_csv.open(newline="", encoding="ascii") as stream:
        for sample in csv.DictReader(stream):
            f = float(sample["frequency_hz"])
            q_gain, q_phase = float(sample["gain_db"]), float(sample["phase_deg"])
            gain, phase = interpolate(points, frequencies, f)
            rows.append((f, q_gain, gain, gain - q_gain, q_phase, phase,
                         math.remainder(phase - q_phase, 360)))
    if not rows:
        raise ValueError(f"No QSPICE samples in {qspice_csv}")
    return rows


def latest_run():
    runs = []
    for path in (ROOT / "build/qspice").glob("run-*"):
        try:
            if all((path / f"preamp_{ch}.{ext}").is_file()
                   for ch in ("l", "r") for ext in ("cir", "csv")):
                runs.append((path.stat().st_mtime, path))
        except PermissionError:
            continue
    return max(runs)[1] if runs else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", type=Path, help="QSPICE run directory (default: latest)")
    parser.add_argument("--distro", default="Ubuntu-24.04")
    args = parser.parse_args()
    run = args.run or latest_run()
    if run is None:
        parser.error("No QSPICE run found. Run scripts/simulate_qspice.py first.")

    comparisons = {}
    try:
        for channel in ("l", "r"):
            process = subprocess.run(
                ["wsl.exe", "-d", args.distro, "--exec", "ngspice", "-n", "-b"],
                input=ngspice_deck(run / f"preamp_{channel}.cir", channel).encode("ascii"),
                capture_output=True, timeout=60,
            )
            if process.returncode:
                message = process.stderr or process.stdout
                encoding = "utf-16-le" if b"\0" in message else "utf-8"
                parser.exit(1, f"ngspice/WSL exited with {process.returncode}: "
                            f"{message.decode(encoding, errors='replace').strip(chr(0xfeff)).strip()}\n")
            points = read_ngspice_table(process.stdout.decode("utf-8", errors="replace"))
            comparisons[channel] = compare(run / f"preamp_{channel}.csv", points)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")

    for channel, rows in comparisons.items():
        path = run / f"ngspice_compare_{channel}.csv"
        with path.open("w", newline="", encoding="ascii") as stream:
            writer = csv.writer(stream)
            writer.writerow(["frequency_hz", "qspice_gain_db", "ngspice_gain_db", "delta_gain_db",
                             "qspice_phase_deg", "ngspice_phase_deg", "delta_phase_deg"])
            writer.writerows(rows)
        print(f"{channel.upper()}: {len(rows)} points, "
              f"max |delta gain| = {max(abs(row[3]) for row in rows):.6g} dB, "
              f"max |delta phase| = {max(abs(row[6]) for row in rows):.6g} deg")
        print(path.resolve())


if __name__ == "__main__":
    main()
