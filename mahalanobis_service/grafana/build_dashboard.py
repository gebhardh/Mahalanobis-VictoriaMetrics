"""
Erzeugt mahalanobis_dashboard.json (Grafana 10/11, Datenquelle Prometheus-Typ auf VictoriaMetrics).

Aufbau
  Übersicht       Status je Instanz, Anomalie-Anteil, Haupttreiber, Service-Zustand, Zeitleiste
  Analyse         je Instanz wiederholt: Distanz vs. Schwelle, |z|-Verlauf, Beitrag zu D²,
                  Leave-one-out und Kennwerttabelle
  Rohdaten        die analysierten Eingangsgrößen; Anomalien als rote Bereiche (Annotation)

Anpassen: RAW_METRICS unten (Titel, Query, Einheit). $instance wird durch die Dashboard-Variable ersetzt.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
I = 'instance=~"$instance"'

# Rohdaten-Panels: (Titel, Query, Einheit, Min, Max)
RAW_METRICS = [
    ("System-Load (5 min)", f"avg by (instance) (system_load5{{{I}}})", "none", 0, None),
    ("Speicher belegt", f"avg by (instance) (mem_used_percent{{{I}}})", "percent", 0, 100),
    ("Disk I/O Wartezeit", f"avg by (instance) (diskio_io_await{{{I}}})", "ms", 0, None),
    ("Netzwerk empfangen", f"avg by (instance) (rate(net_bytes_recv{{{I}}}[5m]))", "Bps", 0, None),
    ("Netzwerk gesendet", f"avg by (instance) (rate(net_bytes_sent{{{I}}}[5m]))", "Bps", 0, None),
    ("Disk lesen", f"avg by (instance) (rate(diskio_read_bytes{{{I}}}[5m]))", "Bps", 0, None),
    ("Disk schreiben", f"avg by (instance) (rate(diskio_write_bytes{{{I}}}[5m]))", "Bps", 0, None),
]

_next_id = 0


def _id():
    global _next_id
    _next_id += 1
    return _next_id


def target(expr, legend="", ref="A", instant=False, fmt="time_series", interval=""):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
         "range": not instant, "instant": instant, "format": fmt, "editorMode": "code"}
    if interval:
        t["interval"] = interval
    return t


def thresholds(*steps):
    """steps: (farbe, wert) – erster Wert None = Basis."""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def panel(typ, title, x, y, w, h, targets, defaults=None, options=None, overrides=None,
          description="", interval="1m", transformations=None, max_data_points=None):
    p = {
        "id": _id(), "type": typ, "title": title, "description": description,
        "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets, "interval": interval,
        "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
        "options": options or {},
    }
    if transformations:
        p["transformations"] = transformations
    if max_data_points:
        p["maxDataPoints"] = max_data_points
    return p


def row(title, y, repeat=None):
    r = {"id": _id(), "type": "row", "title": title, "collapsed": False,
         "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}
    if repeat:
        r["repeat"] = repeat
    return r


def timeseries_defaults(unit="none", fill=10, stack=False, minv=None, maxv=None, decimals=None):
    d = {"unit": unit, "color": {"mode": "palette-classic"},
         "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": 120000, "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "axisSoftMin": minv}}
    if minv is not None:
        d["min"] = minv
    if maxv is not None:
        d["max"] = maxv
    if decimals is not None:
        d["decimals"] = decimals
    return d


TS_OPTIONS = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
              "tooltip": {"mode": "multi", "sort": "desc"}}


def stat_options(text_mode="value_and_name", color_mode="background"):
    return {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": color_mode, "graphMode": "none", "justifyMode": "auto",
            "orientation": "auto", "textMode": text_mode, "wideLayout": True}


def build():
    panels = []
    y = 0

    # ------------------------------------------------------------------ Übersicht
    panels.append(row("Übersicht", y)); y += 1
    panels.append(panel(
        "stat", "Distanz / Schwelle (aktuell)", 0, y, 7, 5,
        [target(f'last_over_time(mahalanobis_d{{{I}}}[15m]) / last_over_time(mahalanobis_schwelle_d{{{I}}}[15m])',
                "{{instance}}", instant=True)],
        defaults={"unit": "none", "decimals": 2, "color": {"mode": "thresholds"},
                  "thresholds": thresholds(("green", None), ("orange", 1), ("red", 2))},
        options=stat_options(),
        description="< 1: normal, ≥ 1: anomal, ≥ 2: stark anomal (letzter Wert der letzten 15 min)."))
    panels.append(panel(
        "stat", "Anomalie-Anteil letzte Stunde", 7, y, 6, 5,
        [target(f"avg_over_time(mahalanobis_anomalie{{{I}}}[1h])", "{{instance}}", instant=True)],
        defaults={"unit": "percentunit", "decimals": 0, "color": {"mode": "thresholds"},
                  "thresholds": thresholds(("green", None), ("orange", 0.05), ("red", 0.5))},
        options=stat_options(),
        description="Anteil der Messzeitpunkte der letzten Stunde, die als anomal bewertet wurden. "
                    "Beim 99-%-Quantil sind ca. 1 % Fehlalarme normal."))
    panels.append(panel(
        "stat", "Häufigster Haupttreiber (1 h)", 13, y, 6, 5,
        [target(f"topk by (instance) (1, sum by (instance, merkmal) "
                f"(count_over_time(mahalanobis_haupttreiber{{{I}}}[1h])))",
                "{{instance}}: {{merkmal}}", instant=True)],
        defaults={"unit": "m", "decimals": 0, "noValue": "keine Anomalie", "color": {"mode": "fixed", "fixedColor": "orange"}},
        options=stat_options(color_mode="value"),
        description="Merkmal, das in der letzten Stunde am häufigsten den größten Leave-one-out-Effekt hatte "
                    "(Wert = Anzahl anomaler Minuten)."))
    panels.append(panel(
        "stat", "Service: letzter Lauf vor", 19, y, 5, 3,
        [target("max(lag(mahalanobis_service_laufdauer_sekunden[1d]))", "letzter Lauf", instant=True)],
        defaults={"unit": "s", "decimals": 0, "noValue": "keine Daten", "color": {"mode": "thresholds"},
                  "thresholds": thresholds(("green", None), ("orange", 900), ("red", 1800))},
        options=stat_options(text_mode="value"),
        description="Zeit seit dem letzten Lauf von mahalanobis_service.py (MetricsQL lag())."))
    panels.append(panel(
        "stat", "Service: Instanzen", 19, y + 3, 5, 2,
        [target('sum by (status) (last_over_time(mahalanobis_service_instanzen[1h]))', "{{status}}", instant=True)],
        defaults={"unit": "none", "decimals": 0, "color": {"mode": "fixed", "fixedColor": "text"}},
        overrides=[
            {"matcher": {"id": "byName", "options": "fehler"},
             "properties": [{"id": "color", "value": {"mode": "thresholds"}},
                            {"id": "thresholds", "value": thresholds(("green", None), ("red", 1))}]},
            {"matcher": {"id": "byName", "options": "uebersprungen"},
             "properties": [{"id": "color", "value": {"mode": "thresholds"}},
                            {"id": "thresholds", "value": thresholds(("green", None), ("orange", 1))}]},
        ],
        options=stat_options(color_mode="value")))
    y += 5
    panels.append(panel(
        "state-timeline", "Zeitleiste: Distanz relativ zur Schwelle", 0, y, 24, 5,
        [target(f"max_over_time(mahalanobis_d{{{I}}}[$__interval]) / "
                f"max_over_time(mahalanobis_schwelle_d{{{I}}}[$__interval])", "{{instance}}")],
        defaults={"unit": "none", "decimals": 1, "color": {"mode": "thresholds"},
                  "thresholds": thresholds(("green", None), ("orange", 1), ("red", 2)),
                  "custom": {"fillOpacity": 80, "lineWidth": 0}},
        options={"showValue": "never", "mergeValues": False, "rowHeight": 0.8, "alignValue": "left",
                 "legend": {"showLegend": False}, "tooltip": {"mode": "multi"}},
        description="Grün: normal, orange: anomal (D > Schwelle), rot: D ≥ 2× Schwelle."))
    y += 5

    # ------------------------------------------------------------------ Analyse je Instanz
    panels.append(row("Analyse: $instance", y, repeat="instance")); y += 1
    panels.append(panel(
        "timeseries", "Mahalanobis-Distanz und Schwelle", 0, y, 12, 8,
        [target(f"max_over_time(mahalanobis_d{{{I}}}[$__interval])", "Distanz D", "A"),
         target(f"max_over_time(mahalanobis_schwelle_d{{{I}}}[$__interval])", "Schwelle", "B")],
        defaults=timeseries_defaults(fill=15, minv=0, decimals=2),
        overrides=[
            {"matcher": {"id": "byName", "options": "Distanz D"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]},
            {"matcher": {"id": "byName", "options": "Schwelle"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                            {"id": "custom.fillOpacity", "value": 0},
                            {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]},
        ],
        options=TS_OPTIONS,
        description="Maximum der Distanz je Zeitintervall; Werte über der roten Linie sind anomal."))
    panels.append(panel(
        "status-history", "|z| je Merkmal", 12, y, 12, 8,
        [target(f"max_over_time(abs(mahalanobis_z{{{I}}})[$__interval:])", "{{merkmal}}")],
        defaults={"unit": "none", "decimals": 1, "color": {"mode": "thresholds"},
                  "thresholds": thresholds(("green", None), ("yellow", 2), ("red", 3)),
                  "custom": {"fillOpacity": 85, "lineWidth": 0}},
        options={"showValue": "never", "rowHeight": 0.85, "colWidth": 0.95,
                 "legend": {"showLegend": False}, "tooltip": {"mode": "multi"}},
        # Status-History stellt nur begrenzt viele Zeitpunkte dar; Grafana vergröbert $__interval
        # entsprechend, max_over_time(...[$__interval:]) behält dabei die Spitzen.
        max_data_points=100,
        description="Univariate Abweichung je Merkmal. Rot (|z| ≥ 3): Merkmal allein auffällig. "
                    "Ist D anomal, aber alles grün/gelb, liegt ein Korrelationsbruch vor."))
    y += 8
    panels.append(panel(
        "timeseries", "Beitrag zu D² je Merkmal", 0, y, 12, 8,
        [target(f"avg_over_time(mahalanobis_beitrag{{{I}}}[$__interval])", "{{merkmal}}")],
        defaults=timeseries_defaults(fill=60, stack=True, decimals=1),
        options=TS_OPTIONS,
        description="Additive Zerlegung: Die gestapelte Summe ergibt D². Negative Beiträge entstehen, "
                    "wenn korrelierte Merkmale sich gegenseitig 'erklären'."))
    panels.append(panel(
        "bargauge", "Leave-one-out (aktuell)", 12, y, 6, 8,
        [target(f"sort_desc(last_over_time(mahalanobis_loo{{{I}}}[15m]))", "{{merkmal}}", instant=True)],
        defaults={"unit": "none", "decimals": 1, "min": 0, "color": {"mode": "continuous-GrYlRd"}},
        options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                 "valueMode": "color", "namePlacement": "left",
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        description="Um wie viel D² sinkt, wenn das Merkmal weggelassen wird (letzter Wert)."))
    panels.append(panel(
        "table", "Kennwerte je Merkmal (aktuell)", 18, y, 6, 8,
        [target(f"last_over_time(mahalanobis_z{{{I}}}[15m])", "", "A", instant=True, fmt="table"),
         target(f"last_over_time(mahalanobis_anteil{{{I}}}[15m])", "", "B", instant=True, fmt="table"),
         target(f"last_over_time(mahalanobis_loo{{{I}}}[15m])", "", "C", instant=True, fmt="table")],
        defaults={"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
        overrides=[
            {"matcher": {"id": "byName", "options": "z"},
             "properties": [{"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "color", "value": {"mode": "thresholds"}},
                            {"id": "thresholds", "value": thresholds(("red", None), ("yellow", -3), ("green", -2),
                                                                     ("yellow", 2), ("red", 3))}]},
            {"matcher": {"id": "byName", "options": "Anteil an D²"},
             "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "Leave-one-out"},
             "properties": [{"id": "decimals", "value": 1}]},
        ],
        options={"showHeader": True, "cellHeight": "sm",
                 "sortBy": [{"displayName": "Leave-one-out", "desc": True}]},
        transformations=[
            {"id": "merge", "options": {}},
            {"id": "filterFieldsByName",
             "options": {"include": {"names": ["merkmal", "Value #A", "Value #B", "Value #C"]}}},
            {"id": "organize", "options": {"renameByName": {
                "merkmal": "Merkmal", "Value #A": "z", "Value #B": "Anteil an D²", "Value #C": "Leave-one-out"}}},
        ]))
    y += 8

    # ------------------------------------------------------------------ Rohdaten
    panels.append(row("Rohdaten (rote Bereiche = Anomalie)", y)); y += 1
    for i, (titel, expr, unit, minv, maxv) in enumerate(RAW_METRICS):
        x = (i % 3) * 8
        if i and i % 3 == 0:
            y += 7
        panels.append(panel(
            "timeseries", titel, x, y, 8, 7, [target(expr, "{{instance}}")],
            defaults=timeseries_defaults(unit=unit, fill=10, minv=minv, maxv=maxv),
            options=TS_OPTIONS))

    return {
        "__inputs": [],
        "__requires": [{"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
                       {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"}],
        "uid": "mahalanobis-anomalie",
        "title": "Mahalanobis-Anomalieerkennung",
        "description": "Ergebnisse von mahalanobis_service.py und analysierte Rohdaten",
        "tags": ["mahalanobis", "anomalie", "victoriametrics"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,               # Fadenkreuz über alle Panels
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "templating": {"list": [
            {"name": "datasource", "label": "Datenquelle", "type": "datasource", "query": "prometheus",
             "current": {}, "hide": 0, "refresh": 1, "regex": ""},
            {"name": "instance", "label": "Instanz", "type": "query", "datasource": DS,
             "query": {"query": "label_values(mahalanobis_d, instance)", "refId": "instance"},
             "definition": "label_values(mahalanobis_d, instance)",
             "multi": True, "includeAll": True, "current": {"text": ["All"], "value": ["$__all"]},
             "refresh": 2, "sort": 1, "hide": 0},
        ]},
        "annotations": {"list": [
            {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
             "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"},
            {"name": "Anomalien", "datasource": DS, "enable": True, "iconColor": "rgba(242, 73, 92, 0.25)",
             "expr": f"max by (instance) (mahalanobis_anomalie{{{I}}}) == 1",
             "target": {"expr": f"max by (instance) (mahalanobis_anomalie{{{I}}}) == 1", "refId": "Anno",
                        "interval": "1m"},
             "step": "1m", "titleFormat": "Anomalie", "textFormat": "{{instance}}", "tagKeys": "instance",
             "useValueForTime": False},
        ]},
        "panels": panels,
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("mahalanobis_dashboard.json")
    out.write_text(json.dumps(build(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{out} geschrieben")
