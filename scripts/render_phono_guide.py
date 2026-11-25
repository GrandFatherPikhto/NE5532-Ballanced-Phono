"""Иллюстрации отдельного руководства docs/PHONO_GUIDE.tex.

Запуск из корня: scripts/.venv/bin/python scripts/render_phono_guide.py
Читает scripts/config.toml и существующие модели, ничего в них не меняет.
Новые результаты сохраняет только в build/phono_guide/.
"""

import json
import math
import tomllib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from calculate_balanced_riaa import nodal, transfer as balanced
from calculate_unbalanced_riaa import transfer as unbalanced
from riaa_common import ROOT, SCRIPT_DIR, db, feedback_impedance, riaa


OUT = ROOT / "build/phono_guide"
OUT.mkdir(parents=True, exist_ok=True)
with (SCRIPT_DIR / "config.toml").open("rb") as stream:
    cfg = tomllib.load(stream)
p, a = cfg["balanced"], cfg["analysis"]
f = np.geomspace(20, 20000, 1001)
ref = 1000
target = np.array([riaa(x, cfg["riaa"]) for x in f]) / abs(riaa(ref, cfg["riaa"]))
hb = np.array([balanced(x, p, a) for x in f])
hu = np.array([unbalanced(x, cfg["unbalanced"], a) for x in f])
gb = abs(balanced(ref, p, a))
gu = abs(unbalanced(ref, cfg["unbalanced"], a))

plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})


def save(fig, name):
    for suffix in ("pdf", "png"):
        fig.savefig(OUT / f"{name}.{suffix}", dpi=170)
    plt.close(fig)


fig, axes = plt.subplots(2, 1, figsize=(7.0, 6.0), sharex=True, layout="constrained")
axes[0].semilogx(f, db(target), color="black", label="Эталон RIAA")
axes[0].semilogx(f, db(hb / gb), "--", label="Балансный вход")
axes[0].semilogx(f, db(hu / gu), ":", label="Небалансный вход")
axes[0].set(ylabel="Относительно 1 кГц, дБ")
axes[1].semilogx(f, db(hb / gb / target), label="Балансный вход")
axes[1].semilogx(f, db(hu / gu / target), label="Небалансный вход")
axes[1].set(xlabel="Частота, Гц", ylabel="Ошибка RIAA, дБ")
for ax in axes:
    ax.grid(True, which="both", alpha=.25)
    ax.legend()
save(fig, "response_comparison")

fig, ax = plt.subplots(figsize=(7.0, 3.6), layout="constrained")
for ceq in (500e-6, 100e-6, 50e-6):
    case = {**p, "gain_bridge_cap_a_f": 2 * ceq, "gain_bridge_cap_b_f": 2 * ceq}
    h = np.array([balanced(x, case, a) for x in f])
    gain = abs(balanced(ref, case, a))
    ax.semilogx(f, db(h / gain / target), label=f"Cэкв = {ceq*1e6:g} мкФ")
ax.set(xlim=(20, 200), xlabel="Частота, Гц", ylabel="Ошибка RIAA, дБ")
ax.grid(True, which="both", alpha=.25)
ax.legend()
save(fig, "bridge_capacitance")

fig, ax = plt.subplots(figsize=(7.0, 3.6), layout="constrained")
g1 = []
g2 = []
for x in f:
    s = 2j * math.pi * x
    zg = p["gain_bridge_resistor_ohm"] + 1/(s*p["gain_bridge_cap_a_f"]) + 1/(s*p["gain_bridge_cap_b_f"])
    g1.append(1 + 2*feedback_impedance(s, p)/zg)
    g2.append(1/(1 + s*p["receiver_resistor_ohm"]*p["receiver_cap_f"]))
ax.semilogx(f, db(g1), label="Входная пара: дифференциальное усиление")
ax.semilogx(f, db(g2), label="U3: разностный фильтр")
ax.semilogx(f, db(hb), label="Весь канал с разделительными C и нагрузкой")
ax.set(xlabel="Частота, Гц", ylabel="Усиление, дБ")
ax.grid(True, which="both", alpha=.25)
ax.legend(fontsize=8)
save(fig, "stage_gains")

rows = []
summary = {"balanced_gain_1khz": gb, "unbalanced_gain_1khz": gu, "frequency_rows": []}
for x in (20, 50, 100, 1000, 10000, 20000):
    gain = abs(balanced(x, p, a))
    v = nodal(x, p, a, cfg["opamp"])
    pre = abs(v["op"] - v["om"])
    row = {"frequency_hz": x, "gain": gain, "gain_db": float(db(gain)),
           "pre_receiver_diff_gain": pre, "output_at_0_5mv_rms": gain*.0005,
           "riaa_relative_db": float(db(riaa(x, cfg["riaa"])/riaa(ref, cfg["riaa"])))}
    summary["frequency_rows"].append(row)
    rows.append(f"{x:g} & {row['gain_db']:.2f} & {gain:.2f} & {gain*.5:.2f} \\\\")
table = ("\\begin{tabular}{@{}rrrr@{}}\n\\toprule\n"
         "Частота, Гц & Усиление, дБ & Усиление, В/В & Выход, мВ RMS\\\\\n"
         "\\midrule\n" + "\n".join(rows) + "\n\\bottomrule\n\\end{tabular}\n")
(OUT / "measurements.tex").write_text(table, encoding="utf-8")
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print(f"Guide figures: {OUT}")
