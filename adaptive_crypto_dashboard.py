"""Launch the dashboard; retain the original Python imports for existing callers.

Implementation lives in the adaptive_crypto package. See README.md.
"""

from adaptive_crypto.core import (
    VERSION,
    ENGINE_VERSION,
    PREVIOUS_ENGINE_VERSION,
    LEGACY_ENGINE_VERSION,
    H4,
    M15,
    DataError,
    finite,
    utc,
    safe_error,
    Rules,
    DEFAULT_ASSETS,
    normalise_pair,
    load_settings,
    Candle,
    validate_candles,
    parse_kraken_rows,
    atr_series,
    ema,
    rsi_series,
    rvol,
    location,
    pivot,
    check,
    passed,
    trend_at,
)

from adaptive_crypto.strategy import (
    reclaim_scan,
    ltf_scan,
    momentum_scan,
    nearby_resistance,
    entry_plan,
)

from adaptive_crypto.ledger import (
    atomic_json,
    fingerprint,
    new_state,
    validate_state,
    upgrade_state,
    StateStore,
    queue_event,
    trade_text,
    portfolio,
    open_trade,
    close_quantity,
    stop_at,
    replay_prices,
    monitor_positions,
)

from adaptive_crypto.engine import (
    reclaim_failure,
    retire_reclaim,
    momentum_stop_breached,
    Engine,
)

from adaptive_crypto.market_data import (
    Kraken,
    LiveCharts,
)

from adaptive_crypto.notifications import (
    telegram_send,
    ai_configuration,
    ai_comment,
    dispatch_once,
    worker,
)

from adaptive_crypto.runtime import (
    DashboardRuntime,
)

from adaptive_crypto.web import (
    number,
    measurement,
    money,
    exact_measurement,
    multiple,
    measurement_rows,
    create_app,
)

from adaptive_crypto.cli import (
    BASE,
    SingleInstance,
    main,
)

if __name__ == "__main__":
    main()
