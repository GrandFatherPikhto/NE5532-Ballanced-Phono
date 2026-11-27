"""Run small-signal AC analysis of the saved stereo KiCad schematic in QSPICE."""

import argparse
import bisect
import csv
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


def executable(explicit, name, candidates):
    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise FileNotFoundError(explicit)
    found = shutil.which(name)
    for path in ([Path(found)] if found else []) + candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"{name}: specify its path with --{name.split('.')[0].lower()}")


def passive_value(ref, value):
    if ref.startswith("R"):
        embedded = re.fullmatch(r"(\d+)([RKM])(\d*)", value, re.I)
        if embedded:
            whole, unit, fraction = embedded.groups()
            return float(f"{whole}.{fraction}") * {"R": 1, "K": 1e3, "M": 1e6}[unit.upper()]
        return float(value)
    capacitor = re.fullmatch(r"([\d.]+)\s*([pnuµμ]?)F(?:\s+\d+\s*V)?", value, re.I)
    if capacitor:
        number, unit = capacitor.groups()
        return float(number) * {"p": 1e-12, "n": 1e-9, "u": 1e-6,
                                "µ": 1e-6, "μ": 1e-6, "": 1}[unit.lower()]
    marking = re.fullmatch(r"\d+V(\d\d)(\d)[A-Z]", value)
    if marking:
        return int(marking[1]) * 10 ** (int(marking[2]) - 12)
    raise ValueError(f"{ref}: unsupported capacitance {value!r}")


def netlist(xml, cfg, excited):
    circuit = ElementTree.parse(xml)
    pins = {}
    for net in circuit.findall("nets/net"):
        name = net.get("name")
        node = {"GND": "0", "+12V": "vcc", "-12V": "vee"}.get(name, f"n{net.get('code')}")
        for pin in net.findall("node"):
            pins[pin.get("ref"), int(pin.get("pin"))] = node

    lines = ["PreAmp stereo small-signal AC", "VCC vcc 0 12", "VEE vee 0 -12"]
    load = cfg["analysis"]["external_load_ohm"]
    for channel, plus, minus, output in (("l", 1, 3, 2), ("r", 5, 7, 4)):
        level = 0.5 if channel == excited else 0
        lines += [
            f"Vinp_{channel} {pins['J1', plus]} 0 DC 0 AC {level}",
            f"Vinn_{channel} {pins['J1', minus]} 0 DC 0 AC {level} 180",
            f"Vsense_{channel} {pins['J2', output]} line_{channel} 0",
            f"Rload_{channel} line_{channel} 0 {load}",
        ]

    for component in circuit.findall("components/comp"):
        ref = component.get("ref")
        value = component.findtext("value")
        properties = {prop.get("name") for prop in component.findall("property")}
        if properties & {"dnp", "exclude_from_sim"}:
            continue
        if ref.startswith(("R", "C")):
            first, second = pins[ref, 1], pins[ref, 2]
            lines.append(f"{ref} {first} {second} {passive_value(ref, value):.12g}")
        elif ref.startswith("U"):
            if value != "NE5532":
                raise ValueError(f"{ref}: unsupported amplifier {value!r}")
            for unit, numbers in (("a", (3, 2, 1)), ("b", (5, 6, 7))):
                nodes = " ".join(pins[ref, number] for number in numbers)
                lines.append(f"X{ref}{unit} {nodes} opamp_one_pole")
        elif not ref.startswith(("J", "H", "D")):
            raise ValueError(f"{ref}: unsupported component {value!r}")

    model = cfg["opamp"]
    gain = model["open_loop_gain"]
    capacitance = gain / (2 * math.pi * model["gain_bandwidth_hz"] * 1000)
    analysis = cfg["analysis"]
    start, stop = analysis["frequency_min_hz"], analysis["frequency_max_hz"]
    points_per_decade = math.ceil(analysis["points"] / math.log10(stop / start))
    lines += [
        "* Linear teaching model, independent of supply rails.",
        ".subckt opamp_one_pole vp vn out",
        f"Eopen drive 0 vp vn {gain}",
        "Rpole drive pole 1k",
        f"Cpole pole 0 {capacitance:.15g}",
        "Eout out 0 pole 0 1",
        ".ends",
        ".save V(line_l) V(line_r)",
        f".ac dec {points_per_decade} {start} {stop}",
        ".end",
    ]
    return "\n".join(lines) + "\n"


def read_ac(path, channel):
    header, body = path.read_text(encoding="ascii").split("Values:\n", 1)
    metadata, variables = header.split("Variables:\n", 1)
    names = [line.split()[1].lower() for line in variables.splitlines()]
    count = int(re.search(r"(?m)^No\. Points:\s*(\d+)", metadata)[1])
    values = iter(body.splitlines())
    rows = list(zip(*([values] * len(names)), strict=True))
    if len(rows) != count or count < 2 or "Flags: complex" not in metadata:
        raise ValueError(f"Incomplete or unsupported AC data in {path}")
    trace = names.index(f"v(line_{channel})")
    return [(float(row[0].split()[1]), complex(*map(float, row[trace].split(","))))
            for row in rows]


def riaa_db(frequency, config):
    s = 2j * math.pi * frequency
    response = (1 + s * config["zero_s"]) / (
        (1 + s * config["low_frequency_pole_s"]) * (1 + s * config["high_frequency_pole_s"]))
    return 20 * math.log10(abs(response))


def write_csv(path, samples, cfg):
    frequency = [f for f, _ in samples]
    gain = [20 * math.log10(abs(v)) for _, v in samples]
    reference = cfg["analysis"]["reference_frequency_hz"]
    index = bisect.bisect_left(frequency, reference)
    if index < len(frequency) and math.isclose(reference, frequency[index], rel_tol=1e-12):
        reference_gain = gain[index]
    elif math.isclose(reference, frequency[-1], rel_tol=1e-12):
        reference_gain = gain[-1]
    elif 0 < index < len(frequency):
        weight = math.log(reference / frequency[index - 1]) / math.log(
            frequency[index] / frequency[index - 1])
        reference_gain = gain[index - 1] + weight * (gain[index] - gain[index - 1])
    else:
        raise ValueError(f"Reference frequency {reference:g} Hz is outside the QSPICE sweep")
    riaa = cfg["riaa"]
    reference_riaa = riaa_db(reference, riaa)
    errors = []
    phase = [math.degrees(math.atan2(v.imag, v.real)) for _, v in samples]
    with path.open("w", encoding="ascii", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frequency_hz", "gain_v_per_v", "gain_db", "phase_deg", "riaa_error_db"])
        for (f, v), db, degrees in zip(samples, gain, phase, strict=True):
            error = db - reference_gain - (riaa_db(f, riaa) - reference_riaa)
            errors.append(error)
            writer.writerow([f, abs(v), db, degrees, error])
    gain_at_reference = 10 ** (reference_gain / 20)
    print(f"{path.name}: {gain_at_reference:.5f} V/V at {reference:g} Hz, "
          f"RIAA error {min(errors):+.5f} ... {max(errors):+.5f} dB")
    return frequency, gain, phase, errors, gain_at_reference


def write_report(run, cfg, results):
    analysis = cfg["analysis"]
    model = cfg["opamp"]
    reference = analysis["reference_frequency_hz"]
    lines = [
        "# PreAmp — AC-анализ QSPICE",
        "",
        f"Диапазон {analysis['frequency_min_hz']:g}–{analysis['frequency_max_hz']:g} Гц. "
        f"Внешняя нагрузка {analysis['external_load_ohm']:g} Ом на канал. "
        f"Нормировка RIAA на {reference:g} Гц.",
        "",
        "| Канал | Точек | Усиление на опорной частоте, В/В | "
        "Мин. ошибка RIAA, дБ (Гц) | Макс. ошибка RIAA, дБ (Гц) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for channel, (frequency, _, _, errors, reference_gain) in results.items():
        lower = min(zip(errors, frequency))
        upper = max(zip(errors, frequency))
        lines.append(
            f"| [{channel.upper()}](preamp_{channel}.csv) | {len(frequency)} | "
            f"{reference_gain:.5f} | {lower[0]:+.5f} ({lower[1]:.3f}) | "
            f"{upper[0]:+.5f} ({upper[1]:.3f}) |"
        )
    lines += [
        "", "[Экспорт схемы KiCad](PreAmp.xml)",
        "",
        f"Линейная однополюсная модель NE5532 с A₀ = {model['open_loop_gain']:g} "
        f"и GBW = {model['gain_bandwidth_hz']:g} Гц. "
        "Вход возбуждается идеальным дифференциальным источником. "
        "Импеданс AT-3600L и ёмкость кабеля в модели отсутствуют.",
    ]
    if cfg["qspice"]["write_plots"]:
        lines += ["", "![АЧХ, фаза и ошибка RIAA](response.png)"]
    (run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_response(plot, run, reference, results):
    figure, axes = plot.subplots(3, 1, sharex=True, figsize=(9, 8), layout="constrained")
    figure.suptitle("PreAmp · QSPICE")
    for channel, (frequency, gain, phase, errors, _) in results.items():
        style = {"linestyle": "--", "color": "#be6225"} if channel == "r" else {"color": "#176b95"}
        for axis, values in zip(axes, (gain, phase, errors), strict=True):
            axis.semilogx(frequency, values, label=channel.upper(), **style)
    for axis, label in zip(axes, ("Усиление, дБ", "Фаза, °", "Ошибка RIAA, дБ"), strict=True):
        axis.set_ylabel(label)
        axis.grid(True, which="both", alpha=0.22)
        axis.axvline(reference, color="0.5", linewidth=0.8)
    axes[0].legend()
    axes[-1].axhline(0, color="0.5", linewidth=0.8)
    axes[-1].set_xlabel("Частота, Гц")
    figure.savefig(run / "response.png", dpi=160)
    figure.savefig(run / "response.svg")
    plot.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "scripts/config.toml")
    parser.add_argument("--qspice64", type=Path)
    parser.add_argument("--kicad-cli", type=Path)
    args = parser.parse_args()
    with args.config.open("rb") as stream:
        cfg = tomllib.load(stream)
    options = {"output_directory": "build/qspice", "write_report": True,
               "write_plots": True} | cfg.get("qspice", {})
    cfg["qspice"] = options
    analysis = cfg["analysis"]
    for key in ("frequency_min_hz", "frequency_max_hz", "reference_frequency_hz",
                "points", "external_load_ohm"):
        if key in options:
            analysis[key] = options[key]
        value = analysis[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            parser.error(f"{key}: expected a finite positive number")
    low, high, reference = (analysis[key] for key in
                            ("frequency_min_hz", "frequency_max_hz", "reference_frequency_hz"))
    if not low < high or not low <= reference <= high:
        parser.error("Expected frequency_min_hz < frequency_max_hz and reference within the sweep")
    if type(analysis["points"]) is not int or analysis["points"] < 2:
        parser.error("points: expected an integer >= 2")
    for key in ("write_report", "write_plots"):
        if type(options[key]) is not bool:
            parser.error(f"qspice.{key}: expected a boolean")
    if options["write_plots"]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plot
        except ModuleNotFoundError:
            parser.error("Install plotting dependencies with python -m pip install -e scripts "
                         "or set qspice.write_plots = false")
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    qspice_path = args.qspice64 or (ROOT / options["qspice64"] if "qspice64" in options else None)
    kicad_path = args.kicad_cli or (ROOT / options["kicad_cli"] if "kicad_cli" in options else None)
    simulator = executable(qspice_path, "QSPICE64.exe", [
        Path.home() / "QSPICE/QSPICE64.exe", program_files / "QSPICE/QSPICE64.exe"])
    kicad = executable(kicad_path, "kicad-cli.exe", [
        candidate
        for location in (local / "Programs/KiCad", program_files / "KiCad")
        for candidate in sorted(location.glob("*/bin/kicad-cli.exe"), reverse=True)
    ])
    output = ROOT / options["output_directory"]
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    print(run, flush=True)
    xml = run / "PreAmp.xml"
    subprocess.run([kicad, "sch", "export", "netlist", "--format", "kicadxml",
                    "-o", xml, ROOT / "PreAmp.kicad_sch"], check=True, timeout=90)
    results = {}
    for channel in ("l", "r"):
        deck = run / f"preamp_{channel}.cir"
        raw = deck.with_suffix(".qraw")
        deck.write_text(netlist(xml, cfg, channel), encoding="ascii")
        subprocess.run([simulator, "-ascii", deck.name, "-r", raw.name],
                       cwd=run, check=True, timeout=90)
        results[channel] = write_csv(deck.with_suffix(".csv"), read_ac(raw, channel), cfg)
    if options["write_plots"]:
        plot_response(plot, run, reference, results)
    if options["write_report"]:
        write_report(run, cfg, results)


if __name__ == "__main__":
    main()
