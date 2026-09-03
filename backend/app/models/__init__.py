"""Model package — importing it registers every mapper on ``Base.metadata``."""
from app.models.base import (  # noqa: F401
    Base,
    DeliveryStatus,
    NotificationKind,
    OrderStatus,
    PaymentStatus,
    Role,
    TxnType,
    new_uuid,
    utcnow,
)
from app.models.device import Device, LoginAttempt, Session  # noqa: F401
from app.models.notification import (  # noqa: F401
    AuditLog,
    Counter,
    Notification,
    Setting,
)
from app.models.order import (  # noqa: F401
    Delivery,
    DownloadLog,
    DownloadToken,
    Order,
    OrderItem,
)
from app.models.payment import (  # noqa: F401
    PaymentMethod,
    PaymentRequest,
    PaymentScreenshot,
)
from app.models.product import Category, Product, ProductFile, ProductMedia  # noqa: F401
from app.models.user import MasterAccount, SellerAccount, User, ViewerProfile  # noqa: F401
from app.models.wallet import CoinPackage, Wallet, WalletTransaction  # noqa: F401

__all__ = [
    "AuditLog",
    "Base",
    "Category",
    "CoinPackage",
    "Counter",
    "Delivery",
    "DeliveryStatus",
    "Device",
    "DownloadLog",
    "DownloadToken",
    "LoginAttempt",
    "MasterAccount",
    "Notification",
    "NotificationKind",
    "Order",
    "OrderItem",
    "OrderStatus",
    "PaymentMethod",
    "PaymentRequest",
    "PaymentScreenshot",
    "PaymentStatus",
    "Product",
    "ProductFile",
    "ProductMedia",
    "Role",
    "SellerAccount",
    "Session",
    "Setting",
    "TxnType",
    "User",
    "ViewerProfile",
    "Wallet",
    "WalletTransaction",
    "new_uuid",
    "utcnow",
]
