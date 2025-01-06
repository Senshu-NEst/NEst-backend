from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TestViewSet, ProductViewSet, StockViewSet, TransactionViewSet, StockReceiveHistoryViewSet

router = DefaultRouter()
router.register(r"test", TestViewSet, basename="test")
router.register(r"products", ProductViewSet)
router.register(r'stocks', StockViewSet, basename='stock')
router.register(r"transactions", TransactionViewSet)
router.register(r'stock-receive-history', StockReceiveHistoryViewSet, basename='stockreceivehistory')


urlpatterns = [
    path("", include(router.urls)),
]
