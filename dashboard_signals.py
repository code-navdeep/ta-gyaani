"""
Signal registry — dashboard wiring for the locked-calculator generation.

Each Signal binds a calculator's typed emission (`row_class` from
`calculators/schemas.py`) to its dashboard-facing surfaces:

    - merge behavior (field_renames, multi_emission)   → get_current_metrics
    - detail panel card                                → create_detail_panel
    - chart(s) in the drawer                           → build_charts
    - row-highlight rule                               → style_data_conditional
    - health-bar label                                 → update_health_bar

Adding a future calculator requires:
    1. Define its Row dataclass in `calculators/schemas.py`.
    2. Add its `_CONSUMED_FIELDS` names to `dashboard.py`.
    3. Register a new `Signal(...)` here.
"""
import json

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
from dash import html

from calculators.schemas import (
    MomentumRow, BreakoutRow, VwapRow, RelativeStrengthRow,
    VolumeBlockRow, VolumeDynamicsRow, PriceVolumeDivergenceRow,
    PriceLevelsRow, RollingMomentumRow,
)


# ============================================================================
# Config knobs consumed by this module — single deref site at import. Loaded
# independently of dashboard.py to avoid a circular import.
# ============================================================================
_APP_CONFIG: Dict[str, Any] = {}
try:
    with open('config.json', 'r') as _f:
        _APP_CONFIG = json.load(_f)
except Exception:
    pass

_Z_HIGHLIGHT: float = float(_APP_CONFIG.get('alerts', {}).get('zscore_highlight_threshold', 2.0))


# ============================================================================
# Shared card styles.
# ============================================================================

H5_STYLE = {
    'fontSize': '12px',
    'letterSpacing': '1.6px',
    'textTransform': 'uppercase',
    'marginTop': '0',
    'marginBottom': '12px',
    'paddingBottom': '6px',
    'color': '#f5e9d3',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontWeight': '500',
    'borderBottom': '1px dashed #3a3128',
}
P_STYLE = {
    'fontSize': '13px',
    'margin': '5px 0',
    'color': '#f5e9d3',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'lineHeight': '1.5',
}


def _fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "N/A"


def _fmt_z(v):
    return f"{v:+.2f}" if v is not None else "N/A"


def _fmt_scalar(v):
    return f"{v:+.3f}" if v is not None else "N/A"


def _fmt_int(v):
    # "N/A", not "0": an uncomputed count is unknown, and "0" reads as "no history".
    return f"{int(v)}" if v is not None else "N/A"


def _fmt_price(v):
    return f"₹{v:.2f}" if v is not None else "N/A"


def _fmt_log_pct(v):
    """A log-return field displayed as an approximate percent."""
    return f"{v * 100:+.2f}%" if v is not None else "N/A"


_TIERS = ('22d', '90d', '180d', '252d')


# ============================================================================
# Detail-panel layout system — uniform 5-column grid.
# ============================================================================

_GRID_ROW_STYLE = {
    'display': 'grid',
    'gridTemplateColumns': 'repeat(5, 1fr)',
    'columnGap': '0',
    'alignItems': 'center',
    'padding': '6px 0',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontSize': '13px',
    'color': '#f5e9d3',
    'fontVariantNumeric': 'tabular-nums',
}

_GRID_CELL_STYLE = {
    'textAlign': 'center',
    'padding': '4px 12px',
}

_GRID_HEADER_ROW_STYLE = {
    **_GRID_ROW_STYLE,
    'fontSize': '11px',
    'letterSpacing': '0.5px',
    'textTransform': 'uppercase',
    'color': '#8a7864',
    'borderBottom': '1px solid #3a3128',
    'paddingBottom': '6px',
    'marginBottom': '4px',
    'marginTop': '10px',
}

# ---- Tier emphasis -------------------------------------------------------- #
#
# The governing rule for the tier toggle: it COLLAPSES where a surface can hold
# exactly one number (a table cell, a glyph) and EMPHASISES where it can hold
# four (charts, and these tier grids).
#
# The drawer keeps showing all four tiers — it is the surface where cross-tier
# divergence is visible, and collapsing it to one tier would destroy the only
# place that divergence can be read. Emphasis satisfies both halves of "see all
# the information, with the toggle governing it".
#
# Same treatment the charts already use (_TIER_EMPHASIS / _TIER_MUTED), rendered
# for text rather than for line weight.
_GRID_ACTIVE_ROW_STYLE = {
    **_GRID_ROW_STYLE,
    'color': '#f5e9d3',
    'fontWeight': '500',
    'backgroundColor': '#2a2119',
    'borderRadius': '3px',
}

_GRID_MUTED_ROW_STYLE = {
    **_GRID_ROW_STYLE,
    'color': '#8a7864',
}

_GRID_TITLE_ROW_STYLE = {
    'display': 'grid',
    'gridTemplateColumns': 'repeat(5, 1fr)',
    'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
    'fontSize': '11px',
    'letterSpacing': '1.4px',
    'textTransform': 'uppercase',
    'color': '#8a7864',
    'paddingTop': '14px',
    'paddingBottom': '6px',
    'marginBottom': '4px',
}


def _grid_row(cells, *, is_header: bool = False, is_title: bool = False,
              is_active: Optional[bool] = None):
    """Single layout primitive — five equal-width columns.

    cells: list of exactly 5 items. Each is a str, an html element, or None
           (empty cell). is_header: uppercase muted caption row. is_title:
           single centered title spanning all 5 columns (cells[0] only).

    is_active drives tier emphasis and is THREE-valued on purpose:
        None  -> neutral (no tier selected, or not a tier row) — the default,
                 so every pre-existing caller renders exactly as before
        True  -> this is the selected tier's row: full contrast, lifted
        False -> a tier row that is not selected: muted
    """
    if is_title:
        title_text = cells[0] if cells else ''
        return html.Div(
            html.Div(title_text, style={'textAlign': 'center', 'gridColumn': '1 / -1'}),
            style=_GRID_TITLE_ROW_STYLE,
        )

    if is_header:
        row_style = _GRID_HEADER_ROW_STYLE
    elif is_active is True:
        row_style = _GRID_ACTIVE_ROW_STYLE
    elif is_active is False:
        row_style = _GRID_MUTED_ROW_STYLE
    else:
        row_style = _GRID_ROW_STYLE
    if len(cells) != 5:
        raise ValueError(f"_grid_row: expected 5 cells, got {len(cells)}")

    rendered = []
    for cell in cells:
        if cell is None:
            rendered.append(html.Div(style=_GRID_CELL_STYLE))
        elif isinstance(cell, (str, int, float)):
            rendered.append(html.Div(str(cell), style=_GRID_CELL_STYLE))
        else:
            rendered.append(html.Div(cell, style=_GRID_CELL_STYLE))
    return html.Div(rendered, style=row_style)


_SIGNAL_CARD_STYLE = {
    'padding': '18px 22px 16px 22px',
    'backgroundColor': '#231b15',
    'marginBottom': '16px',
    'borderTop': '1px solid #3a3128',
}


def _signal_card(name: str, accent: str, children: list):
    """Uniform card wrapper. `accent` is a semantic border color from
    green/red/amber; anything else → neutral."""
    _accent_map = {
        '#5fb87c': '#5fb87c',
        '#d94560': '#d94560',
        '#e8a447': '#e8a447',
    }
    top = _accent_map.get(accent, '#3a3128')
    return html.Div(
        [html.H5(name, style=H5_STYLE), *children],
        style={**_SIGNAL_CARD_STYLE, 'borderTop': f'2px solid {top}'},
    )


#: Minimum half-height of a z-chart's y-axis. Keeps a quiet chart on a steady
#: [-3.5, 3.5] frame, so the unusual-z shading band is a stable visual reference
#: rather than one that resizes under the trader as data arrives.
_Z_AXIS_FLOOR = 3.5

#: Headroom above the largest plotted |z| so an extreme line does not sit flush
#: against the frame edge.
_Z_AXIS_HEADROOM = 1.1


def _z_yaxis_range(frame, columns):
    """Symmetric y-range for a z-score chart: autoscaled, with a floor.

        [-max(3.5, max|z| * 1.1), +same]

    These four charts previously hard-clipped to [-3.5, 3.5]. Measured against
    real stored history — 344,722 z-scores across 2,050 (asset, block) partitions
    — that discarded 2,788 observations (0.81%), up to |z| = 12.88: roughly 16
    events per trading day, drawn as a line vanishing off the top of the frame
    with no way to tell 3.6 from 12.9. Those are by construction the most
    tradeable readings in the dataset, and suppressing them is the opposite of
    what these charts are for.

    Symmetric rather than fitted to the data's own min/max so zero stays centred
    and the unusual-z shading (from the z-slider's value outward) keeps its
    meaning. Floored rather than purely
    autoscaled so a flat, unremarkable chart does not zoom in on noise and
    manufacture the appearance of movement.
    """
    peak = 0.0
    for col in columns:
        if frame is None or col not in frame.columns:
            continue
        # to_numeric first: a tier column that is entirely NULL comes back from
        # metrics.db as object dtype (the frames are built from dicts), and
        # .abs() raises TypeError on it. That is not a rare case — it is the
        # normal state of the 180d/252d columns until enough history accrues.
        series = pd.to_numeric(frame[col], errors='coerce').abs()
        if series.notna().any():
            peak = max(peak, float(series.max()))
    bound = max(_Z_AXIS_FLOOR, peak * _Z_AXIS_HEADROOM)
    return [-bound, bound]


def _std_tier_cells(row, prefix, tier):
    """Return the 5-cell list for a standard tier row: [Tier, z, μ, σ, n]."""
    def _f(v):
        return f"{v:+.2f}" if v is not None else "N/A"
    z   = row.get(f'raw_{prefix}_z_{tier}')
    avg = row.get(f'raw_{prefix}_avg_{tier}')
    std = row.get(f'raw_{prefix}_std_{tier}')
    n   = row.get(f'raw_{prefix}_data_points_{tier}')
    return [tier, _f(z), _f(avg), _f(std), _fmt_int(n)]


#: Cross-tier divergence — the short tier, and the long tiers in preference
#: order. 252d needs roughly a year of same-block history, so it is NULL for
#: months after a fresh start; without the 180d fallback every asset's Δ would
#: be blank for that whole period.
_DIV_SHORT_TIER = '22d'
_DIV_LONG_TIERS = ('252d', '180d')


def _divergence(row, prefix):
    """Δ = z_short − z_long for one signal, or None.

    Positive Δ means the signal is MORE unusual against the recent window than
    against the long one — busy this month, ordinary this year. Negative means
    the reverse. That distinction is the whole reason four tiers are computed,
    and it was invisible until this.

    Shared deliberately with the scores table's Δ column: if the drawer and the
    column that sorted you to a stock disagreed, the column would be useless.
    """
    z_short = row.get(f'raw_{prefix}_z_{_DIV_SHORT_TIER}')
    if z_short is None:
        return None
    for long_tier in _DIV_LONG_TIERS:
        z_long = row.get(f'raw_{prefix}_z_{long_tier}')
        if z_long is not None:
            return z_short - z_long
    return None


#: The z-scored signals whose cross-tier divergence feeds the table's Δ column.
#: Volume Dynamics is excluded deliberately — velocity/acceleration are
#: DIFFERENCES of z, not z-scores, so a "divergence between tiers" of a
#: difference is not the same quantity and would not be comparable.
_DIV_SIGNALS = ('gap', 'early_return', 'vwap', 'block_volume',
                'session_cumulative_volume', 'pvd', 'session_return')


def _row_divergence(row):
    """max |Δ| across a row's z-scored signals, or None.

    The table is one row per asset but Δ is per-signal, so the column needs an
    aggregate. MAX-ABS rather than a mean, for the same reason
    `raw_max_intraday_rs_z` uses it: this is a FINDING tool. A stock should
    surface if ANY of its signals disagree across horizons — averaging would
    wash one strong disagreement out against six quiet ones, which is the
    opposite of what a scan wants.

    Relative Strength is aggregated across benchmarks first, since a stock can
    be in NIFTY and SENSEX and each carries its own α-z.

    Reuses `_divergence`, so this column and the drawer's Δ rows can never
    report different numbers for the same stock.
    """
    best = None
    for prefix in _DIV_SIGNALS:
        d = _divergence(row, prefix)
        if d is not None and (best is None or abs(d) > abs(best)):
            best = d

    for bm in (row.get('raw_all_benchmarks') or []):
        if not isinstance(bm, dict):
            continue
        z_short = bm.get(f'intraday_rs_z_{_DIV_SHORT_TIER}')
        if z_short is None:
            continue
        for long_tier in _DIV_LONG_TIERS:
            z_long = bm.get(f'intraday_rs_z_{long_tier}')
            if z_long is not None:
                d = z_short - z_long
                if best is None or abs(d) > abs(best):
                    best = d
                break

    return abs(best) if best is not None else None


def _divergence_row(row, prefix):
    """The Δ row rendered beneath a tier block.

    Deliberately NOT colour-coded yet: what counts as a *significant* Δ is
    unknown — nobody has observed this quantity's distribution, because all four
    tiers only began populating with the multi-tier work. Colouring it against a
    guessed threshold would reproduce the old ↗-glyph failure: a marker that
    fires constantly and therefore says nothing.

    To calibrate, collect over at least one full session with all four tiers
    populated: Δ per (asset, signal) for every z-scored signal sampled through
    the day, the same for the 22d−90d pair as well as 22d−252d, and how often
    |Δ| exceeds 1.0 / 1.5 / 2.0. That decides three things at once — whether Δ
    gets its own threshold or inherits the z-slider; whether Δ should itself be
    z-scored (a raw spread of +1.5 means nothing without knowing whether that is
    the 50th or the 99th percentile); and whether 22d−90d is more actionable
    than short-vs-year. The colour scale follows from all three. Note the 252d
    tier needs ~a year of history, so the first usable calibration may have to
    run on 22d−90d.
    """
    d = _divergence(row, prefix)
    return _grid_row([
        "Δ 22d−long",
        f"{d:+.2f}" if d is not None else "N/A",
        None, None, None,
    ])


#: Standard tier-block header. Volume Block overrides it — it is the only
#: calculator z-scoring a log-transformed quantity, so its μ/σ carry a "log"
#: suffix. That labelling is load-bearing: before it, μ/σ were computed on raw
#: share counts while z was computed on log(1+volume), and a stored row could
#: report z=+0.2033 while its own displayed μ/σ implied −0.1089.
_TIER_HEADER = ["Tier", "z", "μ", "σ", "n"]
_TIER_HEADER_LOG = ["Tier", "z", "μ log", "σ log", "n"]


def _tier_block(row, prefix, title, active_tier=None, header=None):
    """One complete z-tier block: title, header, four tier rows, Δ row.

    Returns a LIST of html.Div rows, to be splatted into a card's children.

    This exists so the seven tier blocks across the cards are rendered from ONE
    place. They were previously six hand-written lines each — 28 near-identical
    `_grid_row(_std_tier_cells(...))` calls — which meant any change to how a
    tier row renders had to be applied 28 times consistently. That is the same
    shape as the `[-90:]` slice that hid in eleven calculators.

    `active_tier` emphasises the selected tier's row; None renders all four
    neutrally.
    """
    rows = [
        _grid_row([title], is_title=True),
        _grid_row(header or _TIER_HEADER, is_header=True),
    ]
    for tier in _TIERS:
        rows.append(_grid_row(
            _std_tier_cells(row, prefix, tier),
            is_active=(tier == active_tier) if active_tier else None,
        ))
    rows.append(_divergence_row(row, prefix))
    return rows


# ============================================================================
# Chart layout constants + line-trace helpers.
# ============================================================================

CHART_LAYOUT = dict(
    template='plotly_dark',
    height=300,
    margin=dict(l=50, r=20, t=40, b=40),
    font=dict(family='JetBrains Mono, ui-monospace, Menlo, monospace', size=11, color='#b8a99b'),
    xaxis=dict(showgrid=True, gridcolor='#2f2820', linecolor='#3a3128', zerolinecolor='#3a3128', tickfont=dict(color='#b8a99b', size=11), tickformat='%H:%M'),
    yaxis=dict(showgrid=True, gridcolor='#2f2820', linecolor='#3a3128', zerolinecolor='#3a3128', tickfont=dict(color='#b8a99b', size=11)),
    plot_bgcolor='#1a1410',
    paper_bgcolor='#1a1410',
    dragmode='pan',
    title=dict(font=dict(family='Fraunces, Georgia, serif', size=15, color='#f5e9d3')),
    legend=dict(font=dict(color='#b8a99b', size=10), bgcolor='rgba(0,0,0,0)'),
)

_POS_LINE = '#5fb87c'
_NEG_LINE = '#d94560'

_XAXIS_PAD = 0.15
_XAXIS_WINDOW_HOURS = 2


def _padded_xrange(ts_series, window_hours=_XAXIS_WINDOW_HOURS):
    """Return a fixed trailing window [t_max - window_hours, t_max + 15%].
    Pass window_hours=None to show the full data range.

    The chart builders pass the caller's value straight through: None is how
    the 1d chart-window pill asks for the whole day. They used to replace None
    with 2 hours first, which made 1d narrower than 4h and 8h."""
    ts = pd.to_datetime(ts_series, format='ISO8601')
    t_max = ts.max()
    t_min = ts.min() if window_hours is None else t_max - timedelta(hours=window_hours)
    span = t_max - t_min
    return [t_min, t_max + span * _XAXIS_PAD]


def _make_signed_line_traces(x_vals, y_vals, label: str):
    """Single visual line that switches color at each zero crossing: green
    positive, red negative. Two Scatter traces with interpolated crossings."""
    x = list(pd.to_datetime(x_vals, format='ISO8601'))
    y = [None if pd.isna(v) else float(v) for v in y_vals]

    pos_x, pos_y = [], []
    neg_x, neg_y = [], []

    for i, (xi, yi) in enumerate(zip(x, y)):
        if yi is None:
            pos_x.append(xi); pos_y.append(None)
            neg_x.append(xi); neg_y.append(None)
            continue

        if i > 0 and y[i - 1] is not None:
            prev_y = y[i - 1]
            prev_x = x[i - 1]
            if (prev_y < 0 < yi) or (prev_y > 0 > yi):
                t = -prev_y / (yi - prev_y)
                cross_x = prev_x + (xi - prev_x) * t
                pos_x.append(cross_x); pos_y.append(0.0)
                neg_x.append(cross_x); neg_y.append(0.0)

        if yi >= 0:
            pos_x.append(xi); pos_y.append(yi)
            neg_x.append(xi); neg_y.append(None)
        else:
            neg_x.append(xi); neg_y.append(yi)
            pos_x.append(xi); pos_y.append(None)

    return [
        go.Scatter(x=pos_x, y=pos_y, mode='lines', name=f'{label}+',
                   line=dict(color=_POS_LINE, width=1.5),
                   connectgaps=False, showlegend=False),
        go.Scatter(x=neg_x, y=neg_y, mode='lines', name=f'{label}-',
                   line=dict(color=_NEG_LINE, width=1.5),
                   connectgaps=False, showlegend=False),
    ]


def _make_significance_line_traces(x_vals, y_vals, threshold: float, label: str):
    """Three-way significance line: grey inside [-threshold, +threshold]
    (not statistically distinguishable from zero), green above +threshold,
    red below -threshold. Interpolates crossings at both band edges so the
    color switches exactly at the threshold. threshold=0 degenerates to a
    plain positive/negative split (every non-zero value is 'significant')."""
    x = list(pd.to_datetime(x_vals, format='ISO8601'))
    y = [None if pd.isna(v) else float(v) for v in y_vals]

    def band(v):
        if v is None:
            return None
        if v > threshold:
            return 'pos'
        if v < -threshold:
            return 'neg'
        return 'mid'

    buckets = {'pos': ([], []), 'neg': ([], []), 'mid': ([], [])}

    def emit(name, xi, yi):
        bx, by = buckets[name]
        bx.append(xi); by.append(yi)

    def emit_gap(xi):
        for bx, by in buckets.values():
            bx.append(xi); by.append(None)

    prev_x = prev_y = None
    for xi, yi in zip(x, y):
        if yi is None:
            emit_gap(xi)
            prev_x, prev_y = xi, yi
            continue

        if prev_y is not None:
            for edge in {threshold, -threshold}:
                if (prev_y < edge < yi) or (prev_y > edge > yi):
                    t = (edge - prev_y) / (yi - prev_y)
                    cross_x = prev_x + (xi - prev_x) * t
                    for name in buckets:
                        emit(name, cross_x, edge)

        cur_band = band(yi)
        for name in buckets:
            emit(name, xi, yi if name == cur_band else None)

        prev_x, prev_y = xi, yi

    return [
        go.Scatter(x=buckets['mid'][0], y=buckets['mid'][1], mode='lines',
                   name=f'{label} (not significant)',
                   line=dict(color='#7a6e62', width=1.5),
                   connectgaps=False, showlegend=False),
        go.Scatter(x=buckets['pos'][0], y=buckets['pos'][1], mode='lines', name=f'{label}+',
                   line=dict(color=_POS_LINE, width=1.5),
                   connectgaps=False, showlegend=False),
        go.Scatter(x=buckets['neg'][0], y=buckets['neg'][1], mode='lines', name=f'{label}-',
                   line=dict(color=_NEG_LINE, width=1.5),
                   connectgaps=False, showlegend=False),
    ]


# ============================================================================
# Chart builders — one per signal-owned chart. Each takes the signal's
# `history_key` DataFrame slice (from get_asset_metrics_history) plus a
# `resample` string; returns a plotly go.Figure. The composite price chart
# is NOT here — it crosses signals and stays inline in dashboard.py.
# ============================================================================

def _empty_chart(title: str) -> go.Figure:
    return go.Figure(layout={**CHART_LAYOUT,
                             'title': dict(text=f'{title} — No data available', font=dict(size=13))})


_Z_TIER_PALETTE = (
    ('_z_22d',  '22d',  '#f5e9d3'),
    ('_z_90d',  '90d',  '#d94560'),
    ('_z_180d', '180d', '#a6a380'),
    ('_z_252d', '252d', '#a58fa8'),
)


def _threshold(z_threshold) -> float:
    """The settings-drawer z-slider's value, or the configured default."""
    return float(z_threshold) if z_threshold is not None else _Z_HIGHLIGHT


def _add_z_unusual_shading(fig, threshold: float, bound: float):
    """Shade |z| > threshold — from the z-slider's value out to the chart's own
    y-axis edge (`bound`, from _z_yaxis_range).

    Both used to be fixed: the band ran from 2 to 3.5. So at a slider of 2.5 a
    line at 2.2 sat inside the "unusual" band while its row was not lit, and
    with the axis autoscaling past 3.5 a reading of 6 sat on plain background.
    """
    fig.add_hrect(y0=threshold, y1=bound,
                  fillcolor='rgba(255,255,255,0.04)', line_width=0, layer='below')
    fig.add_hrect(y0=-bound, y1=-threshold,
                  fillcolor='rgba(255,255,255,0.04)', line_width=0, layer='below')


#: Same tiers, same colours, but a bare `_22d` suffix — for signals that tier
#: without being z-scores themselves (Volume Dynamics' velocity, acceleration).
_PLAIN_TIER_PALETTE = (
    ('_22d',  '22d',  '#f5e9d3'),
    ('_90d',  '90d',  '#d94560'),
    ('_180d', '180d', '#a6a380'),
    ('_252d', '252d', '#a58fa8'),
)


#: Line weight / opacity for the tier the trader has selected vs the rest.
#: These charts keep plotting ALL four tiers — they are a divergence surface —
#: but with four equal-weight lines nothing ties the chart to the tier the table
#: and the row highlights are showing, so a trader cannot tell which line
#: produced the number that made them open the chart. Emphasis answers that
#: without hiding the other three.
_TIER_EMPHASIS = {'width': 2.6, 'opacity': 1.0}
_TIER_MUTED = {'width': 1.1, 'opacity': 0.55}


def _tier_line_style(tier_label: str, active_tier):
    """Emphasis for one tier line. `active_tier=None` -> all tiers equal, so a
    caller that does not pass one renders exactly as before."""
    if active_tier is None:
        return {'width': 1.5, 'opacity': 1.0}
    return _TIER_EMPHASIS if tier_label == active_tier else _TIER_MUTED


def _build_z_tier_chart(history, field_prefix: str, title: str, resample='30s',
                        window_hours=_XAXIS_WINDOW_HOURS, palette=None, shade_unusual=True,
                        yaxis_title='σ', active_tier=None, z_threshold=None, **_ignore):
    """Generic 4-tier line chart over `{field_prefix}{suffix}` columns.

    `palette` selects the suffix set: `_Z_TIER_PALETTE` (default) for true
    z-scores, `_PLAIN_TIER_PALETTE` for signals that tier without being
    z-scores.

    `shade_unusual` draws the |z| > z-slider band. Turn it OFF for anything that is not
    a z-score: Volume Dynamics' velocity is a CHANGE in z between blocks, so a
    velocity of 2 means "abnormality rose by 2 sigma", not "this is a 2-sigma
    event" — shading it would assert a significance the number does not carry.
    """
    palette = palette or _Z_TIER_PALETTE
    if history is None or history.empty:
        return _empty_chart(title)
    h = history.sort_values('timestamp')
    if resample != '30s':
        freq = '1min' if resample == '1m' else '5min'
        h = h.copy()
        h['timestamp'] = pd.to_datetime(h['timestamp'], format='ISO8601')
        h = h.set_index('timestamp').resample(freq).mean(numeric_only=True).reset_index()
    yrange = _z_yaxis_range(h, [f'{field_prefix}{suffix}' for suffix, _lbl, _c in palette])
    fig = go.Figure()
    if shade_unusual:
        _add_z_unusual_shading(fig, _threshold(z_threshold), yrange[1])
    added_any = False
    for suffix, tier_label, color in palette:
        col = f'{field_prefix}{suffix}'
        if col in h.columns and h[col].notna().any():
            style = _tier_line_style(tier_label, active_tier)
            fig.add_trace(go.Scatter(
                x=h['timestamp'], y=h[col],
                mode='lines', name=tier_label,
                line=dict(color=color, width=style['width']),
                opacity=style['opacity'],
            ))
            added_any = True
    if not added_any:
        return _empty_chart(title)
    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62', line_width=1)
    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text=title, font=dict(size=13)),
        'yaxis_title': yaxis_title,
        'yaxis_range': yrange,
        'xaxis_range': _padded_xrange(h['timestamp'], window_hours=window_hours),
    })
    return fig


def build_velocity_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS, **_ignore):
    """Volume Dynamics velocity, one line per tier.

    velocity = ZQ_t - ZQ_(t-1). Four lines, one per tier, because Volume
    Dynamics tiers exactly as Volume Block does — and cross-tier divergence is
    the informative part: participation accelerating against 22 days while flat
    against 252 is a different story from both agreeing.

    No unusual-z shading: this is a CHANGE in z, not a z.
    """
    return _build_z_tier_chart(
        history, 'velocity', 'Volume Velocity (ΔZQ block-to-block)',
        resample=resample, window_hours=window_hours,
        palette=_PLAIN_TIER_PALETTE, shade_unusual=False, yaxis_title='ΔZQ',
        **_ignore,   # carries active_tier for line emphasis
    )


def build_acceleration_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS, **_ignore):
    """Volume Dynamics acceleration, one line per tier.

    acceleration = velocity_t - velocity_(t-1). No unusual-z shading — a second
    difference of z-scores is further still from being a z-score.
    """
    return _build_z_tier_chart(
        history, 'acceleration', 'Volume Acceleration (ΔVelocity block-to-block)',
        resample=resample, window_hours=window_hours,
        palette=_PLAIN_TIER_PALETTE, shade_unusual=False, yaxis_title='ΔΔZQ',
        **_ignore,   # carries active_tier for line emphasis
    )


def build_vwap_deviation_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS, **_ignore):
    """VWAP displacement (log-return, shown as approx percent) signed-line chart."""
    if history is None or history.empty or 'displacement' not in history.columns:
        return _empty_chart('VWAP Displacement %')
    vwap = history.dropna(subset=['displacement']).sort_values('timestamp')
    if vwap.empty:
        return _empty_chart('VWAP Displacement %')
    fig = go.Figure()
    disp_pct = vwap['displacement'] * 100.0
    for trace in _make_signed_line_traces(vwap['timestamp'], disp_pct, 'VWAP Disp'):
        fig.add_trace(trace)
    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62', line_width=1)
    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text='VWAP Displacement %', font=dict(size=13)),
        'yaxis_title': '%',
        'xaxis_range': _padded_xrange(vwap['timestamp'], window_hours=window_hours),
    })
    return fig


def build_momentum_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS, **_ignore):
    """Momentum early-return 4-tier z-chart (the 09:15->09:30 window move,
    z-scored against 22 / 90 / 180 / 252 prior sessions at the same window,
    one line per tier)."""
    return _build_z_tier_chart(history, 'early_return', 'Early Return Z-Scores',
                               resample, window_hours=window_hours, **_ignore)


def build_rel_strength_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS,
                              rs_benchmark: Optional[str] = None,
                              active_tier=None, z_threshold=None, **_ignore):
    """RS chart — benchmark-selector-driven 4-tier intraday-RS-z view.

    When `rs_benchmark` is set, filters history to that benchmark and plots
    all 4 intraday_rs_z tier lines. Falls back to the first benchmark with
    any z data when `rs_benchmark` isn't set or isn't present."""
    _tier_cols = [f'intraday_rs_z_{t}' for t in _TIERS]
    if history is None or history.empty or not any(c in history.columns for c in _tier_cols):
        return _empty_chart('RS Intraday-z per Benchmark')

    available = list(history['benchmark_name'].dropna().unique())
    if rs_benchmark not in available:
        rs_benchmark = available[0] if available else None
    if rs_benchmark is None:
        return _empty_chart('RS Intraday-z per Benchmark')

    bm_slice = history[history['benchmark_name'] == rs_benchmark].sort_values('timestamp')
    if bm_slice.empty:
        return _empty_chart(f'RS Intraday-z vs {rs_benchmark}')

    _tiers = [
        ('intraday_rs_z_22d',  '22d',  '#f5e9d3'),
        ('intraday_rs_z_90d',  '90d',  '#d94560'),
        ('intraday_rs_z_180d', '180d', '#a6a380'),
        ('intraday_rs_z_252d', '252d', '#a58fa8'),
    ]
    yrange = _z_yaxis_range(bm_slice, [c for c, _n, _col in _tiers])
    fig = go.Figure()
    _add_z_unusual_shading(fig, _threshold(z_threshold), yrange[1])

    added_any = False
    for col, name, color in _tiers:
        if col in bm_slice.columns and bm_slice[col].notna().any():
            style = _tier_line_style(name, active_tier)
            fig.add_trace(go.Scatter(
                x=bm_slice['timestamp'], y=bm_slice[col],
                mode='lines', name=name,
                line=dict(color=color, width=style['width']),
                opacity=style['opacity'],
            ))
            added_any = True
    if not added_any:
        return _empty_chart(f'RS Intraday-z vs {rs_benchmark}')

    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62')
    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text=f'RS Intraday-z vs {rs_benchmark}', font=dict(size=13)),
        'yaxis_title': 'σ',
        'yaxis_range': yrange,
        'xaxis_range': _padded_xrange(
            bm_slice['timestamp'],
            window_hours=window_hours,
        ),
    })
    return fig


def build_volume_block_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS,
                             active_tier=None, z_threshold=None, **_ignore):
    """Volume Block 4-tier z-scores chart (block_volume_z — the primary
    feature). Dedupes to one row per time_block."""
    if history is None or history.empty:
        return _empty_chart('Volume Block Z-Scores')
    vb = history.sort_values('timestamp')
    if 'time_block' in vb.columns:
        vb = vb.drop_duplicates(subset=['time_block'], keep='first')
    # (column, trace name, bare tier for _tier_line_style, colour). The bare
    # tier is separate because the trace name 'Z-Score 22d' would never match
    # the active tier '22d'.
    _z_tiers = [
        ('block_volume_z_22d',  'Z-Score 22d',  '22d',  '#f5e9d3'),
        ('block_volume_z_90d',  'Z-Score 90d',  '90d',  '#d94560'),
        ('block_volume_z_180d', 'Z-Score 180d', '180d', '#a6a380'),
        ('block_volume_z_252d', 'Z-Score 252d', '252d', '#a58fa8'),
    ]
    yrange = _z_yaxis_range(vb, [c for c, _n, _t, _col in _z_tiers])
    fig = go.Figure()
    _add_z_unusual_shading(fig, _threshold(z_threshold), yrange[1])
    added_any = False
    for col, name, tier_label, color in _z_tiers:
        if col in vb.columns and vb[col].notna().any():
            style = _tier_line_style(tier_label, active_tier)
            fig.add_trace(go.Scatter(
                x=vb['timestamp'], y=vb[col],
                mode='lines+markers', name=name,
                line=dict(color=color, width=style['width']),
                opacity=style['opacity'],
                marker=dict(size=6),
            ))
            added_any = True
    if not added_any:
        return _empty_chart('Volume Block Z-Scores')
    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62')
    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text='Volume Block Z-Scores', font=dict(size=13)),
        'yaxis_title': 'σ',
        'yaxis_range': yrange,
        'xaxis_range': _padded_xrange(vb['timestamp'], window_hours=window_hours),
    })
    return fig


def build_rolling_momentum_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS,
                                 active_tier=None, z_threshold=None, **_ignore):
    """Rolling Momentum 4-tier z-scores chart (session_return_z — continuously
    re-evaluated every cycle against whichever 15-min block is current, unlike
    Momentum's fixed block-1 window). No time_block column on this Row (the
    raw value updates every cycle, not per-block, like VWAP's displacement) —
    so no dedupe step, unlike Volume Block's chart."""
    if history is None or history.empty:
        return _empty_chart('Rolling Momentum Z-Scores')
    rm = history.sort_values('timestamp')
    # Bare tier kept separately, as in build_volume_block_chart.
    _z_tiers = [
        ('session_return_z_22d',  'Z-Score 22d',  '22d',  '#f5e9d3'),
        ('session_return_z_90d',  'Z-Score 90d',  '90d',  '#d94560'),
        ('session_return_z_180d', 'Z-Score 180d', '180d', '#a6a380'),
        ('session_return_z_252d', 'Z-Score 252d', '252d', '#a58fa8'),
    ]
    yrange = _z_yaxis_range(rm, [c for c, _n, _t, _col in _z_tiers])
    fig = go.Figure()
    _add_z_unusual_shading(fig, _threshold(z_threshold), yrange[1])
    added_any = False
    for col, name, tier_label, color in _z_tiers:
        if col in rm.columns and rm[col].notna().any():
            style = _tier_line_style(tier_label, active_tier)
            fig.add_trace(go.Scatter(
                x=rm['timestamp'], y=rm[col],
                mode='lines', name=name,
                line=dict(color=color, width=style['width']),
                opacity=style['opacity'],
            ))
            added_any = True
    if not added_any:
        return _empty_chart('Rolling Momentum Z-Scores')
    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62')
    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text='Rolling Momentum Z-Scores', font=dict(size=13)),
        'yaxis_title': 'σ',
        'yaxis_range': yrange,
        'xaxis_range': _padded_xrange(rm['timestamp'], window_hours=window_hours),
    })
    return fig


def build_pv_divergence_chart(history, resample='30s', window_hours=_XAXIS_WINDOW_HOURS, **_ignore):
    """PV Divergence correlation chart (PVD_LOCKED_V3). Pearson r of
    price/volume deltas, three-way significance colored against the current
    critical_r floor: grey inside ±critical_r (not statistically
    distinguishable from zero), green above, red below — plus a shaded
    noise-band rectangle at the same threshold."""
    if history is None or history.empty or 'pearson_r' not in history.columns:
        return _empty_chart('Price-Volume Divergence')
    pvd = history.dropna(subset=['pearson_r']).sort_values('timestamp')
    if pvd.empty:
        return _empty_chart('Price-Volume Divergence')

    latest_row = pvd.iloc[-1]
    critical_r = latest_row.get('critical_r')
    threshold = float(critical_r) if critical_r is not None and not pd.isna(critical_r) else 0.0

    fig = go.Figure()
    if threshold > 0:
        fig.add_hrect(y0=-threshold, y1=threshold,
                      fillcolor='rgba(255,255,255,0.05)', line_width=0, layer='below')
    for trace in _make_significance_line_traces(pvd['timestamp'], pvd['pearson_r'], threshold, 'ρ'):
        fig.add_trace(trace)
    fig.add_hline(y=0, line_dash='dash', line_color='#7a6e62', line_width=1)

    current_pearson = latest_row.get('pearson_r')
    current_reliable = latest_row.get('reliable')
    current_agreement = latest_row.get('estimator_agreement')
    if current_pearson is not None and not pd.isna(current_pearson):
        reliability_text = 'reliable' if current_reliable else 'not reliable'
        agreement_text = (f' · agreement {current_agreement:+.1f}'
                           if current_agreement is not None and not pd.isna(current_agreement) else '')
        title_text = f'Price-Volume Divergence — ρ {current_pearson:+.3f} · {reliability_text}{agreement_text}'
    else:
        title_text = 'Price-Volume Divergence'

    fig.update_layout(**{
        **CHART_LAYOUT,
        'title': dict(text=title_text, font=dict(size=13)),
        'yaxis_range': [-1.15, 1.15],
        'xaxis_range': _padded_xrange(pvd['timestamp'], window_hours=window_hours),
    })
    return fig


# ============================================================================
# Signal dataclass — binds emission to dashboard surfaces.
# ============================================================================

@dataclass
class Signal:
    name: str
    emit_key: str
    row_class: type
    raw_prefix: str = 'raw_'
    field_renames: Dict[str, str] = field(default_factory=dict)
    multi_emission: bool = False
    # (selected_row, z_threshold, tier) -> html.Div. Every builder takes both,
    # even those that read neither (only Volume Block and Relative Strength
    # read the threshold): create_detail_panel calls all nine the same way, so
    # the next builder that needs one gets it without another contract change.
    card_builder: Optional[Callable[[dict, Optional[float], Optional[str]], Any]] = None
    chart_builders: Dict[str, Tuple[str, Callable]] = field(default_factory=dict)
    # (z_threshold, tier) -> (DataTable filter_query, background colour).
    # Both arguments, on every rule, even the two structural ones that use
    # neither — _row_highlight_rules calls them all identically.
    highlight_rule: Optional[Callable[[float, str], Tuple[str, str]]] = None
    health_label: str = ''
    stale_threshold_seconds: int = 90


# ============================================================================
# Card builders — one per signal. Each takes selected_row (a merged-row dict
# from build_table_data) and returns an html.Div.
# ============================================================================

def build_momentum_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Momentum card — fixed 09:15->09:30 window. Gap (open vs prev close)
    and early_return (window vs open) each get their own 4-tier z table."""
    gap_return    = selected_row.get('raw_gap_return')
    early_return  = selected_row.get('raw_early_return')
    open_price    = selected_row.get('raw_open_price')
    prev_close    = selected_row.get('raw_previous_close')

    if early_return is not None and early_return > 0:
        accent = '#5fb87c'
    elif early_return is not None and early_return < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    return _signal_card("Momentum", accent, [
        _grid_row(["Gap",   _fmt_log_pct(gap_return), "Early Return", _fmt_log_pct(early_return), None]),
        _grid_row(["Open",  _fmt_price(open_price),   "Prev Close",   _fmt_price(prev_close),      None]),

        *_tier_block(selected_row, 'gap', "Gap Z-Scores", tier),

        *_tier_block(selected_row, 'early_return', "Early Return Z-Scores", tier),
    ])


_DIRECTION_LABEL = {1: 'UP', -1: 'DOWN', 0: 'NONE'}
_NOW_LABEL = {1: 'Above range', -1: 'Below range', 0: 'Inside range'}


def build_breakout_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Breakout card: direction (+1/-1/0) + position + range + accept/fail state.

    Direction and State tell the story of the day's FIRST breakout. "Now" is
    where price sits against the range right now (two readings in a row) —
    the rule behind the ▲ glyph and the row highlight — so a stock that broke
    up and now trades below its range reads "UP · Failed · Below range"."""
    direction         = selected_row.get('raw_breakout_direction') or 0
    now_side          = selected_row.get('raw_breakout_now') or 0
    position          = selected_row.get('raw_breakout_position')
    range_high        = selected_row.get('raw_range_high')
    range_low         = selected_row.get('raw_range_low')
    accepted          = selected_row.get('raw_accepted_breakout')
    failed            = selected_row.get('raw_failed_breakout')
    distance_from_rng = selected_row.get('raw_distance_from_range')

    if direction == 1:
        accent = '#5fb87c'
    elif direction == -1:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    state_val = "Accepted" if accepted else ("Failed" if failed else "Pending")
    now_val = _NOW_LABEL.get(now_side, 'N/A') if range_high is not None else 'N/A'

    return _signal_card("Breakout", accent, [
        _grid_row(["Direction", _DIRECTION_LABEL.get(direction, 'N/A'), "Position", _fmt_z(position), None]),
        _grid_row(["Range High", _fmt_price(range_high), "Range Low", _fmt_price(range_low), None]),
        _grid_row(["State", state_val, "Distance", _fmt_z(distance_from_rng), None]),
        _grid_row(["Now", now_val, None, None, None]),
    ])


def build_vwap_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """VWAP card: vwap + displacement + side + acceptance + z-tier table.
    No band levels — dropped from the locked design."""
    vwap          = selected_row.get('raw_vwap')
    displacement  = selected_row.get('raw_displacement')
    side          = selected_row.get('raw_side')
    # `raw_acceptance` is VWAP's own acceptance.
    #
    # RelativeStrengthRow used to declare an untiered `acceptance` too. RS is a
    # multi_emission signal: its first benchmark row is merged FLAT onto the
    # asset dict, over the same key, and SIGNAL_REGISTRY orders VWAP before RS
    # — so RS's value won, every cycle, and the "Widening/Reverting" line below
    # was reporting RS's acceptance rather than VWAP's. Measured on a live
    # row: VWAP -0.111, RS +0.999, merged 0.999.
    #
    # RS's acceptance is now per-tier (`acceptance_22d/90d/180d/252d`), so the
    # key no longer collides. DO NOT reintroduce an untiered `acceptance` on
    # RelativeStrengthRow — nothing would fail; this card would just quietly
    # start lying again.
    acceptance    = selected_row.get('raw_acceptance')
    current_price = selected_row.get('raw_current_price')

    if displacement is not None and displacement > 0:
        accent = '#5fb87c'
    elif displacement is not None and displacement < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    side_val = "Above" if side == 1 else ("Below" if side == -1 else "At")
    accept_val = ("Widening" if acceptance is not None and acceptance > 0
                  else "Reverting" if acceptance is not None and acceptance < 0 else "N/A")

    return _signal_card("VWAP", accent, [
        _grid_row(["VWAP", _fmt_price(vwap), "Current", _fmt_price(current_price), None]),
        _grid_row(["Displacement", _fmt_log_pct(displacement), "Side", side_val, None]),
        _grid_row(["Acceptance", accept_val, None, None, None]),

        *_tier_block(selected_row, 'vwap', "VWAP Z-Scores", tier),
    ])


def _rs_z_cell_style(z_val, threshold: float):
    """Cell-background significance coloring: green if z > threshold, red if
    z < -threshold, muted otherwise, neutral if NULL. `threshold` is the
    z-slider's — it used to be a literal 2, so the cell and the row highlight
    disagreed at any other slider position."""
    style = {
        'padding': '5px 8px',
        'textAlign': 'right',
        'fontFamily': "'JetBrains Mono', ui-monospace, Menlo, monospace",
        'fontSize': '12px',
        'color': '#f5e9d3',
        'border': '1px solid #2f2820',
    }
    if z_val is None:
        style['color'] = '#7a6e62'
    elif z_val > threshold:
        style['backgroundColor'] = 'rgba(95, 184, 124, 0.25)'
    elif z_val < -threshold:
        style['backgroundColor'] = 'rgba(217, 69, 96, 0.25)'
    else:
        style['backgroundColor'] = 'rgba(184, 169, 155, 0.06)'
    return style


def build_relative_strength_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """RS card: ONE table per benchmark. Rows are tiers, columns are
    β | Session RS | Intraday RS | α-z.

    This replaced two separate blocks — an α-z matrix across four tiers, and a
    context block that explained only the 90d tier. The problem was structural:
    a trader reading the 22d α-z cell had no way to see it was measured with
    β₂₂ = 0.97 rather than β₉₀ = 1.03. **The number that explains the cell was
    not on the card.**

    Putting β on the SAME ROW as the α-z it produced makes each row one
    coherent measurement — this beta, this RS, this z — rather than numbers
    from two different blocks implying a relationship they do not have. That is
    the rule that a tier's β and its z are one axis, made visible: tier T is
    measured with β_T and normalised against T samples rebuilt with β_T.

    It is also FEWER elements than the alternative. Expanding the context block
    into three more benchmark × tier matrices (one each for β, session,
    intraday) would have tripled the card.

    Significance colouring stays on the α-z column — it is the only z.
    """
    all_benchmarks = selected_row.get('raw_all_benchmarks', [])
    intraday_rs    = selected_row.get(f'raw_intraday_rs_{tier}') if tier else None
    if intraday_rs is None:
        intraday_rs = selected_row.get('raw_intraday_rs_90d')

    if intraday_rs is not None and intraday_rs > 0:
        accent = '#5fb87c'
    elif intraday_rs is not None and intraday_rs < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    children = []

    if all_benchmarks and isinstance(all_benchmarks, list):
        for bm in all_benchmarks:
            bm_name = bm.get('benchmark_name', '?')
            children.append(_grid_row([f"{bm_name} — per tier"], is_title=True))
            children.append(_grid_row(
                ["Tier", "β", "Session RS", "Intraday RS", "α-z"], is_header=True))
            for t in _TIERS:
                z_val = bm.get(f'intraday_rs_z_{t}')
                z_text = f'{z_val:+.2f}' if z_val is not None else '—'
                bg = _rs_z_cell_style(z_val, _threshold(z_threshold)).get('backgroundColor')
                z_cell = html.Div(
                    z_text,
                    style={'backgroundColor': bg, 'padding': '4px 8px',
                           'borderRadius': '3px'},
                ) if bg is not None else z_text
                children.append(_grid_row(
                    [
                        t,
                        _fmt_scalar(bm.get(f'beta_{t}')),
                        _fmt_log_pct(bm.get(f'session_rs_{t}')),
                        _fmt_log_pct(bm.get(f'intraday_rs_{t}')),
                        z_cell,
                    ],
                    is_active=(t == tier) if tier else None,
                ))
    else:
        benchmark = selected_row.get('raw_benchmark_name')
        children.append(_grid_row([
            f"vs {benchmark or 'N/A'}", None, None, None, _fmt_log_pct(intraday_rs),
        ]))

    return _signal_card("Relative Strength", accent, children)


def build_volume_dynamics_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Volume Dynamics card — derived state signal (no z-tiers of its own,
    it's built directly on Volume Block's z-score). Shows raw
    velocity/acceleration/persistence plus the discrete expansion_state."""
    _state_label = {2: 'Expanding rapidly', 1: 'Expanding', 0: 'Neutral',
                    -1: 'Contracting', -2: 'Collapsing'}

    # Accent follows the selected tier (90d only when none is passed).
    state = selected_row.get(f'raw_expansion_state_{tier or "90d"}')
    if state is not None and state > 0:
        accent = '#5fb87c'
    elif state is not None and state < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    # All four tiers, because cross-tier divergence is the point: participation
    # accelerating hard against 22 days while flat against 252 says something
    # quite different from both tiers agreeing.
    #
    # No mu/sigma/n columns — these are NOT z-scores. volume_z_* is a
    # pass-through of Volume Block's own z, whose stats live on that card.
    #
    # The loop variable is `t`, not `tier`: reusing the parameter's name
    # overwrote it, which is why this was the one card that never marked the
    # selected tier's row.
    #
    # An incomplete current block has no ZQ, so every value below is empty;
    # say why, as the Volume Block card does, rather than show bare hyphens.
    rows = []
    if selected_row.get('raw_volume_dynamics_status') == 'incomplete_block':
        rows.append(_grid_row(["Block not fully observed (reading gap at its start or end) — no values"],
                              is_title=True))
    rows.append(_grid_row(["Tier", "ZQ", "Velocity", "Accel", "State"], is_header=True))
    for t in ('22d', '90d', '180d', '252d'):
        rows.append(_grid_row([
            t,
            _fmt_z(selected_row.get(f'raw_volume_z_{t}')),
            _fmt_z(selected_row.get(f'raw_velocity_{t}')),
            _fmt_z(selected_row.get(f'raw_acceleration_{t}')),
            _state_label.get(selected_row.get(f'raw_expansion_state_{t}'), 'N/A'),
        ], is_active=(t == tier) if tier else None))

    persistence_rows = [
        _grid_row(["Persistence"], is_title=True),
        _grid_row([
            "22d", _fmt_z(selected_row.get('raw_persistence_22d')),
            "90d", _fmt_z(selected_row.get('raw_persistence_90d')), None,
        ]),
        _grid_row([
            "180d", _fmt_z(selected_row.get('raw_persistence_180d')),
            "252d", _fmt_z(selected_row.get('raw_persistence_252d')), None,
        ]),
    ]

    return _signal_card("Volume Dynamics", accent, rows + persistence_rows)


def build_volume_block_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Volume Block card: block volume + reconstructed session-cumulative
    volume, each with its own 4-tier z table (block_volume_z is the primary
    feature; session_cumulative_volume_z is session-context only)."""
    time_block = selected_row.get('raw_time_block')
    block_vol  = selected_row.get('raw_block_volume')
    session_cum = selected_row.get('raw_session_cumulative_volume')
    session_blocks = selected_row.get('raw_session_cumulative_blocks')
    status = selected_row.get('raw_volume_block_status')

    # Accent follows the settings-drawer z-slider, not a literal 2.0. The
    # hardcoded value meant that at any slider position other than 2.0 this
    # card's amber accent disagreed with the row highlight and the Signals
    # glyph for the very same stock, on the same screen.
    #
    # The secondary "approaching" band keeps its 3/4-of-threshold relationship
    # to the primary one (1.5 was 3/4 of 2.0), so the two-step accent scales
    # with the slider instead of inverting at low thresholds — at a threshold
    # of 1.5 a fixed 1.5 secondary would have fired at the same value as the
    # primary.
    #
    # And it reads the SELECTED tier's z, compared as |z| — exactly the row
    # highlight's rule. At 90d-only it disagreed with the row at every other
    # tier; as a signed `z > thr` an unusually QUIET block lit the row and
    # left this card plain.
    thr = _threshold(z_threshold)
    z = selected_row.get(f'raw_block_volume_z_{tier or "90d"}')
    if z is not None and abs(z) > thr:
        accent = '#e8a447'
    elif z is not None and abs(z) > thr * 0.75:
        accent = '#5fb87c'
    else:
        accent = '#3a3128'

    block_val = str(int(time_block)) if time_block is not None else "N/A"
    block_vol_val = f"{block_vol:,.0f}" if block_vol is not None else "N/A"
    session_cum_val = f"{session_cum:,.0f}" if session_cum is not None else "N/A"
    # The session cumulative sums only today's COMPLETE blocks (and is compared
    # with the same blocks on prior days), so say how many of the day's it holds.
    session_blocks_val = (f"{int(session_blocks)} of {int(time_block)}"
                          if session_blocks is not None and time_block is not None else "N/A")
    # The engine found no reading within 30 s of this block's start or end, so
    # its total is not trusted and it has no z — a gap, not a lack of history.
    status_note = ("Block not fully observed (reading gap at its start or end) — no z-score"
                   if status == 'incomplete_block' else None)

    # μ/σ carry a "log" suffix here and nowhere else: Volume Block is the only
    # signal that z-scores a TRANSFORM of its quantity (ln(1+volume)), so its
    # μ/σ are in log space and reconcile exactly with the z beside them — but
    # a reader seeing μ 13.48 next to Block Volume 712,340 would otherwise
    # reasonably conclude the μ is wrong.
    return _signal_card("Volume Block", accent, [
        *([_grid_row([status_note], is_title=True)] if status_note else []),
        _grid_row(["Block", block_val, "Block Volume", block_vol_val, None]),
        _grid_row(["Session Cumulative", session_cum_val, "Blocks", session_blocks_val, None]),

        *_tier_block(selected_row, 'block_volume', "Block Volume Z-Scores (primary)", tier, header=_TIER_HEADER_LOG),

        *_tier_block(selected_row, 'session_cumulative_volume', "Session Cumulative Z-Scores (context)", tier, header=_TIER_HEADER_LOG),
    ])


def build_pv_divergence_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """PV Divergence card (PVD_LOCKED_V3). Raw layer: Pearson r of price/volume
    deltas (primary), Spearman r + Winsorized Pearson (outlier-robust cross-
    checks), estimator_agreement (do all three agree on sign), reliable (is
    |pearson_r| above the 95%-confidence critical_r floor), price_volatility
    (context). Accent color reflects `reliable`, not just sign — an
    unreliable correlation renders muted regardless of direction. Tier
    bundle: standard z/μ/σ/n × 4 tiers, z-scoring the Fisher-transformed
    Pearson r."""
    pearson     = selected_row.get('raw_pearson_r')
    spearman    = selected_row.get('raw_spearman_r')
    winsorized  = selected_row.get('raw_winsorized_r')
    fisher      = selected_row.get('raw_fisher_value')
    critical    = selected_row.get('raw_critical_r')
    reliable    = selected_row.get('raw_reliable')
    agreement   = selected_row.get('raw_estimator_agreement')
    available   = selected_row.get('raw_estimators_available')
    sample_size = selected_row.get('raw_sample_size')
    volatility  = selected_row.get('raw_price_volatility')
    status      = selected_row.get('raw_price_volume_divergence_status')

    if pearson is None or not reliable:
        accent = '#3a3128'
    elif pearson > 0:
        accent = '#5fb87c'
    elif pearson < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    # Agreement is None below 2 surviving estimators. Rendering that as plain
    # "N/A" would be indistinguishable from "not warmed up yet" — say how many
    # of the three actually produced a value instead.
    if agreement is None:
        agreement_text = f"{available or 0} of 3" if available is not None else "N/A"
    else:
        agreement_text = f"{_fmt_scalar(agreement)} ({available or 0}/3)"

    # A stock that has not ticked for 15 minutes, or is locked at a circuit
    # limit, is a FINDING — not an absence. These statuses exist so the drawer
    # can say which, rather than showing a card of blanks.
    _status_note = {
        'no_price_variation': 'Price did not move in this window',
        'no_volume_variation': 'Volume did not vary in this window',
        'insufficient_data': 'Not enough ticks yet',
        'interrupted': 'Readings interrupted in this window (a gap over 30 s) — no value',
    }.get(status)

    return _signal_card("Price-Volume Divergence", accent, [
        *([_grid_row([_status_note], is_title=True)] if _status_note else []),
        _grid_row(["Pearson r", _fmt_scalar(pearson), "Spearman r", _fmt_scalar(spearman), None]),
        _grid_row(["Winsorized r", _fmt_scalar(winsorized), "Fisher z", _fmt_scalar(fisher), None]),
        _grid_row(["Reliable", "N/A" if reliable is None else ("Yes" if reliable else "No"),
                   "Critical r", _fmt_scalar(critical), None]),
        _grid_row(["Agreement", agreement_text, "Sample n", _fmt_int(sample_size), None]),
        _grid_row(["Price Volatility", _fmt_scalar(volatility), None, None, None]),

        *_tier_block(selected_row, 'pvd', "PVD Z-Scores (Fisher r)", tier),
    ])


def build_rolling_momentum_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Rolling Momentum card. Raw layer: session_return (plain percent, same
    convention as the shared store's session_price_move column it's z-scored
    against). Tier bundle: standard z/μ/σ/n x 4 tiers."""
    session_return = selected_row.get('raw_session_return')

    if session_return is None:
        accent = '#3a3128'
    elif session_return > 0:
        accent = '#5fb87c'
    elif session_return < 0:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    return _signal_card("Rolling Momentum", accent, [
        _grid_row(["Session Return", _fmt_pct(session_return), None, None, None]),

        *_tier_block(selected_row, 'session_return', "Session Return Z-Scores", tier),
    ])


_LEVEL_LABEL = {
    'local_high':  'Local High',
    'global_high': 'Global High',
    'local_low':   'Local Low',
    'global_low':  'Global Low',
}


def build_price_levels_card(selected_row: dict, z_threshold: float = None,
                    tier: str = None):
    """Price Levels card — one row per tracked level (local/global high/low),
    reading the `levels` dict directly (level_name -> LevelEventResult-shaped
    dict) plus `day_events` (levels crossed at any point today)."""
    levels     = selected_row.get('raw_levels', {}) or {}
    day_events = selected_row.get('raw_day_events', []) or []

    crossed_today = set(day_events) if isinstance(day_events, list) else set()
    any_high = any('high' in name for name in crossed_today)
    any_low = any('low' in name for name in crossed_today)

    if any_high:
        accent = '#5fb87c'
    elif any_low:
        accent = '#d94560'
    else:
        accent = '#3a3128'

    rows = []
    for level_name in ('local_high', 'global_high', 'local_low', 'global_low'):
        info = levels.get(level_name) if isinstance(levels, dict) else None
        if isinstance(info, dict):
            price_str = _fmt_price(info.get('level_value'))
            dist_str = _fmt_pct(info.get('distance_pct'))
            crossed_str = "Yes" if info.get('crossed') else "No"
        else:
            price_str, dist_str, crossed_str = "N/A", "N/A", "No"
        today_str = "Yes" if level_name in crossed_today else "No"
        rows.append(_grid_row([
            _LEVEL_LABEL[level_name], price_str, dist_str, crossed_str, today_str,
        ]))

    return _signal_card("Price Levels", accent, [
        _grid_row(["Level", "Price", "Distance", "Crossed Now", "Crossed Today"], is_header=True),
        *rows,
    ])


# ============================================================================
# Registry — one Signal per per-asset calculator.
# ============================================================================

SIGNAL_REGISTRY: Dict[str, Signal] = {
    'momentum': Signal(
        name='momentum',
        emit_key='momentum',
        row_class=MomentumRow,
        field_renames={'status': 'momentum_status'},
        card_builder=build_momentum_card,
        chart_builders={'chart-momentum': ('momentum', build_momentum_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_early_return_z_{tier}}} > {t} || {{raw_early_return_z_{tier}}} < -{t}',
            '#4a3614',
        ),
        health_label='Mom',
    ),
    'breakout': Signal(
        name='breakout',
        emit_key='breakout',
        row_class=BreakoutRow,
        field_renames={'status': 'breakout_status'},
        card_builder=build_breakout_card,
        # No dedicated chart — breakout state is a discrete signal, surfaced
        # via the composite price chart's overlays + the row highlight.
        highlight_rule=lambda t, tier: (
            # Outside the opening range right now — not the day's first
            # breakout (breakout_direction), which never clears.
            '{raw_breakout_now} != 0',  # structural — neither threshold nor tier applies
            '#4a3614',
        ),
        health_label='Bkout',
    ),
    'vwap': Signal(
        name='vwap',
        emit_key='vwap',
        row_class=VwapRow,
        field_renames={'status': 'vwap_status'},
        card_builder=build_vwap_card,
        chart_builders={'chart-vwap-deviation': ('vwap', build_vwap_deviation_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_vwap_z_{tier}}} > {t} || {{raw_vwap_z_{tier}}} < -{t}',
            '#4a3614',
        ),
        health_label='VWAP',
    ),
    'relative_strength': Signal(
        name='relative_strength',
        emit_key='relative_strength',
        row_class=RelativeStrengthRow,
        field_renames={'status': 'relative_strength_status'},
        multi_emission=True,
        card_builder=build_relative_strength_card,
        chart_builders={'chart-rel-strength': ('relative_strength', build_rel_strength_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_max_intraday_rs_z}} > {t} || {{raw_max_intraday_rs_z}} < -{t}',
            '#4a3614',
        ),
        health_label='RS',
    ),
    'volume_dynamics': Signal(
        name='volume_dynamics',
        emit_key='volume_dynamics',
        row_class=VolumeDynamicsRow,
        # `acceleration` no longer needs an alias — Momentum's is a scalar while
        # Volume Dynamics' is acceleration_22d/90d/180d/252d, so the collision
        # that required the rename no longer exists.
        field_renames={'status': 'volume_dynamics_status'},
        card_builder=build_volume_dynamics_card,
        chart_builders={
            'chart-velocity':     ('volume_dynamics', build_velocity_chart),
            'chart-acceleration': ('volume_dynamics', build_acceleration_chart),
        },
        # No row highlight — Volume Dynamics' velocity surfaces through its own
        # Signals-column glyph (↗) and its card instead.
        health_label='VolDyn',
    ),
    'price_volume_divergence': Signal(
        name='price_volume_divergence',
        emit_key='price_volume_divergence',
        row_class=PriceVolumeDivergenceRow,
        field_renames={'status': 'price_volume_divergence_status'},
        card_builder=build_pv_divergence_card,
        chart_builders={'chart-divergence': ('price_volume_divergence', build_pv_divergence_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_pvd_z_{tier}}} > {t} || {{raw_pvd_z_{tier}}} < -{t}',
            '#4a3614',
        ),
        health_label='PVDiv',
    ),
    'volume_block': Signal(
        name='volume_block',
        emit_key='volume_block',
        row_class=VolumeBlockRow,
        field_renames={'status': 'volume_block_status'},
        card_builder=build_volume_block_card,
        chart_builders={'chart-zscores': ('volume_block', build_volume_block_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_block_volume_z_{tier}}} > {t} || {{raw_block_volume_z_{tier}}} < -{t}',
            '#4a3614',
        ),
        health_label='VolBlk',
        # Writes only at 15-min block boundaries — the default 90s threshold
        # would flag red half the time. 1200s = block interval (15 min) +
        # 5 min safety buffer.
        stale_threshold_seconds=1200,
    ),
    'price_levels': Signal(
        name='price_levels',
        emit_key='price_levels',
        row_class=PriceLevelsRow,
        field_renames={'status': 'price_levels_status'},
        card_builder=build_price_levels_card,
        # No dedicated chart — level state surfaced via row highlight + card.
        highlight_rule=lambda t, tier: (
            '{raw_day_breach_count} > 0',  # structural — neither threshold nor tier applies
            '#4a3614',
        ),
        health_label='Levels',
    ),
    'rolling_momentum': Signal(
        name='rolling_momentum',
        emit_key='rolling_momentum',
        row_class=RollingMomentumRow,
        field_renames={'status': 'rolling_momentum_status'},
        card_builder=build_rolling_momentum_card,
        chart_builders={'chart-rolling-momentum': ('rolling_momentum', build_rolling_momentum_chart)},
        highlight_rule=lambda t, tier: (
            f'{{raw_session_return_z_{tier}}} > {t} || {{raw_session_return_z_{tier}}} < -{t}',
            '#4a3614',
        ),
        health_label='RollMom',
    ),
}


# Fixed output order used by dashboard.py's update_charts callback. The
# composite price chart is inline (crosses signals); the other 8 are driven
# by per-signal chart_builders in SIGNAL_REGISTRY.
CHART_OUTPUT_ORDER = (
    'chart-price',           # composite — inline in dashboard.py
    'chart-velocity',        # volume_dynamics
    'chart-acceleration',    # volume_dynamics
    'chart-vwap-deviation',  # vwap
    'chart-momentum',        # momentum
    'chart-rel-strength',    # relative_strength
    'chart-zscores',         # volume_block
    'chart-divergence',      # price_volume_divergence
    'chart-rolling-momentum',  # rolling_momentum
)


# ============================================================================
# Convenience accessors.
# ============================================================================

def field_renames_for(emit_key: str) -> Dict[str, str]:
    sig = SIGNAL_REGISTRY.get(emit_key)
    return sig.field_renames if sig else {}


def signal_for(emit_key: str) -> Optional[Signal]:
    return SIGNAL_REGISTRY.get(emit_key)


def all_row_classes() -> Tuple[type, ...]:
    """Ordered tuple of every registered signal's Row class."""
    return tuple(s.row_class for s in SIGNAL_REGISTRY.values())
