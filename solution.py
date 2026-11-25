"""兼容常见入口名的薄封装；全部逻辑在 settlement.py。

用法::

    python3 solution.py
    from solution import build_result
"""

from settlement import (  # noqa: F401
    SettlementEngine,
    build_headline,
    build_result,
    load_records,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
