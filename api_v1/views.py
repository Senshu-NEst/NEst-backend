from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from .models import Product, Stock, Transaction, Store, StockReceiveHistory, CustomUser, StockReceiveHistoryItem
from .serializers import ProductSerializer, StockSerializer, TransactionSerializer, CustomTokenObtainPairSerializer, StockReceiveSerializer
from rest_framework_simplejwt.views import TokenObtainPairView


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer


class StockViewSet(viewsets.ModelViewSet):
    queryset = Stock.objects.all()
    serializer_class = StockSerializer

    def list(self, request, *args, **kwargs):
        store_code = request.query_params.get("store_code")
        jan = request.query_params.get("jan")

        if not store_code and not jan:
            return Response({"error": "パラメーターが不足しています。"}, status=status.HTTP_400_BAD_REQUEST)

        stocks = self.get_stocks(store_code, jan)
        serializer = StockSerializer(stocks, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def get_stocks(self, store_code, jan):
        filters = {}
        if store_code:
            filters["store_code__store_code"] = store_code
        if jan:
            filters["jan__jan"] = jan
        return Stock.objects.filter(**filters)

    @action(detail=False, methods=["post"])
    def receive(self, request):
        serializer = StockReceiveSerializer(data=request.data)

        if serializer.is_valid():
            return self.process_stock_receive(serializer)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def process_stock_receive(self, serializer):
        store_code = serializer.validated_data["store_code"]
        staff_code = serializer.validated_data["staff_code"]
        items = serializer.validated_data["items"]

        try:
            with transaction.atomic():
                stock_receive_history = self.create_stock_receive_history(store_code, staff_code)
                received_items = self.save_received_items(items, stock_receive_history)

                response_data = {
                    "store_code": store_code,
                    "staff_code": staff_code,
                    "received_at": stock_receive_history.received_at.isoformat(),
                    "items": received_items,
                }

                return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": f"入荷処理中にエラーが発生しました: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

    def create_stock_receive_history(self, store_code, staff_code):
        return StockReceiveHistory.objects.create(
            store_code=Store.objects.get(store_code=store_code),
            staff_code=CustomUser.objects.get(staff_code=staff_code),
        )

    def save_received_items(self, items, stock_receive_history):
        received_items = []
        for item in items:
            product = Product.objects.get(jan=item["jan"])
            stock, _ = Stock.objects.get_or_create(store_code=stock_receive_history.store_code, jan=product, defaults={"stock": 0})
            additional_stock = item["additional_stock"]
            stock.stock += additional_stock
            stock.save()

            StockReceiveHistoryItem.objects.create(
                history=stock_receive_history,
                jan=product,
                additional_stock=additional_stock,
            )

            received_items.append({"jan": product.jan, "additional_stock": additional_stock})

        return received_items


class StockReceiveHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = StockReceiveHistory.objects.all()

    def list(self, request, *args, **kwargs):
        store_code = request.query_params.get("store_code")
        jan = request.query_params.get("jan")
        start_date, end_date = self.get_date_range(request)

        filters = {}
        if store_code:
            filters["store_code__store_code"] = store_code

        histories = StockReceiveHistory.objects.filter(**filters, received_at__range=[start_date, end_date]).distinct()

        response_data = self.get_histories_response(histories, jan)
        return Response(response_data)

    def get_date_range(self, request):
        start_date = request.query_params.get("start_date", (datetime.now() - timedelta(days=7)).strftime("%Y%m%d"))
        end_date = request.query_params.get("end_date", datetime.now().strftime("%Y%m%d"))

        start_date = datetime.strptime(start_date, "%Y%m%d")
        end_date = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=1)

        return start_date, end_date

    def get_histories_response(self, histories, jan):
        response_data = []
        for history in histories:
            items = history.items.filter(jan__jan=jan) if jan else history.items.all()

            for item in items:
                response_data.append(
                    {
                        "received_at": history.received_at,
                        "staff_code": history.staff_code.staff_code,
                        "store_code": history.store_code.store_code,
                        "jan": item.jan.jan,
                        "additional_stock": item.additional_stock,
                    }
                )
        return response_data


class TransactionViewSet(viewsets.ModelViewSet):
    queryset = Transaction.objects.all()
    serializer_class = TransactionSerializer


class TestViewSet(viewsets.ViewSet):
    def list(self, request):
        data = {
            "message": "Connection successful!",
            "remote address": request.META.get("REMOTE_ADDR"),
            "current_time": timezone.now(),
        }
        return Response(data)


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
