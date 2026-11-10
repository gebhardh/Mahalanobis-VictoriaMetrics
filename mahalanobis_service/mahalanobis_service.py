#!/usr/bin/env python3
"""
Mahalanobis-Anomalie-Service für VictoriaMetrics.

Benötigt Python >= 3.8.

Ablauf je Lauf (alle `schedule.interval`):
  1. Für jedes Merkmal die PromQL-Query aus der Config per /api/v1/query_range lesen
     (Messfenster seit dem letzten Lauf und – gecacht – das Referenzfenster).
  2. Die Ergebnisse nach den `group_labels` (Standard: instance) gruppieren:
     ein DataFrame je Instanz, eine Spalte je Merkmal.
  3. Je Instanz mahalanobis_analyse(df_ref, df_probe) ausführen.
  4. Ergebnis mit den Labels aus `output.labels` plus den Gruppen-Labels
     (instance aus dem Query-Ergebnis) nach VictoriaMetrics schreiben.

Aufruf:
  python mahalanobis_service.py -c config.yml            # Dauerbetrieb
  python mahalanobis_service.py -c config.yml --once     # ein Lauf (Cron, Test)
  python mahalanobis_service.py -c config.yml --once --dry-run   # nichts schreiben, nur anzeigen
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml

from mahalanobis_analyse import bereinige_referenz, mahalanobis_analyse
from vm_export import (
    _labels,
    _senden,
    dataframe_nach_victoriametrics,
    dataframe_zu_prometheus,
    ergebnis_zu_prometheus,
    nach_victoriametrics,
)

log = logging.getLogger("mahalanobis_service")


# =========================================================================== Konfiguration

_DAUER_TEIL = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")
_DAUER_GANZ = re.compile(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w))+")
_FAKTOR = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def dauer_s(wert) -> float:
    """'90s', '5m', '1h30m', '7d' oder Zahl (Sekunden) -> Sekunden."""
    if isinstance(wert, (int, float)):
        return float(wert)
    text = str(wert).strip()
    if not _DAUER_GANZ.fullmatch(text):
        raise ValueError(f"Ungültige Dauer: {wert!r} (Beispiele: 30s, 5m, 1h, 7d)")
    return sum(float(z) * _FAKTOR[e] for z, e in _DAUER_TEIL.findall(text))


STANDARD = {
    "victoriametrics": {
        "read_url": "http://localhost:8428",
        "write_url": "http://localhost:8428/api/v1/import/prometheus",
        "username": None,
        "password": None,
        "bearer_token": None,
        "timeout": "60s",
        "verify_tls": True,
        "max_points_per_query": 10000,
    },
    "schedule": {
        "interval": "5m",
        "step": "1m",
        "delay": "1m",
        "max_catchup": "1h",
        "state_file": None,
    },
    "reference": {
        "window": "7d",
        "end_offset": "1h",
        "start": None,
        "end": None,
        "step": None,          # Standard: schedule.step
        "refresh": "1h",
        "min_points": 100,
        "include_recent": 0,   # Referenz je Zeitpunkt t um t-1 ... t-N ergänzen (0 = aus)
        "clean_quantile": None,  # iterative Bereinigung der Referenz, z. B. 0.999 (None = aus)
        "clean_iterations": 3,
    },
    "analysis": {"quantil": 0.99, "schwelle": "chi2", "robust": False, "support_fraction": None},
    "group_labels": ["instance"],
    "metrics": {},
    "output": {
        "labels": {},
        "prefix": "mahalanobis",
        "mit_messwert": False,
        "write_input": False,
        "input_prefix": "mahalanobis_input_",
        "service_metrics": True,
    },
}


def _zusammenfuehren(basis: dict, neu: dict) -> dict:
    out = dict(basis)
    for k, v in (neu or {}).items():
        if isinstance(v, dict) and isinstance(basis.get(k), dict) and k not in ("metrics", "labels"):
            out[k] = _zusammenfuehren(basis[k], v)
        else:
            out[k] = v
    return out


def lade_config(pfad: str) -> dict:
    """Liest die YAML-Config; ${VAR} wird durch Umgebungsvariablen ersetzt (für Passwörter)."""
    text = os.path.expandvars(Path(pfad).read_text(encoding="utf-8"))
    # Kopie der Standardwerte: sonst würden Abschnitte, die in der Datei fehlen, die
    # Standardwerte selbst referenzieren und spätere Änderungen an cfg dorthin durchschlagen
    cfg = _zusammenfuehren(copy.deepcopy(STANDARD), yaml.safe_load(text) or {})

    if not isinstance(cfg["metrics"], dict) or not cfg["metrics"]:
        raise ValueError("config: 'metrics' muss mindestens ein Merkmal mit PromQL-Query enthalten.")
    for name, q in cfg["metrics"].items():
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"config: Query für Merkmal {name!r} fehlt.")
    if not cfg["group_labels"]:
        raise ValueError("config: 'group_labels' darf nicht leer sein.")
    konflikt = set(cfg["output"]["labels"]) & set(cfg["group_labels"])
    if konflikt:
        raise ValueError(f"config: output.labels darf {sorted(konflikt)} nicht enthalten – "
                         "diese Labels werden aus dem Query-Ergebnis übernommen.")
    if (cfg["reference"]["start"] is None) != (cfg["reference"]["end"] is None):
        raise ValueError("config: reference.start und reference.end nur gemeinsam angeben.")

    # Dauern in Sekunden umrechnen
    for abschnitt, schluessel in [("schedule", ["interval", "step", "delay", "max_catchup"]),
                                  ("reference", ["window", "end_offset", "refresh"]),
                                  ("victoriametrics", ["timeout"])]:
        for s in schluessel:
            cfg[abschnitt][s] = dauer_s(cfg[abschnitt][s])
    ref = cfg["reference"]
    ref["step"] = dauer_s(ref["step"]) if ref["step"] is not None else cfg["schedule"]["step"]
    n = ref["include_recent"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"config: reference.include_recent muss eine ganze Zahl >= 0 sein, nicht {n!r}.")
    q = ref["clean_quantile"]
    if q is not None and (isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 < q < 1):
        raise ValueError(f"config: reference.clean_quantile muss zwischen 0 und 1 liegen (z. B. 0.999), nicht {q!r}.")
    it = ref["clean_iterations"]
    if isinstance(it, bool) or not isinstance(it, int) or it < 1:
        raise ValueError(f"config: reference.clean_iterations muss eine ganze Zahl >= 1 sein, nicht {it!r}.")
    if cfg["schedule"]["interval"] < cfg["schedule"]["step"]:
        raise ValueError("config: schedule.interval muss >= schedule.step sein.")
    return cfg


# =========================================================================== VictoriaMetrics lesen

class VMClient:
    def __init__(self, cfg: dict):
        vm = cfg["victoriametrics"]
        self.read_url = vm["read_url"].rstrip("/")
        self.write_url = vm["write_url"]
        self.timeout = vm["timeout"]
        self.verify = vm["verify_tls"]
        self.max_points = int(vm["max_points_per_query"])
        self.auth = (vm["username"], vm["password"] or "") if vm["username"] else None
        self.headers = {"Authorization": f"Bearer {vm['bearer_token']}"} if vm["bearer_token"] else {}
        self.session = requests.Session()

    def query_range(self, query: str, start: float, end: float, step: float) -> list[tuple[dict, np.ndarray, np.ndarray]]:
        """
        Führt /api/v1/query_range aus, bei langen Zeiträumen in mehreren Teilabfragen.
        Gibt je Zeitreihe (labels, zeitstempel_s, werte) zurück.
        """
        serien: dict[str, list] = {}
        teil_start = start
        while teil_start <= end:
            teil_ende = min(end, teil_start + (self.max_points - 1) * step)
            r = self.session.get(
                f"{self.read_url}/api/v1/query_range",
                params={"query": query, "start": teil_start, "end": teil_ende, "step": f"{step:g}s"},
                auth=self.auth, headers=self.headers, timeout=self.timeout, verify=self.verify,
            )
            if r.status_code != 200:
                raise RuntimeError(f"query_range HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if data.get("status") != "success":
                raise RuntimeError(f"query_range Fehler: {data.get('error', data)}")
            if data["data"]["resultType"] != "matrix":
                raise RuntimeError(f"Query liefert {data['data']['resultType']} statt matrix: {query}")
            for s in data["data"]["result"]:
                labels = s["metric"]
                schluessel = json.dumps(labels, sort_keys=True)
                eintrag = serien.setdefault(schluessel, [labels, [], []])
                for ts, wert in s["values"]:
                    eintrag[1].append(float(ts))
                    eintrag[2].append(float(wert))      # "NaN", "+Inf" werden zu float
            teil_start = teil_ende + step
        return [(lab, np.asarray(ts), np.asarray(w)) for lab, ts, w in serien.values()]


# =========================================================================== Service

def _ausrichten(t: float, step: float) -> float:
    return math.floor(t / step) * step


def _schluessel_text(schluessel: tuple, group_labels: list) -> str:
    return ",".join(f"{l}={v}" for l, v in zip(group_labels, schluessel))


class MahalanobisService:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.vm = VMClient(cfg)
        self.merkmale = list(cfg["metrics"])
        self.group_labels = list(cfg["group_labels"])
        self.sch = cfg["schedule"]
        self.ref_cfg = cfg["reference"]
        self.out = cfg["output"]
        self.ref_cache: dict[tuple, pd.DataFrame] = {}
        self.ref_geladen_um: float | None = None
        self.ref_ende: dict[tuple, pd.Timestamp] = {}   # Ende des Referenzzeitraums je Instanz (vor Bereinigung)
        self.state_pfad = Path(self.sch["state_file"]) if self.sch["state_file"] else None
        self.state: dict[str, float] = self._state_laden()

    # ------------------------------------------------------------------ Zustand

    def _state_laden(self) -> dict:
        if self.state_pfad and self.state_pfad.exists():
            try:
                return {k: float(v) for k, v in json.loads(self.state_pfad.read_text()).items()}
            except Exception:
                log.exception("State-Datei %s unlesbar, starte ohne Zustand", self.state_pfad)
        return {}

    def _state_speichern(self):
        if self.state_pfad and not self.dry_run:
            tmp = self.state_pfad.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
            tmp.replace(self.state_pfad)

    # ------------------------------------------------------------------ Lesen

    def lade(self, start: float, end: float, step: float) -> dict[tuple, pd.DataFrame]:
        """Liest alle Merkmal-Queries und baut ein DataFrame je Instanz (Gruppen-Label-Kombination)."""
        daten: dict[tuple, dict[str, pd.Series]] = {}
        mehrdeutig: set[tuple] = set()
        for merkmal, query in self.cfg["metrics"].items():
            ohne_label = 0
            for labels, ts, werte in self.vm.query_range(query, start, end, step):
                schluessel = tuple(labels.get(l) for l in self.group_labels)
                if None in schluessel:
                    ohne_label += 1
                    continue
                spalten = daten.setdefault(schluessel, {})
                if merkmal in spalten:
                    if schluessel not in mehrdeutig:
                        log.error(
                            "%s: Query für %r liefert mehrere Zeitreihen – Instanz übersprungen. "
                            "Bitte in der Query mit 'by (%s)' auf eine Zeitreihe je Instanz aggregieren.",
                            _schluessel_text(schluessel, self.group_labels), merkmal,
                            ", ".join(self.group_labels))
                    mehrdeutig.add(schluessel)
                    continue
                spalten[merkmal] = pd.Series(werte, index=pd.to_datetime(ts, unit="s", utc=True))
        for schluessel in mehrdeutig:
            daten.pop(schluessel, None)
            if ohne_label:
                log.warning("Query für %r: %d Zeitreihe(n) ohne Label(s) %s ignoriert",
                            merkmal, ohne_label, self.group_labels)

        frames = {}
        for schluessel, spalten in daten.items():
            fehlend = [m for m in self.merkmale if m not in spalten]
            if fehlend:
                log.warning("%s: keine Daten für %s – Instanz übersprungen",
                            _schluessel_text(schluessel, self.group_labels), fehlend)
                continue
            df = pd.DataFrame(spalten)[self.merkmale].replace([np.inf, -np.inf], np.nan)
            frames[schluessel] = df.sort_index()
        return frames

    def referenz(self, jetzt: float) -> dict[tuple, pd.DataFrame]:
        """Referenzdaten je Instanz, gecacht und alle reference.refresh neu geladen."""
        fest = self.ref_cfg["start"] is not None
        faellig = (self.ref_geladen_um is None
                   or (not fest and jetzt - self.ref_geladen_um >= self.ref_cfg["refresh"]))
        if not faellig:
            return self.ref_cache

        step = self.ref_cfg["step"]
        if fest:
            start = pd.Timestamp(self.ref_cfg["start"]).timestamp()
            end = pd.Timestamp(self.ref_cfg["end"]).timestamp()
        else:
            end = _ausrichten(jetzt - self.ref_cfg["end_offset"], step)
            start = end - self.ref_cfg["window"]
        try:
            geladen = self.lade(start, end, step)
            log.info("Referenz geladen: %s bis %s, %d Instanz(en)",
                     pd.Timestamp(start, unit="s", tz="UTC"), pd.Timestamp(end, unit="s", tz="UTC"),
                     len(geladen))
            self.ref_ende = {k: df.index.max() for k, df in geladen.items() if len(df)}
            if self.ref_cfg["clean_quantile"] is not None:
                geladen = {k: self._bereinigen(k, df) for k, df in geladen.items()}
            self.ref_cache = geladen
            self.ref_geladen_um = jetzt
        except Exception:
            if not self.ref_cache:
                raise
            log.exception("Referenz konnte nicht neu geladen werden, verwende bisherige")
        return self.ref_cache

    def _bereinigen(self, schluessel: tuple, df: pd.DataFrame) -> pd.DataFrame:
        """Entfernt deutliche Anomalien aus der Referenz einer Instanz (reference.clean_quantile)."""
        name = _schluessel_text(schluessel, self.group_labels)
        a = self.cfg["analysis"]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sauber, info = bereinige_referenz(
                    df, self.merkmale, quantil=self.ref_cfg["clean_quantile"],
                    iterationen=self.ref_cfg["clean_iterations"],
                    robust=a["robust"], support_fraction=a["support_fraction"])
        except Exception:
            log.exception("%s: Bereinigung der Referenz fehlgeschlagen, verwende unbereinigte Referenz", name)
            return df
        entfernt = info["vorher"] - info["nachher"]
        anteil = entfernt / info["vorher"] if info["vorher"] else 0.0
        (log.warning if anteil > 0.1 else log.info)(
            "%s: Referenz bereinigt, %d von %d Punkten entfernt (%.1f %%, %d Iteration(en))%s",
            name, entfernt, info["vorher"], 100 * anteil, info["iterationen"],
            " – ungewöhnlich viel, Referenzzeitraum prüfen" if anteil > 0.1 else "")
        return sauber

    # ------------------------------------------------------------------ Schreiben

    def _schreiben(self, erg: pd.DataFrame, df_probe: pd.DataFrame, labels: dict) -> int:
        vm = self.vm
        gemeinsam = dict(auth=vm.auth, headers=vm.headers, timeout=vm.timeout, verify=vm.verify)
        if self.dry_run:
            text = ergebnis_zu_prometheus(erg, labels, prefix=self.out["prefix"],
                                          mit_messwert=self.out["mit_messwert"])
            if self.out["write_input"]:
                text += dataframe_zu_prometheus(df_probe, labels, prefix=self.out["input_prefix"])
            zeilen = text.splitlines()
            log.info("[dry-run] %d Samples, z. B.:\n  %s", len(zeilen), "\n  ".join(zeilen[:5]))
            return len(zeilen)

        n = nach_victoriametrics(erg, vm.write_url, labels, prefix=self.out["prefix"],
                                 mit_messwert=self.out["mit_messwert"], **gemeinsam)
        if self.out["write_input"]:
            n += dataframe_nach_victoriametrics(df_probe, vm.write_url, labels,
                                                prefix=self.out["input_prefix"], **gemeinsam)
        return n

    def _service_metriken(self, statistik: dict, dauer: float, ts_ms: int):
        if not self.out["service_metrics"]:
            return
        p = self.out["prefix"]
        basis = dict(self.out["labels"])
        zeilen = [f"{p}_service_instanzen{_labels({**basis, 'status': s})} {n} {ts_ms}"
                  for s, n in statistik.items()]
        zeilen.append(f"{p}_service_laufdauer_sekunden{_labels(basis) if basis else ''} {dauer:.3f} {ts_ms}")
        text = "\n".join(zeilen) + "\n"
        if self.dry_run:
            log.info("[dry-run] Service-Metriken:\n  %s", "\n  ".join(zeilen))
            return
        try:
            _senden([text], self.vm.write_url, self.vm.auth, self.vm.headers, True,
                    self.vm.timeout, self.vm.verify)
        except Exception:
            log.exception("Service-Metriken konnten nicht geschrieben werden")

    # ------------------------------------------------------------------ Analyse

    def _analysieren(self, df_ref: pd.DataFrame, df_voll: pd.DataFrame, df_probe: pd.DataFrame,
                     ref_ende: pd.Timestamp | None = None) -> pd.DataFrame:
        """
        Führt mahalanobis_analyse() aus.

        include_recent = 0: eine Analyse für alle Zeitpunkte gegen dieselbe Referenz.
        include_recent = N: jeder Zeitpunkt t wird gegen eine eigene Referenz bewertet, nämlich
          die Referenzdaten plus die vorhandenen Messpunkte im Zeitraum [t - N*step, t - step],
          die nicht schon in der Referenz liegen. Fehlende Vorgänger (Lücken, fehlende Werte)
          werden ausgelassen, die Referenz ist dann entsprechend kleiner ergänzt.

        df_voll  alle gelesenen, vollständigen Messpunkte der Instanz inkl. Vorlauf
        ref_ende Ende des Referenzzeitraums; Vorgänger bis dahin gehören zur Referenz (auch wenn
                 sie bei der Bereinigung entfernt wurden) und werden nicht ergänzt
        """
        a = self.cfg["analysis"]
        kw = dict(merkmale=self.merkmale, quantil=a["quantil"], schwelle=a["schwelle"],
                  robust=a["robust"], support_fraction=a["support_fraction"])
        n = self.ref_cfg["include_recent"]
        if n == 0:
            return mahalanobis_analyse(df_ref, df_probe, **kw)

        step = pd.Timedelta(seconds=self.sch["step"])
        if ref_ende is None:
            ref_ende = df_ref.index.max()
        teile = []
        for ts in df_probe.index:
            vorgaenger = df_voll[(df_voll.index >= ts - n * step) & (df_voll.index < ts)
                                 & (df_voll.index > ref_ende)]
            teile.append(mahalanobis_analyse(pd.concat([df_ref, vorgaenger]), df_probe.loc[[ts]], **kw))
        return pd.concat(teile)

    # ------------------------------------------------------------------ Ein Lauf

    def lauf(self, jetzt: float | None = None) -> dict:
        t0 = time.time()
        jetzt = time.time() if jetzt is None else jetzt
        step = self.sch["step"]
        ende = _ausrichten(jetzt - self.sch["delay"], step)
        frueheste = ende - self.sch["max_catchup"]
        standard_start = ende - self.sch["interval"] + step

        # Instanzen, die länger als max_catchup nicht verarbeitet wurden, vergessen
        self.state = {k: v for k, v in self.state.items() if v >= frueheste}
        # ab dem ältesten noch nicht verarbeiteten Zeitpunkt lesen; ohne Zustand: ein Intervall.
        # Neue Instanzen werden ab diesem Start mitverarbeitet.
        start = min(v + step for v in self.state.values()) if self.state else standard_start
        start = max(start, frueheste)
        statistik = {"ok": 0, "uebersprungen": 0, "fehler": 0}
        if start > ende:
            log.debug("Nichts zu tun (start > ende)")
            return statistik

        log.info("Lauf: Messfenster %s bis %s",
                 pd.Timestamp(start, unit="s", tz="UTC"), pd.Timestamp(ende, unit="s", tz="UTC"))
        referenz = self.referenz(jetzt)
        # bei include_recent = N zusätzlich N Zeitpunkte vor dem Messfenster lesen,
        # damit auch der erste Zeitpunkt seine Vorgänger t-1 ... t-N hat
        vorlauf = self.ref_cfg["include_recent"] * step
        proben = self.lade(start - vorlauf, ende, step)

        for schluessel, df in proben.items():
            name = _schluessel_text(schluessel, self.group_labels)
            try:
                # nur Zeitpunkte nach dem letzten erfolgreich verarbeiteten dieser Instanz
                ab = self.state.get(name)
                grenze = pd.Timestamp(ab + step if ab is not None else standard_start, unit="s", tz="UTC")
                df_probe = df[df.index >= grenze]
                vorher = len(df_probe)
                df_probe = df_probe.dropna()
                if vorher - len(df_probe):
                    log.debug("%s: %d Zeitpunkte mit fehlenden Werten ignoriert", name, vorher - len(df_probe))

                df_ref = referenz.get(schluessel)
                n_ref = 0 if df_ref is None else len(df_ref.dropna())
                if n_ref < self.ref_cfg["min_points"]:
                    log.warning("%s: nur %d vollständige Referenzpunkte (min_points=%d) – übersprungen",
                                name, n_ref, self.ref_cfg["min_points"])
                    statistik["uebersprungen"] += 1
                    self.state[name] = ende
                    continue

                if df_probe.empty:
                    self.state[name] = ende
                    statistik["ok"] += 1
                    continue

                # Bewertete Zeitpunkte nie in ihrer eigenen Referenz: Überlappen sich Referenz und
                # Messfenster (z. B. beim Nachholen mit max_catchup >= end_offset), wird die Referenz
                # vor dem ersten bewerteten Zeitpunkt abgeschnitten.
                ref_ende = self.ref_ende.get(schluessel, df_ref.index.max())
                if ref_ende >= df_probe.index.min():
                    df_ref = df_ref[df_ref.index < df_probe.index.min()]
                    ref_ende = df_probe.index.min() - pd.Timedelta(1, "ns")
                    log.debug("%s: Referenz überlappt das Messfenster, bis %s gekürzt", name, df_probe.index.min())

                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    erg = self._analysieren(df_ref, df.dropna(), df_probe, ref_ende)
                for meldung in dict.fromkeys(str(x.message) for x in w):   # jede Meldung einmal
                    log.warning("%s: %s", name, meldung)

                labels = {**self.out["labels"], **dict(zip(self.group_labels, schluessel))}
                n = self._schreiben(erg, df_probe, labels)
                self.state[name] = ende
                statistik["ok"] += 1
                log.info("%s: %d Zeitpunkte, %d anomal, %d Samples geschrieben",
                         name, len(erg), int(erg["anomalie"].sum()), n)
            except Exception:
                statistik["fehler"] += 1
                log.exception("%s: Analyse/Schreiben fehlgeschlagen (wird im nächsten Lauf wiederholt)", name)

        self._state_speichern()
        self._service_metriken(statistik, time.time() - t0, int(ende * 1000))
        log.info("Lauf fertig in %.1f s: %s", time.time() - t0, statistik)
        return statistik

    # ------------------------------------------------------------------ Dauerbetrieb

    def laufen(self, stop: threading.Event):
        intervall = self.sch["interval"]
        while not stop.is_set():
            try:
                self.lauf()
            except Exception:
                log.exception("Lauf fehlgeschlagen")
            # auf den nächsten Intervall-Zeitpunkt warten (z. B. alle 5 min zur vollen 5. Minute)
            naechster = (math.floor(time.time() / intervall) + 1) * intervall
            stop.wait(max(1.0, naechster - time.time()))


# =========================================================================== Einstieg

def main(argv=None):
    p = argparse.ArgumentParser(description="Mahalanobis-Anomalie-Service für VictoriaMetrics")
    p.add_argument("-c", "--config", required=True, help="Pfad zur YAML-Konfiguration")
    p.add_argument("--once", action="store_true", help="nur einen Lauf ausführen")
    p.add_argument("--dry-run", action="store_true", help="nichts schreiben, Ergebnis nur protokollieren")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = lade_config(args.config)
    service = MahalanobisService(cfg, dry_run=args.dry_run)
    log.info("Gestartet: %d Merkmal(e) %s, Gruppierung nach %s, Intervall %gs",
             len(service.merkmale), service.merkmale, service.group_labels, cfg["schedule"]["interval"])
    if cfg["reference"]["include_recent"]:
        log.info("Referenz wird je Zeitpunkt t um die vorhergehenden Messpunkte t-1 ... t-%d ergänzt",
                 cfg["reference"]["include_recent"])

    if args.once:
        try:
            stat = service.lauf()
        except Exception as e:
            log.error("Lauf fehlgeschlagen: %s", e, exc_info=log.isEnabledFor(logging.DEBUG))
            return 2
        return 1 if stat["fehler"] else 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (log.info("Beende ..."), stop.set()))
    service.laufen(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
