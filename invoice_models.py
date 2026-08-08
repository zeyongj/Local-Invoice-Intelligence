from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

STATUS_OK = "OK"
STATUS_REVIEW = "REVIEW_REQUIRED"
STATUS_FAILED = "FAILED"
STATUS_DUPLICATE = "DUPLICATE"

@dataclass(slots=True)
class TextLine:
    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def norm_x(self) -> float:
        return self.center_x / self.page_width if self.page_width else 0.0

    @property
    def norm_y(self) -> float:
        return self.center_y / self.page_height if self.page_height else 0.0

@dataclass(slots=True)
class FieldValue:
    value: Any = None
    raw: Optional[str] = None
    confidence: float = 0.0
    method: str = ""
    page: Optional[int] = None
    evidence: Optional[str] = None
    x0: Optional[float] = None
    y0: Optional[float] = None
    x1: Optional[float] = None
    y1: Optional[float] = None
    page_width: Optional[float] = None
    page_height: Optional[float] = None

    @property
    def found(self) -> bool:
        return self.value is not None

    @property
    def norm_x(self) -> Optional[float]:
        if self.x0 is None or self.x1 is None or not self.page_width:
            return None
        return ((self.x0 + self.x1) / 2.0) / self.page_width

    @property
    def norm_y(self) -> Optional[float]:
        if self.y0 is None or self.y1 is None or not self.page_height:
            return None
        return ((self.y0 + self.y1) / 2.0) / self.page_height

@dataclass(slots=True)
class DocumentView:
    lines: list[TextLine]
    text: str
    page_count: int
    pages_processed: int
    has_text_layer: bool

@dataclass(slots=True)
class LayoutProfile:
    vendor: str
    field_name: str
    page_num: int
    mean_x: float
    mean_y: float
    mean_w: float
    mean_h: float
    samples: int

@dataclass(slots=True)
class HistoricalStats:
    vendor: str
    account_number: str
    invoice_count: int = 0
    amounts: list[float] = field(default_factory=list)
    known_meters: set[str] = field(default_factory=set)
    last_amount: Optional[float] = None
    last_bill_date: Optional[str] = None

@dataclass(slots=True)
class InvoiceResult:
    file_name: str
    file_path: str
    vendor: str = "UNKNOWN"
    account_number: Optional[str] = None
    bill_date: Optional[str] = None
    billing_period: Optional[str] = None
    current_charges: Optional[float] = None
    total_amount_due: Optional[float] = None
    meter_number: Optional[str] = None
    consumption: Optional[float] = None
    consumption_unit: Optional[str] = None
    previous_balance: Optional[float] = None
    payments: Optional[float] = None
    confidence: float = 0.0
    status: str = STATUS_REVIEW
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    page_count: int = 0
    pages_processed: int = 0
    file_size: int = 0
    mtime_ns: int = 0
    processing_seconds: float = 0.0
    error: Optional[str] = None
    logical_fingerprint: Optional[str] = None
    duplicate_of: Optional[str] = None
