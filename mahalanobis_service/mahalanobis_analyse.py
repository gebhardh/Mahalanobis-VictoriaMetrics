"""
Anomalieerkennung mit der Mahalanobis-Distanz.

Ergänzt die Messprobe um:
  - mahalanobis_d2 / mahalanobis_d   quadrierte bzw. einfache Distanz
  - schwelle_d                       Schwellwert für die Distanz
  - anomalie                         True, wenn mahalanobis_d > schwelle_d
  - konstant_abweichung              in df_ref konstante Merkmale, deren Wert in der Probe abweicht
  - haupttreiber                     Merkmal mit dem größten Leave-one-out-Effekt
  - z_<merkmal>                      univariater z-Wert
  - beitrag_<merkmal>                additiver Beitrag zu D² (Zeilensumme = D²)
  - anteil_<merkmal>                 relativer Anteil des Beitrags an D²
  - loo_<merkmal>                    Rückgang von D², wenn das Merkmal weggelassen wird
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.stats import chi2


def _schaetze_lage_und_kovarianz(ref: np.ndarray, robust: bool, support_fraction: float | None = None):
    """
    Mittelwert und Kovarianz der Referenzdaten (klassisch oder robust via MCD).

    Robust: MinCovDet schätzt Lage und Kovarianz aus dem "kompaktesten" Anteil support_fraction
    der Referenzpunkte (Standard sklearn: ca. 0.5). Bei Daten mit Tagesgang ist das oft nur die
    ruhige Phase, normale Last gilt dann als anomal -> eher 0.8–0.9 wählen.

    Die Daten werden vor dem Fit standardisiert (bessere Kondition bei Merkmalen in sehr
    unterschiedlichen Größenordnungen, z. B. Load ~1 und Bytes/s ~1e6) und danach zurück-
    transformiert. Das MCD-Ergebnis ändert sich dadurch nicht (affin äquivariant).
    """
    if not robust:
        return ref.mean(axis=0), np.atleast_2d(np.cov(ref, rowvar=False))

    from sklearn.covariance import MinCovDet
    mitte = ref.mean(axis=0)
    skala = ref.std(axis=0)
    skala = np.where(skala > 0, skala, 1.0)
    with warnings.catch_warnings():
        # sklearn meldet in Zwischenschritten (C-Steps auf Teilmengen) gelegentlich einen
        # numerisch bedingten Anstieg der Determinante. Das betrifft verworfene Kandidaten,
        # nicht das Endergebnis -> nicht als Warnung durchreichen.
        warnings.filterwarnings("ignore", message="Determinant has increased", category=RuntimeWarning)
        mcd = MinCovDet(random_state=0, support_fraction=support_fraction).fit((ref - mitte) / skala)
    lage = mitte + mcd.location_ * skala
    kovarianz = mcd.covariance_ * np.outer(skala, skala)
    return lage, kovarianz


def bereinige_referenz(
    df_ref: pd.DataFrame,
    merkmale: list | None = None,
    quantil: float = 0.999,
    iterationen: int = 3,
    robust: bool = False,
    support_fraction: float | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Entfernt deutliche Anomalien aus den Referenzdaten (iterative Bereinigung).

    Je Iteration: Modell aus den aktuell behaltenen Punkten schätzen, Distanz D für ALLE
    Referenzpunkte berechnen, Punkte mit D > Wurzel(chi2(quantil, p)) entfernen. Ein Punkt
    kann in einer späteren Iteration wieder aufgenommen werden. Ende, wenn sich nichts mehr
    ändert oder nach `iterationen` Durchläufen.

    quantil  hoch wählen (0.999 oder 0.9999), sonst fallen auch seltene, aber normale Spitzen heraus

    Rückgabe: (bereinigte Referenz mit allen Spalten, Info-Dict mit vorher/nachher/iterationen)
    Zeilen mit fehlenden Werten werden dabei verworfen; konstante Merkmale bleiben unberücksichtigt.
    """
    if merkmale is None:
        merkmale = df_ref.select_dtypes("number").columns.tolist()
    ref_df = df_ref.dropna(subset=list(merkmale))
    std = ref_df[merkmale].std()
    aktiv = [m for m in merkmale if std[m] > 1e-12 * max(1.0, abs(ref_df[m].mean()))]
    info = {"vorher": len(ref_df), "nachher": len(ref_df), "iterationen": 0}
    if not aktiv or len(ref_df) <= len(aktiv):
        return ref_df, info

    X = ref_df[aktiv].to_numpy(dtype=float)
    grenze = np.sqrt(chi2.ppf(quantil, df=len(aktiv)))
    maske = np.ones(len(X), dtype=bool)
    for i in range(iterationen):
        mu, cov = _schaetze_lage_und_kovarianz(X[maske], robust, support_fraction)
        diff = X - mu
        d = np.sqrt(np.einsum("ij,jk,ik->i", diff, np.linalg.pinv(cov), diff))
        neu = d <= grenze
        info["iterationen"] = i + 1
        if neu.sum() <= len(aktiv):          # nie (fast) alles entfernen
            break
        if (neu == maske).all():
            break
        maske = neu
    info["nachher"] = int(maske.sum())
    return ref_df[maske], info


def _d2(diff: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """Quadrierte Mahalanobis-Distanz für jede Zeile von diff."""
    if diff.shape[1] == 0:
        return np.zeros(diff.shape[0])
    cov_inv = np.linalg.pinv(cov)
    return np.einsum("ij,jk,ik->i", diff, cov_inv, diff)


def mahalanobis_analyse(
    df_ref: pd.DataFrame,
    df_probe: pd.DataFrame,
    merkmale: list | None = None,
    quantil: float = 0.99,
    schwelle: str = "chi2",   # "chi2" (setzt Normalverteilung voraus) oder "empirisch"
    robust: bool = False,     # True: robuste Kovarianz (MinCovDet) gegen Ausreißer in df_ref
    support_fraction: float | None = None,  # nur robust: Anteil der Referenz im MCD-Kern (None = ca. 0.5)
) -> pd.DataFrame:
    """Gibt eine Kopie von df_probe zurück, ergänzt um alle Kennwerte."""
    if merkmale is None:
        merkmale = df_ref.select_dtypes("number").columns.tolist()
    merkmale = list(merkmale)

    # Konstante Merkmale (Varianz 0) erkennen: Sie machen die Kovarianz singulär
    # und führen bei den z-Werten zu 0/0. Sie werden aus der Distanz ausgeschlossen
    # und stattdessen auf exakte Abweichung vom Referenzwert geprüft.
    ref_df = df_ref[merkmale].dropna()
    std = ref_df.std()
    konstant = [m for m in merkmale if std[m] <= 1e-12 * max(1.0, abs(ref_df[m].mean()))]
    if konstant:
        warnings.warn(
            f"Konstante Merkmale in den Referenzdaten, aus der Distanz ausgeschlossen: {konstant}",
            stacklevel=2,
        )
        konst_wert = ref_df[konstant].iloc[0]
        abweich = ~np.isclose(df_probe[konstant].to_numpy(dtype=float), konst_wert.to_numpy())
        konst_abweichung = [
            ", ".join(k for k, a in zip(konstant, zeile) if a) or None for zeile in abweich
        ]
        merkmale = [m for m in merkmale if m not in konstant]
    else:
        konst_abweichung = [None] * len(df_probe)
    if not merkmale:
        raise ValueError("Keine nicht-konstanten Merkmale übrig.")

    ref = df_ref[merkmale].dropna().to_numpy(dtype=float)
    probe = df_probe[merkmale].to_numpy(dtype=float)
    n, p = ref.shape
    if n <= p:
        raise ValueError(f"Zu wenige Referenzzeilen ({n}) für {p} Merkmale.")

    mu, cov = _schaetze_lage_und_kovarianz(ref, robust, support_fraction)
    diff = probe - mu
    cov_inv = np.linalg.pinv(cov)

    # Distanz
    d2 = np.einsum("ij,jk,ik->i", diff, cov_inv, diff)
    d = np.sqrt(d2)

    # Schwellwert
    if schwelle == "chi2":
        schwelle_d = np.sqrt(chi2.ppf(quantil, df=p))
    elif schwelle == "empirisch":
        ref_diff = ref - mu
        ref_d = np.sqrt(np.einsum("ij,jk,ik->i", ref_diff, cov_inv, ref_diff))
        schwelle_d = np.quantile(ref_d, quantil)
    else:
        raise ValueError("schwelle muss 'chi2' oder 'empirisch' sein.")

    # z-Werte (mit derselben Lage/Streuung wie die Distanz)
    # (Schutz für den Fall, dass die robuste Schätzung eine Varianz von 0 liefert)
    sd = np.sqrt(np.clip(np.diag(cov), 0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sd > 0, diff / np.where(sd > 0, sd, 1.0), np.nan)

    # Additive Beiträge: Zeilensumme = D²
    beitrag = diff * (diff @ cov_inv)
    with np.errstate(divide="ignore", invalid="ignore"):
        anteil = beitrag / d2[:, None]

    # Leave-one-out: D² ohne Merkmal j (Teilmatrix der Kovarianz neu invertieren)
    loo = np.empty_like(diff)
    for j in range(p):
        rest = [k for k in range(p) if k != j]
        loo[:, j] = d2 - _d2(diff[:, rest], cov[np.ix_(rest, rest)])

    # Ergebnis zusammenbauen
    out = df_probe.copy()
    out["mahalanobis_d2"] = d2
    out["mahalanobis_d"] = d
    out["schwelle_d"] = schwelle_d
    out["anomalie"] = d > schwelle_d
    out["konstant_abweichung"] = konst_abweichung   # konstante Merkmale mit abweichendem Wert
    # Haupttreiber nur bei Anomalien – bei kleiner Distanz wäre er reines Rauschen
    idx_max = np.argmax(np.where(np.isnan(loo), -np.inf, loo), axis=1)
    out["haupttreiber"] = [
        merkmale[i] if (ist_anomal and np.isfinite(row).all()) else None
        for i, row, ist_anomal in zip(idx_max, loo, out["anomalie"])
    ]

    zusatz = pd.concat(
        [
            pd.DataFrame(z, columns=[f"z_{m}" for m in merkmale], index=out.index),
            pd.DataFrame(beitrag, columns=[f"beitrag_{m}" for m in merkmale], index=out.index),
            pd.DataFrame(anteil, columns=[f"anteil_{m}" for m in merkmale], index=out.index),
            pd.DataFrame(loo, columns=[f"loo_{m}" for m in merkmale], index=out.index),
        ],
        axis=1,
    )
    return pd.concat([out, zusatz], axis=1)


if __name__ == "__main__":
    # Beispiel mit synthetischen Daten: Druck und Temperatur stark korreliert
    rng = np.random.default_rng(42)
    n = 500
    temp = rng.normal(80, 5, n)
    druck = 2.0 * temp + rng.normal(0, 2, n)
    drehzahl = rng.normal(1500, 50, n)
    df_ref = pd.DataFrame({"temperatur": temp, "druck": druck, "drehzahl": drehzahl})

    df_probe = pd.DataFrame(
        {
            "temperatur": [80.0, 75.0, 80.0],
            "druck":      [160.0, 170.0, 160.0],   # Zeile 2: Korrelationsbruch
            "drehzahl":   [1500.0, 1500.0, 1700.0],  # Zeile 3: Drehzahl univariat auffällig
        },
        index=["normal", "korrelationsbruch", "drehzahl_hoch"],
    )

    erg = mahalanobis_analyse(df_ref, df_probe)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    print(erg.round(2).T)
