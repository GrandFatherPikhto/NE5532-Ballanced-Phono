"""Shared KiCad-to-SPICE AC model, configuration, reports and plots."""

import bisect
import csv
import math
from pathlib import Path
import re
import tomllib
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: expected a finite positive number")
    return value


def load_config(path, section):
    with Path(path).open("rb") as stream:
        cfg = tomllib.load(stream)
    supplied = cfg.get(section, {})
    if not isinstance(supplied, dict):
        raise ValueError(f"{section}: expected a TOML table")
    options = {"output_directory": f"build/{section}", "write_report": True,
               "write_plots": True} | supplied
    cfg[section] = options
    analysis = cfg["analysis"]
    if not isinstance(analysis, dict):
        raise ValueError("analysis: expected a TOML table")
    for key in ("frequency_min_hz", "frequency_max_hz", "reference_frequency_hz",
                "points", "external_load_ohm"):
        if key in options:
            analysis[key] = options[key]
        positive(analysis[key], key)
    low, high, reference = (analysis[key] for key in
                            ("frequency_min_hz", "frequency_max_hz", "reference_frequency_hz"))
    if not low < high or not low <= reference <= high:
        raise ValueError("Expected frequency_min_hz < frequency_max_hz and reference within the sweep")
    if type(analysis["points"]) is not int or analysis["points"] < 2:
        raise ValueError("points: expected an integer >= 2")
    for key in ("write_report", "write_plots"):
        if type(options[key]) is not bool:
            raise ValueError(f"{section}.{key}: expected a boolean")
    if not isinstance(options["output_directory"], str) or not options["output_directory"].strip():
        raise ValueError(f"{section}.output_directory: expected a nonempty path")
    for key in ("open_loop_gain", "gain_bandwidth_hz"):
        positive(cfg["opamp"][key], f"opamp.{key}")
    for key in ("low_frequency_pole_s", "zero_s", "high_frequency_pole_s"):
        positive(cfg["riaa"][key], f"riaa.{key}")
    return cfg


def load_plot(options):
    if not options["write_plots"]:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plot
    except ModuleNotFoundError as error:
        raise ValueError("Install plotting dependencies with python -m pip install -e scripts "
                         "or set write_plots = false") from error
    return plot


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
        raise ValueError(f"Reference frequency {reference:g} Hz is outside the AC sweep")
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


def write_report(run, cfg, results, simulator="QSPICE", section="qspice"):
    analysis = cfg["analysis"]
    model = cfg["opamp"]
    reference = analysis["reference_frequency_hz"]
    lines = [
        f"# PreAmp — AC-анализ {simulator}",
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
        "Импеданс AT-3600L и ёмкость кабеля в модели отсутствуют. "
        "Выход ОУ идеален, питание на модель не влияет. "
        "Шум, THD, перегрузка и реальная устойчивость не проверяются.",
    ]
    if cfg[section]["write_plots"]:
        lines += ["", "![АЧХ, фаза и ошибка RIAA](response.png)"]
    (run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_response(plot, run, reference, results, simulator="QSPICE"):
    figure, axes = plot.subplots(3, 1, sharex=True, figsize=(9, 8), layout="constrained")
    figure.suptitle(f"PreAmp · {simulator}")
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


