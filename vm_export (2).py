"""
Export nach VictoriaMetrics.

  nach_victoriametrics()            Ergebnis von mahalanobis_analyse(), strukturiert (siehe unten)
  dataframe_nach_victoriametrics()  beliebiges DataFrame: Spaltenname = Metrikname, globale Labels
  probe_und_ergebnis_nach_victoriametrics()
                                    Rohdaten der Messprobe + Analyseergebnis in einem Aufruf

Der Zeitstempel jeder Zeile von df_probe wird übernommen (siehe zeitstempel_ms()).

Format: Prometheus-Textformat mit Zeitstempel (ms), gesendet an
  Single-Node:  http://<host>:8428/api/v1/import/prometheus
  Cluster:      http://<vminsert>:8480/insert/<accountID>/prometheus/api/v1/import/prometheus

Erzeugte Metriken (Präfix einstellbar, Standard "mahalanobis"):
  mahalanobis_d, mahalanobis_d2, mahalanobis_schwelle_d, mahalanobis_anomalie (0/1)
  mahalanobis_messwert{merkmal="..."}      gemessener Wert
  mahalanobis_z{merkmal="..."}             z-Wert
  mahalanobis_beitrag{merkmal="..."}       additiver Beitrag zu D²
  mahalanobis_anteil{merkmal="..."}        Anteil an D²
  mahalanobis_loo{merkmal="..."}           Leave-one-out-Effekt
  mahalanobis_haupttreiber{merkmal="..."} 1           (nur bei Anomalie)
  mahalanobis_konstant_abweichung{merkmal="..."} 1    (nur bei Abweichung)

Merkmale werden als Label "merkmal" geführt (nicht im Metriknamen), damit sie sich in
PromQL/MetricsQL einfach vergleichen lassen, z. B.  topk(3, mahalanobis_loo{anlage="A1"}).
"""

import gzip
import math
import re
import warnings

import numpy as np
import pandas as pd
import requests


# --------------------------------------------------------------------------- Hilfsfunktionen

def _label_name(name: str) -> str:
    n = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))
    return f"_{n}" if n[:1].isdigit() else n


def _label_value(wert) -> str:
    return str(wert).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(d: dict) -> str:
    return "{" + ",".join(f'{_label_name(k)}="{_label_value(v)}"' for k, v in d.items()) + "}"


def _metric_name(name: str) -> str:
    """Macht aus einem Spaltennamen einen gültigen Metriknamen ([a-zA-Z_:][a-zA-Z0-9_:]*)."""
    n = re.sub(r"[^a-zA-Z0-9_:]", "_", str(name))
    return n if re.match(r"[a-zA-Z_:]", n[:1]) else f"_{n}"


def _zahl(v):
    """Gibt float zurück oder None, wenn der Wert nicht schreibbar ist (NaN, inf, None)."""
    if v is None or isinstance(v, str):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- Zeitstempel

def zeitstempel_ms(
    df: pd.DataFrame,
    zeit_spalte: str | None = None,
    zeitzone_naiv: str = "UTC",
) -> np.ndarray:
    """
    Ermittelt für jede Zeile den Zeitstempel in Millisekunden (Unix-Zeit, UTC).

    Reihenfolge:
      1. zeit_spalte, falls angegeben
      2. Index, falls er ein DatetimeIndex ist oder sich in Zeitstempel umwandeln lässt
      3. die einzige Datetime-Spalte des DataFrames
    Findet sich nichts, wird ein Fehler ausgelöst – es wird nie stillschweigend "jetzt" verwendet.

    zeitzone_naiv  Zeitzone für Zeitstempel ohne Zeitzonenangabe, z. B. "Europe/Berlin",
                   wenn die Messdaten in lokaler Zeit vorliegen.
    """
    if zeit_spalte is not None:
        zeiten = pd.to_datetime(df[zeit_spalte])
    elif isinstance(df.index, pd.DatetimeIndex):
        zeiten = df.index.to_series()
    else:
        zeiten = None
        if not pd.api.types.is_numeric_dtype(df.index):   # z. B. Zeitstempel als Strings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    zeiten = pd.to_datetime(df.index.to_series())
            except (ValueError, TypeError):
                zeiten = None
        if zeiten is None:
            dt_spalten = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
            if len(dt_spalten) == 1:
                zeiten = df[dt_spalten[0]]
            else:
                raise ValueError(
                    "Kein Zeitstempel gefunden. Bitte df_probe mit DatetimeIndex übergeben "
                    f"oder zeit_spalte angeben (Datetime-Spalten gefunden: {dt_spalten})."
                )

    zeiten = pd.DatetimeIndex(zeiten)
    if zeiten.tz is None:
        try:
            zeiten = zeiten.tz_localize(zeitzone_naiv, ambiguous="infer", nonexistent="shift_forward")
        except Exception:
            # z. B. nicht sortiert beim Rückfall auf Winterzeit: mehrdeutige Stunde als Sommerzeit
            zeiten = zeiten.tz_localize(zeitzone_naiv, ambiguous=True, nonexistent="shift_forward")
    if zeiten.isna().any():
        raise ValueError(f"{int(zeiten.isna().sum())} Zeilen ohne gültigen Zeitstempel.")
    return (zeiten.tz_convert("UTC").as_unit("ns").asi8 // 1_000_000).astype(np.int64)


# --------------------------------------------------------------------------- Umwandlung

def ergebnis_zu_prometheus(
    erg: pd.DataFrame,
    labels: dict,
    prefix: str = "mahalanobis",
    zeit_spalte: str | None = None,
    zeitzone_naiv: str = "UTC",
    zeilen_label: str | None = None,
    mit_messwert: bool = True,
) -> str:
    """
    Wandelt das Ergebnis-DataFrame in Prometheus-Textformat um.

    mit_messwert   False: <prefix>_messwert nicht schreiben (z. B. wenn die Rohdaten der
                   Messprobe ohnehin separat als eigene Metriken geschrieben werden)

    labels         feste Labels für alle Zeitreihen, z. B. {"anlage": "A1", "modell": "v2"}
    zeit_spalte    Spalte mit Zeitstempel (sonst Index bzw. einzige Datetime-Spalte)
    zeitzone_naiv  Zeitzone für Zeitstempel ohne Zeitzonenangabe
    zeilen_label   optional: Index als Label mit diesem Namen (Vorsicht: Kardinalität!)
    """
    ts_ms = zeitstempel_ms(erg, zeit_spalte, zeitzone_naiv)
    merkmale = [c[2:] for c in erg.columns if c.startswith("z_")]
    zeilen = []

    for ts, (idx, row) in zip(ts_ms, erg.iterrows()):
        basis = dict(labels)
        if zeilen_label:
            basis[zeilen_label] = idx

        def add(name, wert, extra=None):
            w = _zahl(wert)
            if w is not None:
                lab = {**basis, **(extra or {})}
                zeilen.append(f"{prefix}_{name}{_labels(lab)} {w!r} {ts}")

        # Gesamtwerte
        add("d", row.get("mahalanobis_d"))
        add("d2", row.get("mahalanobis_d2"))
        add("schwelle_d", row.get("schwelle_d"))
        if "anomalie" in row:
            add("anomalie", int(bool(row["anomalie"])))

        # Werte pro Merkmal
        for m in merkmale:
            mlab = {"merkmal": m}
            if mit_messwert:
                add("messwert", row.get(m), mlab)
            add("z", row.get(f"z_{m}"), mlab)
            add("beitrag", row.get(f"beitrag_{m}"), mlab)
            add("anteil", row.get(f"anteil_{m}"), mlab)
            add("loo", row.get(f"loo_{m}"), mlab)

        # Textspalten als Label mit Wert 1
        ht = row.get("haupttreiber")
        if isinstance(ht, str) and ht:
            add("haupttreiber", 1, {"merkmal": ht})
        ka = row.get("konstant_abweichung")
        if isinstance(ka, str) and ka:
            for m in (s.strip() for s in ka.split(",")):
                add("konstant_abweichung", 1, {"merkmal": m})

    return "\n".join(zeilen) + ("\n" if zeilen else "")


def dataframe_zu_prometheus(
    df: pd.DataFrame,
    labels: dict | None = None,
    prefix: str = "",
    spalten: list | None = None,
    zeit_spalte: str | None = None,
    zeitzone_naiv: str = "UTC",
) -> str:
    """
    Allgemeiner Export: jede Spalte wird eine Metrik (Spaltenname = Metrikname),
    jede Zeile ein Sample mit ihrem Zeitstempel, alle mit denselben globalen Labels.

    labels         globale Labels, z. B. {"anlage": "A1", "linie": "L3"}
    prefix         optionales Präfix für alle Metriknamen, z. B. "sensor_"
    spalten        zu exportierende Spalten (Standard: alle numerischen und booleschen)
    zeit_spalte    Spalte mit Zeitstempel (sonst Index bzw. einzige Datetime-Spalte)
    zeitzone_naiv  Zeitzone für Zeitstempel ohne Zeitzonenangabe

    Nicht-numerische Spalten werden übersprungen (mit Warnung), NaN/inf-Werte ausgelassen,
    boolesche Werte als 0/1 geschrieben. Ungültige Zeichen im Namen werden zu "_".
    """
    ts = zeitstempel_ms(df, zeit_spalte, zeitzone_naiv).astype(str)

    if spalten is None:
        spalten = [c for c in df.columns
                   if c != zeit_spalte and not pd.api.types.is_datetime64_any_dtype(df[c])]
    uebersprungen = [c for c in spalten
                     if not (pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]))]
    if uebersprungen:
        warnings.warn(f"Nicht-numerische Spalten übersprungen: {uebersprungen}", stacklevel=2)
    spalten = [c for c in spalten if c not in uebersprungen]

    namen = {c: _metric_name(f"{prefix}{c}") for c in spalten}
    doppelt = pd.Series(namen).loc[lambda s: s.duplicated(keep=False)]
    if not doppelt.empty:
        raise ValueError(f"Spalten ergeben denselben Metriknamen: {doppelt.to_dict()}")

    lab = _labels(labels) if labels else ""
    zeilen = []
    for c in spalten:
        werte = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(werte)
        kopf = f"{namen[c]}{lab} "
        zeilen.extend(f"{kopf}{v!r} {t}" for v, t in zip(werte[ok].tolist(), ts[ok]))

    return "\n".join(zeilen) + ("\n" if zeilen else "")


# --------------------------------------------------------------------------- Senden

def _senden(texte, url, auth, headers, komprimieren, timeout, verify=True) -> int:
    """Sendet mehrere Textblöcke im Prometheus-Format. Gibt die Anzahl Samples zurück.

    verify  TLS-Prüfung: True, False oder Pfad zu einem CA-Bundle
    """
    h = {"Content-Type": "text/plain", **(headers or {})}
    if komprimieren:
        h["Content-Encoding"] = "gzip"
    anzahl = 0
    with requests.Session() as s:
        for text in texte:
            if not text:
                continue
            body = text.encode("utf-8")
            if komprimieren:
                body = gzip.compress(body)
            r = s.post(url, data=body, headers=h, auth=auth, timeout=timeout, verify=verify)
            r.raise_for_status()
            anzahl += text.count("\n")
    return anzahl


def _bloecke(df: pd.DataFrame, groesse: int):
    for start in range(0, len(df), groesse):
        yield df.iloc[start:start + groesse]


def nach_victoriametrics(
    erg: pd.DataFrame,
    url: str,
    labels: dict,
    auth: tuple | None = None,         # (user, passwort) für Basic Auth
    headers: dict | None = None,       # z. B. {"Authorization": "Bearer <token>"}
    komprimieren: bool = True,
    zeilen_pro_request: int = 5000,    # lange Zeitreihen werden in Blöcken gesendet
    timeout: float = 30,
    verify=True,                       # TLS-Prüfung: True, False oder Pfad zu CA-Bundle
    **kwargs,                          # an ergebnis_zu_prometheus: zeit_spalte, zeitzone_naiv, prefix ...
) -> int:
    """Sendet das Ergebnis von mahalanobis_analyse() an VictoriaMetrics."""
    texte = (ergebnis_zu_prometheus(b, labels, **kwargs) for b in _bloecke(erg, zeilen_pro_request))
    return _senden(texte, url, auth, headers, komprimieren, timeout, verify)


def dataframe_nach_victoriametrics(
    df: pd.DataFrame,
    url: str,
    labels: dict | None = None,
    auth: tuple | None = None,
    headers: dict | None = None,
    komprimieren: bool = True,
    zeilen_pro_request: int = 5000,
    timeout: float = 30,
    verify=True,
    **kwargs,                          # an dataframe_zu_prometheus: prefix, spalten, zeit_spalte, zeitzone_naiv
) -> int:
    """Sendet alle Spalten eines beliebigen DataFrames als Metriken an VictoriaMetrics."""
    texte = (dataframe_zu_prometheus(b, labels, **kwargs) for b in _bloecke(df, zeilen_pro_request))
    return _senden(texte, url, auth, headers, komprimieren, timeout, verify)


def probe_und_ergebnis_nach_victoriametrics(
    erg: pd.DataFrame,
    url: str,
    labels: dict,
    df_probe: pd.DataFrame | None = None,
    probe_prefix: str = "",
    probe_spalten: list | None = None,
    mit_messwert: bool = False,
    prefix: str = "mahalanobis",
    zeit_spalte: str | None = None,
    zeitzone_naiv: str = "UTC",
    auth: tuple | None = None,
    headers: dict | None = None,
    komprimieren: bool = True,
    zeilen_pro_request: int = 5000,
    timeout: float = 30,
    verify=True,
) -> dict:
    """
    Schreibt in einem Aufruf
      1. die Rohdaten der Messprobe (Spaltenname = Metrikname) und
      2. das Analyseergebnis (mahalanobis_* Metriken)
    mit denselben globalen Labels und den Zeitstempeln der Messprobe.

    df_probe       Messprobe; ohne Angabe werden ihre Spalten aus erg übernommen
                   (mahalanobis_analyse hängt seine Spalten hinter die der Messprobe an)
    probe_prefix   optionales Präfix für die Rohdaten-Metriken, z. B. "sensor_"
    probe_spalten  nur diese Spalten der Messprobe schreiben (Standard: alle numerischen)
    mit_messwert   zusätzlich mahalanobis_messwert{merkmal=...} schreiben (Standard: nein,
                   da die Rohdaten bereits als eigene Metriken vorliegen)

    Gibt die Anzahl geschriebener Samples zurück: {"probe": ..., "ergebnis": ...}
    """
    if df_probe is None:
        df_probe = erg.iloc[:, :erg.columns.get_loc("mahalanobis_d2")]

    gemeinsam = dict(url=url, labels=labels, auth=auth, headers=headers, komprimieren=komprimieren,
                     zeilen_pro_request=zeilen_pro_request, timeout=timeout, verify=verify,
                     zeit_spalte=zeit_spalte, zeitzone_naiv=zeitzone_naiv)
    return {
        "probe": dataframe_nach_victoriametrics(df_probe, prefix=probe_prefix,
                                                spalten=probe_spalten, **gemeinsam),
        "ergebnis": nach_victoriametrics(erg, prefix=prefix, mit_messwert=mit_messwert, **gemeinsam),
    }


# --------------------------------------------------------------------------- Beispiel

if __name__ == "__main__":
    from mahalanobis_analyse import mahalanobis_analyse

    rng = np.random.default_rng(42)

    # Referenz-Zeitreihe: 500 Minutenwerte
    zeit_ref = pd.date_range("2026-10-15 00:00", periods=500, freq="min", tz="Europe/Berlin")
    t = rng.normal(80, 5, 500)
    df_ref = pd.DataFrame({"temperatur": t, "druck": 2 * t + rng.normal(0, 2, 500),
                           "drehzahl": rng.normal(1500, 50, 500)}, index=zeit_ref)

    # Messprobe: 3 Zeitpunkte, der Index bleibt im Ergebnis erhalten
    zeit_probe = pd.date_range("2026-10-16 14:30", periods=3, freq="min", tz="Europe/Berlin")
    df_probe = pd.DataFrame({"temperatur": [80.0, 75.0, 80.0],
                             "druck": [160.0, 170.0, 160.0],
                             "drehzahl": [1500.0, 1500.0, 1700.0]}, index=zeit_probe)

    erg = mahalanobis_analyse(df_ref, df_probe)

    # Zum Senden:
    # nach_victoriametrics(erg, "http://localhost:8428/api/v1/import/prometheus",
    #                      labels={"anlage": "A1", "linie": "L3", "modell": "v1"})
    text = ergebnis_zu_prometheus(erg, labels={"anlage": "A1", "linie": "L3", "modell": "v1"})
    print("\n".join(l for l in text.splitlines() if l.startswith("mahalanobis_d{")))
