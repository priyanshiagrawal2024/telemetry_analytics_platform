"""Telemetry loading helpers (Ingestion Layer).

Reads raw MyJio telemetry exports into a :class:`pandas.DataFrame` for the
downstream Preprocessing / Feature Extraction layers.

Why this exists
---------------
The shipped sample ``sample_data/telemetry_sample.csv`` is actually an **XLSX
workbook** (it begins with the ``PK`` zip signature and contains
``xl/workbook.xml``), not comma-separated text. Naively calling
``pd.read_csv`` on it fails or yields garbage. :func:`load_telemetry` sniffs the
real format from the file's magic bytes and dispatches to the correct reader, so
callers do not have to care about the misleading extension.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

import pandas as pd

__all__ = ["load_telemetry"]

logger = logging.getLogger(__name__)

# ZIP local-file-header magic. XLSX (and other OOXML) files are ZIP containers
# and always start with these two bytes.
_ZIP_MAGIC = b"PK"


def load_telemetry(path: Union[str, Path]) -> pd.DataFrame:
    """Load a telemetry export, auto-detecting XLSX-vs-CSV from content.

    Parameters
    ----------
    path:
        Path to the export. The file *extension is not trusted*; the format is
        determined from the leading magic bytes.

    Returns
    -------
    pandas.DataFrame
        Raw telemetry, one row per event, columns exactly as exported.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Telemetry file not found: {path}")

    with path.open("rb") as fh:
        head = fh.read(2)

    if head == _ZIP_MAGIC:
        # OOXML/XLSX container (the sample's true format despite its .csv name).
        logger.info("Detected XLSX content in %s; reading via openpyxl.", path.name)
        df = pd.read_excel(path, engine="openpyxl")
    else:
        logger.info("Reading %s as delimited text (CSV).", path.name)
        df = pd.read_csv(path)

    logger.info("Loaded %d telemetry rows x %d columns.", len(df), df.shape[1])
    return df
