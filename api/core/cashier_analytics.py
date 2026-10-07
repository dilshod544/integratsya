"""Backward-compatibility shim for Cashier Analytics module.

This module re-exports all public symbols from `api.core.cashier`.
The monolithic implementation has been refactored into `api.core.cashier`:
  - `helpers.py`: String normalization, HR status parser, and constants
  - `repository.py`: DB schema initialization and persistence
  - `parsers.py`: Excel/CSV parser for the monthly KPI report
  - `analytics.py`: Workload, BEK/FRONT and analytics queries
"""
from __future__ import annotations

from .cashier import (
    BACK_GROUPS,
    BACK_KEYWORDS,
    EXCLUDED_GROUPS,
    FRONT_GROUPS,
    FRONT_KEYWORDS,
    SUMMARY_KEYWORDS,
    _build_op_column_map,
    _classify_op_group,
    _enrich,
    _find_header_rows,
    cashier_analytics,
    cashier_detail,
    cashier_role,
    clean_branch_name,
    norm,
    num,
    parse_cashiers_xlsx,
    parse_hr_status,
    save_cashier_import,
    sval,
)

__all__ = [
    "clean_branch_name",
    "norm",
    "num",
    "sval",
    "BACK_KEYWORDS",
    "FRONT_KEYWORDS",
    "SUMMARY_KEYWORDS",
    "BACK_GROUPS",
    "FRONT_GROUPS",
    "EXCLUDED_GROUPS",
    "parse_hr_status",
    "save_cashier_import",
    "_classify_op_group",
    "_find_header_rows",
    "_build_op_column_map",
    "parse_cashiers_xlsx",
    "cashier_role",
    "_enrich",
    "cashier_analytics",
    "cashier_detail",
]
