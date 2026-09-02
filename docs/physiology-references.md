# Physiological reference values

Every constant in `ml/vetra_ml/synth/species.py` and every disease signature in
`ml/vetra_ml/synth/conditions.py` traces back to this page. It exists so the
simulated data can be audited rather than taken on trust — if a reviewer
disagrees with a number, they can change it here and regenerate.

## Why the data is simulated

No public dataset of continuous, labelled, multi-channel vitals for Indian
smallholder livestock was available for this project. Real precision-livestock
datasets exist but are commercial, single-species, or cover only one channel
(usually rumination or activity from a specific collar vendor).

Rather than train on a mismatched dataset and overclaim, the pipeline generates
data from published reference ranges and documented disease presentations. The
consequence is stated plainly in the README: **the reported accuracy measures
the pipeline, not clinical performance.** Substituting real data means replacing
`ml/vetra_ml/synth/generator.py` with a loader that emits the same columns; no
other module changes.

## Resting reference ranges (adult animals)

| Species | Heart rate (bpm) | Rectal temp (°C) | Respiratory rate (/min) |
|---|---|---|---|
| Cattle  | 48–84  | 38.0–39.3 | 26–50 |
| Buffalo | 40–60  | 37.2–38.6 | 12–36 |
| Goat    | 70–110 | 38.6–40.0 | 15–30 |
| Sheep   | 70–90  | 38.3–39.9 | 16–34 |

Sources: Merck Veterinary Manual, *Clinical Examination of Farm Animals*
(Jackson & Cockcroft), and Indian Council of Agricultural Research (ICAR)
livestock husbandry material for buffalo values, which most Western texts omit.

Peripheral oxygen saturation in healthy ruminants sits at 95–99%. Values below
92% indicate hypoxaemia and below 88% are treated here as an emergency.

Rumination occupies roughly 7–9 hours a day in cattle and buffalo and 6–8 hours
in small ruminants, which is where the `rumination_duty` fractions come from.
A drop of 30% or more against an animal's own baseline is a well-established
early sign of illness and is what the `RUMINATION_COLLAPSE` rule encodes.

## Temperature-humidity index

```
THI = 0.8 T + (RH / 100) (T − 14.4) + 46.4        T in °C, RH in %
```

The standard livestock formula (NRC). Interpretation used by the rules:

| THI | Meaning | Rule severity |
|---|---|---|
| < 72 | No meaningful heat load | — |
| 72–78 | Mild; measurable in respiratory rate | — |
| 78–84 | Herd-wide advisory | `info` |
| 84–88 | Genuine heat stress | `warning` |
| ≥ 88 | Dangerous | `critical` |

Mild heat is deliberately advisory, not a per-animal warning: on a hot afternoon
it is true of every animal at once, and raising it as an alert per head would
bury the alerts that concern an individual.

## Disease presentations

Each condition is encoded as the shift it produces in each channel at full
severity. The discriminating feature — the one that separates it from its
nearest neighbour — is listed last.

| Condition | Presentation | Discriminator |
|---|---|---|
| Cardiac disorder | Resting tachycardia, irregular rhythm, mild hypoxaemia, exercise intolerance | Heart rate stays high *when the animal stops moving*; high beat-to-beat variability |
| Respiratory disease | Tachypnoea, falling SpO₂, moderate fever, reduced rumination | Hypoxaemia — breathing harder yet saturating worse |
| Obesity | Sustained inactivity, mild resting tachycardia and tachypnoea | Chronic, present at enrollment, no fever, normal gait |
| Diabetes / metabolic | Erratic activity, mild tachycardia, reduced rumination | High *variance* in activity rather than a shifted mean |
| Heat stress | Marked panting, raised core temperature, rumination collapse | Only under a high THI; SpO₂ stays near normal |
| Infectious disease | Fever, tachycardia, collapsed activity and rumination | Fever with *preserved* SpO₂ — this is what separates it from respiratory disease |
| Mobility disorder | Irregular gait, reduced distance, increased lying time | Gait rhythm collapses while temperature stays normal |

Two pairs are deliberately hard to separate, because they are hard in practice:

* **Respiratory disease vs. infectious disease** both present with fever and
  lethargy. Only SpO₂ and the respiratory-rate-to-effort ratio distinguish them.
* **Heat stress vs. respiratory disease** both drive respiratory rate up. The
  ambient THI is what separates a panting animal from a sick one, which is why
  ambient conditions are carried as model inputs rather than discarded.

## Severity and onset

Acute conditions ramp from zero at an onset time over 6–36 hours to a plateau
between 0.55 and 1.0. Chronic conditions (obesity, diabetes) hold a constant
severity, since they are already established when monitoring begins.

A window is labelled with the condition only once mean severity crosses **0.15**.
Below that the animal genuinely still presents as healthy, and labelling it
diseased would train the model to invent illness from noise.
