"""Общие инструменты расчёта RIAA, узловых моделей и графиков.

Две модели каждой схемы проверяют друг друга:
1. Передаточная функция, полученная вручную для идеальных ОУ.
2. Узловой расчёт по Кирхгофу с отдельными входами каждого ОУ.

Однополюсная модель служит только оценкой влияния конечного усиления и GBW.
Она не подтверждает устойчивость реального NE5532: дополнительные полюса,
выходной импеданс, паразитные ёмкости, шум и насыщение здесь не моделируются.
Источник — идеальный генератор с нулевым импедансом. Катушка и кабель не включены.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import tomllib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent


class NodalCircuit:
    """Y*V = I. Строки выходов ОУ заменяются уравнениями Vout=A*(V+ - V-)."""

    def __init__(self, names: tuple[str, ...]):
        self.index = {name: i for i, name in enumerate(names)}
        self.y = np.zeros((len(names), len(names)), dtype=complex)
        self.rhs = np.zeros(len(names), dtype=complex)

    def branch(self, first: str, second: str | None, admittance: complex):
        i = self.index[first]
        self.y[i, i] += admittance
        if second is not None:
            j = self.index[second]
            self.y[j, j] += admittance
            self.y[i, j] -= admittance
            self.y[j, i] -= admittance

    def source_branch(self, node: str, admittance: complex, voltage: complex):
        """Ветвь от узла к идеальному источнику с известным напряжением."""
        self.branch(node, None, admittance)
        self.rhs[self.index[node]] += admittance * voltage

    def amplifier(self, output: str, positive: str, negative: str, inverse_gain: complex):
        row = self.index[output]
        self.y[row, :] = 0
        self.y[row, self.index[output]] = inverse_gain
        self.y[row, self.index[positive]] = -1
        self.y[row, self.index[negative]] = 1

    def solve(self) -> dict[str, complex]:
        voltages = np.linalg.solve(self.y, self.rhs)
        return {name: complex(voltages[i]) for name, i in self.index.items()}


def feedback_impedance(s: complex, p: dict) -> complex:
    """Zf = Rf || (Rz + 1/(s*Cf)): две первые постоянные времени RIAA."""
    return 1 / (1 / p["feedback_resistor_ohm"] + 1 / (
        p["feedback_series_resistor_ohm"] + 1 / (s * p["feedback_cap_f"])))


def inverse_open_loop_gain(s: complex, model: dict, finite: bool) -> complex:
    if not finite:
        return 0j
    a0 = model["open_loop_gain"]
    gbw = model["gain_bandwidth_hz"]
    return (1 + s * a0 / (2 * math.pi * gbw)) / a0


def parallel_load(p: dict, analysis: dict) -> float:
    return 1 / (1 / p["output_bleed_resistor_ohm"] + 1 / analysis["external_load_ohm"])


def riaa(frequency: float, parameters: dict) -> complex:
    s = 2j * math.pi * frequency
    return (1 + s * parameters["zero_s"]) / (
        (1 + s * parameters["low_frequency_pole_s"]) *
        (1 + s * parameters["high_frequency_pole_s"]))


def db(value):
    return 20 * np.log10(np.maximum(np.abs(value), 1e-300))


def load_config(path: Path, section: str) -> dict:
    with path.open("rb") as stream:
        cfg = tomllib.load(stream)
    for name in ("analysis", "opamp", "riaa", section):
        for key, value in cfg[name].items():
            if isinstance(value, (float, int)) and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name}.{key}: требуется конечное положительное число")
    a = cfg["analysis"]
    if not a["frequency_min_hz"] < a["frequency_max_hz"]:
        raise ValueError("frequency_min_hz должен быть меньше frequency_max_hz")
    if not isinstance(a["points"], int) or a["points"] < 2:
        raise ValueError("points: требуется целое число >= 2")
    return cfg


def run(section: str, transfer, nodal):
    parser = argparse.ArgumentParser(description=f"Расчёт {section} RIAA-фонокорректора")
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "config.toml")
    parser.add_argument("--output-dir", type=Path, help="Каталог CSV, JSON, PNG и PDF")
    args = parser.parse_args()
    cfg = load_config(args.config, section)
    a, p, model = cfg["analysis"], cfg[section], cfg["opamp"]
    outdir = args.output_dir or ROOT / a["output_directory"]
    outdir.mkdir(parents=True, exist_ok=True)
    f = np.geomspace(a["frequency_min_hz"], a["frequency_max_hz"], a["points"])
    ref = a["reference_frequency_hz"]
    closed = np.array([transfer(x, p, a) for x in f])
    ideal = np.array([nodal(x, p, a, model)["line"] for x in f])
    finite = np.array([nodal(x, p, a, model, finite=True)["line"] for x in f])
    relative_error = float(np.max(np.abs(ideal / closed - 1)))
    if relative_error > 1e-8:
        raise RuntimeError(f"Узловая модель не совпала с формулой: {relative_error:g}")
    gain_ref = abs(transfer(ref, p, a))
    finite_ref = abs(nodal(ref, p, a, model, finite=True)["line"])
    target = np.array([riaa(x, cfg["riaa"]) for x in f])
    target *= gain_ref / abs(riaa(ref, cfg["riaa"]))
    error = db(ideal / target)
    finite_error = db(finite / target) - float(db(finite_ref / gain_ref))
    cm_max = None
    if section == "balanced":
        # На оба конца источника подаём один и тот же 1 В. У идеальной,
        # согласованной схемы выход должен быть нулевым. Это не обещание
        # реального CMRR: допуски и асимметрия кабеля сюда не включены.
        cm_max = max(abs(nodal(x, p, a, model, common=True)["line"]) for x in f)
        if cm_max > 1e-7:
            raise RuntimeError(f"Синфазная проверка не пройдена: {cm_max:g}")
    summary = {
        "circuit": section,
        "reference_hz": ref,
        "gain_v_per_v": gain_ref,
        "gain_db": float(db(gain_ref)),
        "output_rms_v": gain_ref * a["input_rms_v"],
        "input_resistance_ohm_midband": p["input_bias_resistor_ohm"] * (2 if section == "balanced" else 1),
        "riaa_error_min_db": float(error.min()),
        "riaa_error_max_db": float(error.max()),
        "one_pole_shape_error_min_db": float(finite_error.min()),
        "one_pole_shape_error_max_db": float(finite_error.max()),
        "one_pole_gain_at_reference_db": float(db(finite_ref)),
        "nodal_vs_formula_max_relative_error": relative_error,
        "ideal_common_mode_output_max_v": cm_max,
        "config": cfg,
    }
    stem = outdir / section
    stem.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    with stem.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frequency_hz", "ideal_gain_db", "one_pole_gain_db", "riaa_error_db",
                         "one_pole_normalized_riaa_error_db", "ideal_phase_deg"])
        phase = np.rad2deg(np.unwrap(np.angle(ideal)))
        writer.writerows(zip(f, db(ideal), db(finite), error, finite_error, phase))
    fig, axes = plt.subplots(2, 1, figsize=(9, 7), sharex=True, layout="constrained")
    axes[0].semilogx(f, db(ideal), label="Ideal op amps", lw=2)
    axes[0].semilogx(f, db(finite), label="One-pole op amps", lw=1.4)
    axes[0].semilogx(f, db(target), "--", label="RIAA reference", lw=1.4)
    axes[0].set(ylabel="Gain, dB", title=f"NE5532 / AT-3600L: {section}")
    axes[1].semilogx(f, error, label="Ideal op amps", lw=2)
    axes[1].semilogx(f, finite_error, label="One-pole op amps", lw=1.4)
    axes[1].axhline(0, color="0.5", lw=.7)
    axes[1].set(xlabel="Frequency, Hz", ylabel=f"RIAA error, dB (normalized at {ref:g} Hz)")
    for ax in axes:
        ax.grid(True, which="both", alpha=.25)
        ax.legend()
    fig.savefig(stem.with_suffix(".png"), dpi=180)
    fig.savefig(stem.with_suffix(".pdf"))
    plt.close(fig)
    print(f"{section}: {gain_ref:.3f} V/V = {float(db(gain_ref)):.3f} dB at {ref:g} Hz")
    print(f"Input {a['input_rms_v']*1000:g} mV rms -> {summary['output_rms_v']:.4f} V rms")
    print(f"RIAA error: {error.min():+.4f} .. {error.max():+.4f} dB")
    print(f"One-pole RIAA shape error: {finite_error.min():+.4f} .. {finite_error.max():+.4f} dB")
    print(f"Nodal / formula relative error: {relative_error:.3g}; common-mode residual: {cm_max}")
    print(" Hz       gain dB     RIAA error dB")
    for freq in (20, 50, 100, 1000, 10000, 20000):
        h = transfer(freq, p, a)
        target_at_f = riaa(freq, cfg["riaa"]) * gain_ref / abs(riaa(ref, cfg["riaa"]))
        print(f"{freq:6g}   {float(db(h)):10.3f}   {float(db(h/target_at_f)):+.4f}")
    print(f"Results: {stem}.{{csv,json,png,pdf}}")
    print("Linear model only: no cartridge impedance, cable, noise, clipping or stability verification.")
