from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TestViewSet, ProductViewSet, StockViewSet, TransactionViewSet, StockReceiveHistoryViewSet, ProductVariationViewSet, generate_receipt_view, WalletViewSet, CustomUserTokenViewSet, ApprovalViewSet, ReturnTransactionViewSet

router = DefaultRouter()
router.register(r"test", TestViewSet, basename="test")
router.register(r"products", ProductViewSet)
router.register(r'variations', ProductVariationViewSet)
router.register(r'stocks', StockViewSet, basename='stock')
router.register(r"transactions", TransactionViewSet, basename='transaction')
router.register(r'returns', ReturnTransactionViewSet, basename='returntransaction')
router.register(r'stock-receive-history', StockReceiveHistoryViewSet, basename='stockreceivehistory')
router.register(r'wallet', WalletViewSet, basename='wallet')
router.register(r"custom-token", CustomUserTokenViewSet, basename="custom-token")
router.register(r"approval", ApprovalViewSet, basename="approval")


urlpatterns = [
    path("", include(router.urls)),
    path('transactions/receipt/id=<int:transaction_id>/<str:receipt_type>/', generate_receipt_view, name='generate_receipt_view'),
]
